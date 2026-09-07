"""
Faza 4 — modele Structured Outputs (pydantic) + narzedzie strict-schema.

Koniec parsowania stringow z v1 ('Brand: X,a,b' + replace(' ','')) —
model zwraca JSON zgodny ze schematem wymuszonym przez API. Pulapki
klasyfikacyjne (dzialaja lepiej niz zakazy w promptach):
  type = third_party_brand  -> cudza marka (nikee u academy) — twarde ciecie
  specificity = weak        -> opisowe bez intencji type-in — twarde ciecie
  specificity = category_strong -> ruch aukcyjny (creaditcard) — dopuszczalny,
                                   podrzedny; przechodzi przez sedziego
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Specificity = Literal["brand_core", "branded", "category_strong", "weak"]
KeywordType = Literal["brand", "product", "technology", "service",
                      "category", "third_party_brand", "other"]


class KeywordItem(BaseModel):
    keyword: str = Field(description="Single token, ASCII; join multi-word as one token (Fire TV -> FireTV)")
    type: KeywordType
    specificity: Specificity
    rationale: str = Field(default="", description="One short sentence why a typo of this drives traffic to THIS brand")


class GenerationResult(BaseModel):
    brand_name: str = Field(description="Official brand name as used by the company")
    vertical: str = Field(default="", description="Short vertical label, e.g. 'travel OTA', 'apparel DTC', 'grocery retail'")
    keywords: list[KeywordItem] = Field(default_factory=list)


class JudgeVerdict(BaseModel):
    keyword: str
    accept: bool
    reason: str = ""


class JudgeResult(BaseModel):
    verdicts: list[JudgeVerdict] = Field(default_factory=list)


def to_strict_schema(model: type[BaseModel]) -> dict:
    """model_json_schema() dostosowany do trybu strict OpenAI:
    kazdy obiekt ma additionalProperties=false i required=WSZYSTKIE klucze."""
    schema = model.model_json_schema()

    def patch(node: dict) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
            for child in node["properties"].values():
                patch(child)
        for key in ("items",):
            if key in node:
                patch(node[key])
        for definition in node.get("$defs", {}).values():
            patch(definition)
        for variant in node.get("anyOf", []):
            patch(variant)

    patch(schema)
    return schema


def response_format_for(model: type[BaseModel], name: str) -> dict:
    """Gotowy parametr response_format dla chat.completions."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True,
                        "schema": to_strict_schema(model)},
    }
