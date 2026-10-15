"""Диагностика хода из его записи (`GET /turns/{turn_id}`): что делала модель и что за неё
исправил код. На «прошёл или нет» не влияет; запись недоступна — диагностики нет,
проверки контракта от этого не меняются.

Имена инструментов — из контракта задания и схем сервиса; содержимого набора здесь нет.
"""

import ast
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from evalrun.textmatch import normalize, split_normalize

SEARCH = "search_documents"
CALCULATE = "calculate"
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DIGITS = re.compile(r"\d+")
# Разметка вызова инструмента в шаблонах открытых моделей. Такой вызов в тексте ответа
# провайдер не распознал: инструмент не выполнен, а модель может продолжить, будто выполнен.
# Одинокий маркер без тела вызова — след шаблона, а не потерянный вызов: не считается.
_CALL_IN_TEXT = re.compile(r"(?:<\|?tool_call\|?>|\[TOOL_CALLS\])\s*[^\s<]|<function=")
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


def _plain(number: str) -> str:
    """Число как в тексте: без знака, без лишних нулей и без экспоненты — «2852.50»
    и «2852.5» одно."""
    try:
        return format(abs(Decimal(number)).normalize(), "f")
    except InvalidOperation:
        return number


def source_numbers(text: str) -> set[str]:
    """Опоры чисел — то же правило, что у защиты расчёта в сервисе (решение 15): числа
    источника целиком и их группы цифр. «14.09.2026» даёт 14.09, 14, 9 и 2026; «1143» на
    «114300» не опирается. Сервис здесь не импортируется — прогон видит его только по HTTP;
    совпадение правил сверяет тест."""
    text = normalize(text)
    return {_plain(item) for item in _NUMBER.findall(text) + _DIGITS.findall(text)}


def _whole(text: str) -> set[str]:
    return {_plain(item) for item in _NUMBER.findall(split_normalize(text))}


def _calculate_inputs(item: dict[str, Any]) -> list[str]:
    """Числа, которые модель подала в расчёт: из `numbers` и `expression`."""
    try:
        arguments = json.loads(str(item.get("arguments") or "{}"))
    except ValueError:
        return []
    if not isinstance(arguments, dict):
        return []
    # Каждое значение — отдельно: нормализация убирает пробел между цифрами, и склеенный
    # через пробел список чисел превратился бы в одно число.
    # Как у сервиса: у выражения — его числа из разбора, иначе — список; повтор — один вход.
    if arguments.get("operation") == "expression":
        found = _literals(str(arguments.get("expression") or ""))
    else:
        found = [str(value) for value in arguments.get("numbers") or []]
    return list(dict.fromkeys(_plain(number) for number in found))


def _literals(expression: str) -> list[str]:
    """Числа выражения тем же разбором, что у расчёта в сервисе: `1e5` и `114_300` — одно
    число каждое. Не разбирается — входов нет: сервис такой расчёт не выполнил."""
    prepared = expression.replace("×", "*").replace("÷", "/").replace("−", "-")
    prepared = re.sub(r"(?<=\d),(?=\d)", ".", re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d)", "", prepared))
    try:
        tree = ast.parse(prepared.strip(), mode="eval")
    except SyntaxError, ValueError:
        return []
    return [
        str(node.value) if isinstance(node.value, int) else ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
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
    # Опоры — сообщение пользователя и результаты `ok` в порядке хода, как у сервиса:
    # вход расчёта сверяется с тем, что было до него, включая прежние расчёты.
    sources = source_numbers(question_text)
    # Опора чисел ответа — по всем результатам `ok` и при нормализации строгого сравнения:
    # нормализация задания склеивает «14.09.2026 83.8» в одно число.
    said = source_numbers(question_text) | _whole(question_text)
    inputs: list[str] = []
    inputs_grounded: list[str] = []
    for item in executed:
        if item.get("name") == CALCULATE:
            mine = [n for n in _calculate_inputs(item) if n not in _CONSTANTS]
            inputs += mine
            inputs_grounded += [n for n in mine if n in sources]
        if item.get("status") == "ok":
            result = str(item.get("result", ""))
            sources |= source_numbers(result)
            said |= source_numbers(result) | _whole(result)
    numbers = [_plain(n) for n in _NUMBER.findall(split_normalize(answer))]
    grounded = [n for n in numbers if n in said]
    calls_in_text = sum(
        1
        for call in record.get("calls", [])
        if isinstance(call, dict) and _CALL_IN_TEXT.search(str(call.get("content") or ""))
    )
    return {
        "parse_path": record.get("parse_path"),
        "outcome_replaced": "outcome_replaced" in record,
        "refusal_replaced": "refusal_unsourced" in record,
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
