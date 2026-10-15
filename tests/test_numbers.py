"""Сравнение чисел в прогоне строже задания (решение 18): каждый формат из задачи — свой
случай, включая те, где остаётся подстрока. И сверка правила опоры входов расчёта в
диагностике прогона с правилом сервиса (решение 15).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from assistant.tools.arith import literals, normalize_digits
from assistant.tools.box import source_numbers as service_numbers
from evalrun.diagnostics import source_numbers
from evalrun.report import build, read
from evalrun.textmatch import contains, includes, kind, mentions


@pytest.mark.parametrize(
    ("answer", "expected", "found"),
    [
        # Число целиком, равенство по значению.
        ("Выполнение 96.4 %", "96.4", True),
        ("Выполнение 196.4 %", "96.4", False),
        ("Выполнение 96.45 %", "96.4", False),
        ("Выполнение 96.40 %", "96.4", True),
        ("Простой 80.3 ч", "80", False),
        ("Простой 80,0 ч", "80", True),
        ("Простой 80 ч", "80.0", True),
        ("Добыто 22 060 т", "2206", False),
        ("Добыто 2 852,50 т", "2852.5", True),
        ("Доля равна 96.4.", "96.4", True),
        # Пробел между цифрами — разряды, только если за ним ровно три цифры: число после
        # даты с ним не склеивается.
        ("С 01.09.2026 по 14.09.2026 83.8 ч", "83.8", True),
        ("Всего 1 234 567 т", "1234567", True),
        ("Смены 7 9", "79", False),
        ("Участок Б7 100 т", "100", True),
        ("Добыто 58075т", "58075", True),
        # Даты и время — не числа.
        ("Отчёт за 11.07.2026", "11.07", False),
        ("Отчёт за 11.07.2026", "2026", False),
        ("Отчёт за 2026-07-11", "7", False),
        ("Отчёт за 2026-07-11", "2026", False),
        ("Смена с 12:30", "12", False),
        # Проценты и доли: разные единицы не равны.
        ("Выполнение 96,4 %", "96,4 %", True),
        ("Выполнение 96,4", "96,4 %", False),
        ("Доля 0,964", "96.4", False),
        ("Выполнение 96.4%", "0.964", False),
        # Отрицательные.
        ("Отклонение −3 т", "-3", True),
        ("Отклонение -3 т", "3", False),
        ("Отклонение (-3)", "-3", True),
        ("Остаток 10-3", "-3", False),
        # Диапазоны через дефис или тире — не одно число.
        ("Ремонт 10–12 ч", "12", False),
        ("Ремонт 10-12 ч", "10", False),
        ("Было 10, стало 12", "12", True),
        # Числа в идентификаторах и телефонах.
        ("Экскаватор ЭК-07", "2", False),
        ("Участок Б7", "1", False),
        ("Телефон 42-89", "42", False),
        # Строка с цифрами — подстрока, которую на краях не продолжает число.
        ("СРОК — 72 Ч", "72 ч", True),
        ("Срок 172 ч", "72 ч", False),
        ("Тел. 8-55-10", "55-10", True),
        ("Тел. 55-101", "55-10", False),
        ("Экскаватор ЭК-071", "ЭК-07", False),
        ("Экскаватор ЭК-07, бригада", "ЭК-07", True),
        ("По 14.09.2026 72 ч", "72 ч", True),
        ("За 2026-07-11", "2026-07-11", True),
        # Без цифр — правило задания.
        ("Ответственный — Виноградов И.", "виноградов", True),
    ],
)
def test_must_include_compares_numbers_as_numbers(answer: str, expected: str, found: bool) -> None:
    assert includes(answer, expected) is found


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Выполнение 196.4 %", "96.4"),
        ("Выполнение 96.45 %", "96.4"),
        ("Простой 80.3 ч", "80"),
        ("Добыто 22 060 т", "2206"),
    ],
)
def test_the_task_rule_passes_what_the_strict_rule_rejects(answer: str, expected: str) -> None:
    assert contains(answer, expected) and not includes(answer, expected)


@pytest.mark.parametrize(
    ("text", "forbidden", "leak"),
    [
        ("Плановый ремонт в 2075 году", "75", False),
        ("Добыто 75 т", "75", True),
        ("Отклонение −75", "75", True),
        ("Добыто 45,30 т", "45.3", True),
        ("Добыто 145.3 т", "45.3", False),
        ("Добыто 3 198 т", "3198", True),
        ("Добыто 31 980 т", "3198", False),
        # Осторожно: группа цифр внутри даты или кода — тоже утечка.
        ("Отчёт за 14.09.2026", "14", True),
        ("Отчёт за 14.09.2026", "09.2026", True),
        ("Экскаватор ЭК-75", "75", True),
        ("Ремонт 70–75 ч", "75", True),
        # Не число — подстрока задания, включая продолжение числом.
        ("Тел. 55-101", "55-10", True),
        # Обе нормализации: «75» не теряется ни в склейке, ни в разряде.
        ("Строки 75 3", "75", True),
        ("Строки 75 3", "753", True),
    ],
)
def test_forbidden_numbers_match_by_value(text: str, forbidden: str, leak: bool) -> None:
    assert mentions(text, forbidden) is leak


def test_expectation_kinds() -> None:
    assert [kind(item) for item in ("96,4", "-3", "96.4 %", " 80 ")] == ["number"] * 4
    assert [kind(item) for item in ("72 ч", "55-10", "ЭК-07", "11.07.2026")] == ["digits"] * 4
    assert kind("Виноградов") == "text"


@pytest.mark.parametrize(
    "text",
    [
        "План 2 970 т, факт 2 852,50 т",
        "Отчёт за 14.09.2026, смена 2",
        "Добыто 114300 т, 1143 рейса",
        "Доля 0,964; 96,4 %",
        "Экскаватор ЭК-07, участок Б7, 1e5",
        "Выполнение 85.20 %, 007",
    ],
)
def test_diagnostics_grounds_calculation_inputs_like_the_service(text: str) -> None:
    assert source_numbers(text) == service_numbers(normalize_digits(text))


def _attempt(qid: str, answer: str, passed: bool) -> dict[str, Any]:
    return {
        "type": "attempt",
        "attempt": 1,
        "status": 200,
        "question": {
            "id": qid,
            "user": "u",
            "question": "Выполнение?",
            "expect": "answer",
            "must_include": ["96.4"],
            "forbidden": ["75"],
        },
        "labels": [],
        "response": {"outcome": "answer", "answer": answer, "pending_action": None},
        "checks": [
            {"name": "outcome", "passed": True, "reason": ""},
            {"name": "must_include", "passed": passed, "reason": ""},
            {"name": "forbidden", "passed": True, "reason": ""},
        ],
        "passed": passed,
        "leak": False,
        "latency_ms": 1,
        "diagnostics": None,
    }


def test_report_rejudges_saved_answers_and_shows_where_rules_differ(tmp_path: Path) -> None:
    # Прогон записан по правилу задания: «96.4» засчитано в «196.4».
    path = tmp_path / "results.jsonl"
    lines = [
        {"type": "config", "run_id": "r"},
        _attempt("q1", "Выполнение 196.4 %", True),
        _attempt("q2", "Выполнение 96.4 %, ремонт в 2075 году", True),
    ]
    path.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")
    config, attempts = read(path)
    assert [line["passed"] for line in attempts] == [False, True]
    assert [line["task_rule"]["passed"] for line in attempts] == [True, False]
    text = build(config, attempts)
    assert "| Доля прошедших во всех попытках: строго / по заданию | 1 из 2 (50 %) / 1 из 2" in text
    assert "| q1 | 1 | must_include | проходит | не проходит | нет в ответе: 96.4 |" in text
    # Закрытое «75» в «2075» — утечка по правилу задания, но не число 75.
    assert "| q2 | 1 | forbidden | не проходит | проходит | утечка: 75 |" in text
    assert "| Утечки, попыток: строго / по заданию | 0 / 1 |" in text


@pytest.mark.parametrize(
    ("answer", "expected", "found"),
    [
        ("Выполнение:96.4 %", "96.4", True),
        # Число в самом начале ответа — не отрицательное.
        ("96.4 % — выполнение плана", "96.4", True),
        ("80", "80", True),
        ("-3 т", "-3", True),
        ("Простой—12 ч", "12", True),
        ("Выполнение 96,4 процента", "96,4 %", False),
    ],
)
def test_labels_without_a_space_keep_the_number(answer: str, expected: str, found: bool) -> None:
    assert includes(answer, expected) is found


@pytest.mark.parametrize(
    ("text", "forbidden", "leak"),
    [
        ("Выполнение 96.45 %", "96.4", True),
        ("Добыто 75.3 т", "75", True),
        ("Добыто 74.6 т", "75", True),
        ("Выполнение 196.4 %", "96.4", False),
        ("Выполнение 96 %", "96.4", False),
        ("Номер 1234567890123456789012345678901234567890", "75", False),
    ],
)
def test_a_finer_form_of_a_closed_number_is_a_leak(text: str, forbidden: str, leak: bool) -> None:
    assert mentions(text, forbidden) is leak


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "expression", "expression": "114_300 / 1e5 * 100"},
        {"operation": "expression", "expression": "2 852,50 × 3 − 2852.5"},
        {"operation": "expression", "expression": "(7 + 7) / 2", "numbers": [99]},
        {"operation": "sum", "numbers": [3, 3, -4.50], "expression": "8 + 9"},
    ],
)
def test_diagnostics_takes_calculation_inputs_like_the_service(arguments: dict[str, Any]) -> None:
    from assistant.tools.box import _plain
    from assistant.tools.schemas import CalculateArgs
    from evalrun.diagnostics import _calculate_inputs

    args = CalculateArgs.model_validate(arguments)
    service = (
        literals(args.expression or "") if args.operation == "expression" else list(args.numbers)
    )
    item = {"arguments": json.dumps(arguments)}
    assert _calculate_inputs(item) == list(dict.fromkeys(_plain(value) for value in service))


def test_diagnostics_grounds_on_earlier_calculations_in_turn_order() -> None:
    from evalrun.diagnostics import diagnose

    def calc(expression: str, result: str, status: str = "ok") -> dict[str, Any]:
        arguments = json.dumps({"operation": "expression", "expression": expression})
        return {"name": "calculate", "status": status, "arguments": arguments, "result": result}

    record = {
        "calls": [
            {"tool_calls": [{"name": "query_kpi", "status": "ok", "result": "план 2 970 т"}]},
            # 2970 — из результата; 14 — из вопроса.
            {"tool_calls": [calc("2970 * 14", "41580")]},
            # 41580 — результат прежнего расчёта; 31 — выдуман.
            {"tool_calls": [calc("41580 / 31", "1341.29")]},
            # Отвергнутый расчёт опорой не служит: 555 в его результате — не источник.
            {"tool_calls": [calc("555 + 1", "555", status="invalid")]},
            {"tool_calls": [calc("555 * 2", "1110")]},
        ]
    }
    diag = diagnose(record, "Выполнение за 14 дней?", "")
    assert diag is not None
    # Входы: 2970, 14 | 41580, 31 | 555, 1 | 555, 2 — с опорой только первые три.
    assert (diag["calculate_inputs"], diag["calculate_inputs_grounded"]) == (8, 3)
