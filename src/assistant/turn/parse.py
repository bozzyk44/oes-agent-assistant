"""Разбор итога хода: основной путь — объект JSON, запасной — текст.

Код не требует чистого JSON и разбирает ответ по убыванию строгости. Из-за формата вопрос
не проваливается: хуже становится только точность цитат и исхода. Блок размышлений
вырезается до разбора и никуда не попадает.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

PROPOSABLE = ("answer", "refusal", "no_data")
_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>.*", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BRACKETED = re.compile(r"\[([^\[\]\s]+#[0-9][0-9.]*)\]")


@dataclass(frozen=True)
class Parsed:
    # Путь разбора — в запись хода: доля запасного пути — сигнал о модели.
    path: str
    answer: str
    outcome: str | None
    citations: tuple[str, ...]
    # Объект без поля answer: итогом не считается.
    is_final: bool = True


def strip_thinking(text: str) -> str:
    text = _THINK.sub("", text)
    return _UNCLOSED_THINK.sub("", text).strip()


def _objects(text: str) -> list[Any]:
    """Кандидаты в объект: весь текст, блоки кода, первый объект внутри текста."""
    candidates = [text]
    candidates += _FENCE.findall(text)
    start = text.find("{")
    if start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            candidates.append(value)
        except ValueError:
            pass
    found = []
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                continue
        if isinstance(candidate, dict):
            found.append(candidate)
    return found


def _bracketed(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_BRACKETED.findall(text)))


def parse_final(content: str | None) -> Parsed:
    text = strip_thinking(content or "")
    objects = _objects(text)
    for value in objects:
        answer = value.get("answer")
        if not isinstance(answer, str):
            continue
        outcome = value.get("outcome")
        raw = value.get("citations")
        citations = (
            tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()
        )
        citations = tuple(dict.fromkeys((*citations, *_bracketed(answer))))
        if outcome in PROPOSABLE:
            path = "json" if isinstance(raw, list) else "json_partial"
            return Parsed(path, answer.strip(), str(outcome), citations)
        return Parsed("json_partial", answer.strip(), None, citations)
    if objects:
        # Объект без answer: возможно, вызов инструмента текстом — итогом не считается.
        return Parsed("object_without_answer", "", None, (), is_final=False)
    return Parsed("text", text, None, _bracketed(text))
