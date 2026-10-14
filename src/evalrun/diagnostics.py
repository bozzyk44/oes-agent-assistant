"""Диагностика хода из его записи (`GET /turns/{turn_id}`): что делала модель и что за неё
исправил код. На «прошёл или нет» не влияет; запись недоступна — диагностики нет,
проверки контракта от этого не меняются.

Имена инструментов — из контракта задания и схем сервиса; содержимого набора здесь нет.
"""

import re
from typing import Any

from evalrun.textmatch import normalize

SEARCH = "search_documents"
CALCULATE = "calculate"
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _tool_calls(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for call in record.get("calls", [])
        if isinstance(call, dict)
        for item in call.get("tool_calls", [])
        if isinstance(item, dict)
    ]


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
