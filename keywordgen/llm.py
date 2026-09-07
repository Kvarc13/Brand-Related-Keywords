"""
Faza 4 — transport LLM (szew wstrzykiwalny).

Cala logika pipeline'u zna TYLKO ten interfejs: generate() i judge().
Testy wstrzykuja FakeTransport (100% offline); produkcja uzywa
OpenAITransport. API OpenAI jest nietestowalne z sandboxa dostawcy,
wiec warstwa jest cienka, defensywna i degraduje sie glosno:
  - generate: chat.completions + response_format json_schema (strict)
  - judge grounded (Q9, brandy fallback): Responses API + web_search;
    kazdy blad ksztaltu/API -> fallback do judge bez narzedzi + WARNING
  - usage tokenow zwracane przy kazdym wywolaniu (log kosztow w runnerze)
"""

from __future__ import annotations

import json
import logging

from keywordgen.models import (GenerationResult, JudgeResult,
                               response_format_for, to_strict_schema)

logger = logging.getLogger("keywordgen.llm")


class TransportError(Exception):
    pass


class OpenAITransport:
    def __init__(self, model: str = "gpt-5-mini", judge_model: str | None = None,
                 reasoning_effort: str = "minimal"):
        from openai import OpenAI  # leniwie: testy nie potrzebuja klucza
        self.client = OpenAI()
        self.model = model
        self.judge_model = judge_model or model
        self.reasoning_effort = reasoning_effort

    # --- generacja ---
    def generate(self, messages: list[dict]) -> tuple[GenerationResult, dict]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            reasoning_effort=self.reasoning_effort,
            response_format=response_format_for(GenerationResult, "keyword_generation"),
            max_completion_tokens=2000,
        )
        content = response.choices[0].message.content
        if not content:
            raise TransportError("Pusta odpowiedz generatora")
        parsed = GenerationResult.model_validate_json(content)
        return parsed, _usage(response)

    # --- sedzia ---
    def judge(self, messages: list[dict], grounded: bool) -> tuple[JudgeResult, dict]:
        if grounded:
            try:
                return self._judge_grounded(messages)
            except Exception as exc:
                logger.warning("Sedzia z groundingiem padl (%s) — degradacja "
                               "do sedziego bez narzedzi.", str(exc)[:120])
        return self._judge_plain(messages)

    def _judge_plain(self, messages: list[dict]) -> tuple[JudgeResult, dict]:
        response = self.client.chat.completions.create(
            model=self.judge_model,
            messages=messages,
            reasoning_effort=self.reasoning_effort,
            response_format=response_format_for(JudgeResult, "keyword_judge"),
            max_completion_tokens=1500,
        )
        content = response.choices[0].message.content
        if not content:
            raise TransportError("Pusta odpowiedz sedziego")
        return JudgeResult.model_validate_json(content), _usage(response)

    def _judge_grounded(self, messages: list[dict]) -> tuple[JudgeResult, dict]:
        """Responses API + web_search. Ksztalt API moze sie roznic miedzy
        wersjami SDK — stad defensywnosc i degradacja w judge()."""
        response = self.client.responses.create(
            model=self.judge_model,
            input=[{"role": m["role"] if m["role"] != "developer" else "developer",
                    "content": m["content"]} for m in messages],
            tools=[{"type": "web_search"}],
            text={"format": {"type": "json_schema", "name": "keyword_judge",
                             "strict": True,
                             "schema": to_strict_schema(JudgeResult)}},
        )
        content = getattr(response, "output_text", None)
        if not content:
            raise TransportError("Pusta odpowiedz sedziego (responses)")
        usage = {"prompt_tokens": getattr(getattr(response, "usage", None),
                                          "input_tokens", 0) or 0,
                 "completion_tokens": getattr(getattr(response, "usage", None),
                                              "output_tokens", 0) or 0}
        return JudgeResult.model_validate_json(content), usage


def _usage(response) -> dict:
    usage = getattr(response, "usage", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }


class FakeTransport:
    """Transport testowy: skrypt odpowiedzi per wywolanie."""

    def __init__(self, generations: list[GenerationResult] | None = None,
                 judge_results: list[JudgeResult | None] | None = None):
        import threading
        self._lock = threading.Lock()   # 9.1: pula per-brand dzieli transport
        self._generations = list(generations or [])
        self._judges = list(judge_results or [])
        self.generate_calls: list[list[dict]] = []
        self.judge_calls: list[tuple[list[dict], bool]] = []

    def generate(self, messages):
        with self._lock:
            self.generate_calls.append(messages)
            if not self._generations:
                raise TransportError("FakeTransport: brak zaplanowanych generacji")
            result = self._generations.pop(0)
        return result, {"prompt_tokens": 10, "completion_tokens": 10}

    def judge(self, messages, grounded):
        with self._lock:
            self.judge_calls.append((messages, grounded))
            if not self._judges:
                raise TransportError("FakeTransport: brak zaplanowanych werdyktow")
            result = self._judges.pop(0)
        if result is None:
            raise TransportError("FakeTransport: symulowany blad sedziego")
        return result, {"prompt_tokens": 5, "completion_tokens": 5}
