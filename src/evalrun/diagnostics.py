"""Диагностика хода из его записи (`GET /turns/{turn_id}`): что делала модель и что за неё
исправил код. На «прошёл или нет» не влияет; запись недоступна — диагностики нет,
проверки контракта от этого не меняются.

Имена инструментов — из контракта задания и схем сервиса; содержимого набора здесь нет.
"""

import json
import re
from typing import Any

from evalrun.textmatch import normalize

SEARCH = "search_documents"
CALCULATE = "calculate"
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# Разметка вызова инструмента в шаблонах открытых моделей. Такой вызов в тексте ответа
# провайдер не распознал: инструмент не выполнен, а модель может продолжить, будто выполнен.
_CALL_IN_TEXT = re.compile(r"<\|?tool_call\|?>|\[TOOL_CALLS\]|<function=")
# Постоянная перевода доли в проценты: в выражении расчёта она законна без источника.
_CONSTANTS = frozenset({"100"})


def _tool_calls(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for call in record.get("calls", [])
        if isinstance(call, dict)
        for item in call.get("tool_calls", [])
        if isinstance(item, dict)
    ]


def _calculate_inputs(item: dict[str, Any]) -> list[str]:
    """Числа, которые модель подала в расчёт: из `numbers` и `expression`."""
    try:
        arguments = json.loads(str(item.get("arguments") or "{}"))
    except ValueError:
        return []
    if not isinstance(arguments, dict):
        return []
    text = " ".join(
        [str(value) for value in arguments.get("numbers") or []]
        + [str(arguments.get("expression") or "")]
    )
    return _NUMBER.findall(normalize(text))


def diagnose(
    record: dict[str, Any] | None, question_text: str, answer: str
) -> dict[str, Any] | None:
    if record is None:
        return None
    calls = _tool_calls(record)
    executed = [item for item in calls if "status" in item]
    searches = [item for item in executed if item.get("name") == SEARCH]
    chunks = {str(chunk) for chunk in record.get("history_chunks", [])}
    for item in executed:
        chunks.update(str(chunk) for chunk in item.get("chunks", []) or [])
    results = normalize(" ".join(str(item.get("result", "")) for item in executed))
    numbers = _NUMBER.findall(normalize(answer))
    from_question = set(_NUMBER.findall(normalize(question_text)))
    grounded = [n for n in numbers if n in from_question or n in results]
    # Входы расчёта сверяются с результатами остальных инструментов: результат самого расчёта
    # опорой своим входам не служит, иначе выдуманный вход «подтверждал» бы сам себя.
    sources = normalize(
        " ".join(str(item.get("result", "")) for item in executed if item.get("name") != CALCULATE)
    )
    inputs = [
        n
        for item in executed
        if item.get("name") == CALCULATE
        for n in _calculate_inputs(item)
        if n not in _CONSTANTS
    ]
    inputs_grounded = [n for n in inputs if n in from_question or n in sources]
    calls_in_text = sum(
        1
        for call in record.get("calls", [])
        if isinstance(call, dict) and _CALL_IN_TEXT.search(str(call.get("content") or ""))
    )
    return {
        "parse_path": record.get("parse_path"),
        "outcome_replaced": "outcome_replaced" in record,
        "dropped_citations": len(record.get("dropped_citations", []) or []),
        "budget_stop": "stopped" in record,
        "tool_calls": len(executed),
        "invalid_calls": sum(1 for item in executed if item.get("status") == "invalid"),
        "tool_calls_after_ban": len(record.get("tool_calls_after_ban", []) or []),
        "searches": len(searches),
        "empty_searches": sum(1 for item in searches if item.get("status") == "empty"),
        "calculate_used": any(item.get("name") == CALCULATE for item in executed),
        "chunks_in_input": sorted(chunks),
        "docs_in_input": sorted({chunk.split("#", 1)[0] for chunk in chunks}),
        "numbers": len(numbers),
        "numbers_grounded": len(grounded),
        "calculate_inputs": len(inputs),
        "calculate_inputs_grounded": len(inputs_grounded),
        "calls_in_text": calls_in_text,
        "model_calls": len(record.get("calls", [])),
    }


def fragment_in_input(labels: tuple[str, ...], chunks: list[str]) -> bool | None:
    """Все размеченные фрагменты были во входе. Раздел засчитывает и свои части: `D#2` —
    это и `D#2.1`. Нет разметки — `None`."""
    if not labels:
        return None
    return all(
        any(chunk == label or chunk.startswith(f"{label}.") for chunk in chunks) for label in labels
    )
