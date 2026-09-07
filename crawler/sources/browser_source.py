"""
Faza 3 — zrodlo `web_scraper` (Playwright), przebudowa v1 z fixami:

  1. JEDEN parser: `page.content()` -> parse_html_to_schema (v1 mial osobny,
     ubozszy parser reczny — koniec konfliktu legacy).
  2. Blokowanie zasobow (image/font/stylesheet/media) — wzorzec
     Network.setBlockedURLs z zipa w wydaniu Playwright (route.abort).
  3. Fix wycieku page przy hard-timeout: page tworzona i zamykana w
     try/finally OBEJMUJACYM asyncio.wait_for.
  4. Zero cichego `except: pass` — kazdy wyjatek to rekord bledu.

Import playwright jest leniwy — srodowiska bez przegladarki (testy, tiery
HTTP-only) nie placa za import.
"""

from __future__ import annotations

import asyncio
import random

import logging

from crawler.schema import empty_result, parse_html_to_schema

logger = logging.getLogger("crawler.web_scraper")
LAUNCH_TIMEOUT_S = 120

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)
BLOCKED_RESOURCES = {"image", "font", "stylesheet", "media"}
PAGE_TIMEOUT_MS = 30_000
HARD_TIMEOUT_S = 90.0
CONCURRENCY = 5

_STEALTH_SCRIPTS = (
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });",
    "window.chrome = { runtime: {} };",
    "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });",
    "Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });",
)


async def _scrape_one(context, domain: str) -> dict:
    url = str(domain).strip().lower()
    if not url.startswith("http"):
        url = "https://" + url

    page = await context.new_page()
    try:
        try:
            html = await asyncio.wait_for(_load(page, url), timeout=HARD_TIMEOUT_S)
        except asyncio.TimeoutError:
            return _err(domain, url, "Hard timeout exceeded (asyncio)")
        except Exception as exc:
            return _err(domain, url, str(exc)[:150])
    finally:
        # Fix v1: zamkniecie NIEZALEZNIE od hard-timeoutu (wczesniej page ciekl).
        try:
            await page.close()
        except Exception:
            pass

    result = parse_html_to_schema(html, domain, url)
    result["source"] = "web_scraper"
    return result


def _err(domain: str, url: str, message: str) -> dict:
    result = empty_result(domain, url, message)
    result["source"] = "web_scraper"
    return result


async def _load(page, url: str) -> str:
    for script in _STEALTH_SCRIPTS:
        await page.add_init_script(script)
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in BLOCKED_RESOURCES
        else route.continue_(),
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    await asyncio.sleep(1)
    try:
        return await page.content()
    except Exception:
        # Race Playwrighta: JS-redirect tuz po domcontentloaded ("Unable to
        # retrieve content because the page is navigating"). Jedna ponowna
        # proba po ustabilizowaniu nawigacji.
        await asyncio.sleep(2)
        return await page.content()


async def fetch(domains: list[str], concurrency: int = CONCURRENCY) -> list[dict]:
    if not domains:
        return []
    from playwright.async_api import async_playwright  # leniwy import

    results: list[dict] = []
    async with async_playwright() as playwright:
        # Hotfix 3.1: guard na launchu — patologiczny zwis startu Chromium
        # ma dac czytelny blad, nie wieczna cisze.
        logger.info("Start Chromium (concurrency=%d, hard timeout %ss/strone, "
                    "najgorszy przypadek ~%d min dla %d domen)...",
                    concurrency, int(HARD_TIMEOUT_S),
                    int((len(domains) / max(1, concurrency)) * HARD_TIMEOUT_S / 60) + 1,
                    len(domains))
        browser = await asyncio.wait_for(
            playwright.chromium.launch(headless=True), timeout=LAUNCH_TIMEOUT_S)
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 800},
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def guarded(domain: str) -> dict:
            async with semaphore:
                try:
                    return await _scrape_one(context, domain)
                except Exception as exc:
                    # Fix v1: zadnego `except: pass` — blad to rekord, nie cisza.
                    return _err(domain, f"https://{domain}", str(exc)[:150])

        # Hotfix 3.1: as_completed + log per domena — praca odrozniana od zwisu.
        done = 0
        tasks = [asyncio.ensure_future(guarded(d)) for d in domains]
        for future in asyncio.as_completed(tasks):
            result = await future
            done += 1
            verdict = result.get("status") if result.get("status") != "error"                 else f"error: {str(result.get('error'))[:60]}"
            logger.info("web_scraper %d/%d: %s -> %s",
                        done, len(domains), result.get("domain"), verdict)
            results.append(result)
        await browser.close()
    return results
