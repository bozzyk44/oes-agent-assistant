"""Таблица статусов и `outcome`: каждая из восьми строк на обоих путях разбора ответа.

Путь JSON — модель назвала исход; текстовый путь — исход не назван (`None`). Строки 1–4
не зависят от предложения модели и проверяются на всех вариантах; строки 5–7 бывают только
на пути JSON, строка 8 — только на текстовом.
"""

import pytest

from assistant.access.status import Status
from assistant.turn.outcome import (
    DOCUMENT,
    KPI,
    REPAIR,
    SEARCH,
    ToolStatus,
    resolve_outcome,
)

ANY_PROPOSAL = ("answer", "refusal", "no_data", None)
CALCULATE = "calculate"


def _resolve(
    calls: list[ToolStatus],
    proposed: str | None,
    *,
    cited: bool = False,
    action: bool = False,
) -> tuple[str, int]:
    verdict = resolve_outcome(calls, proposed=proposed, cited=cited, action_drafted=action)
    return verdict.outcome, verdict.row


@pytest.mark.parametrize("proposed", ANY_PROPOSAL)
def test_row_1_action_draft_is_action(proposed: str | None) -> None:
    calls = [ToolStatus(SEARCH, Status.OK), ToolStatus(REPAIR, Status.OK)]
    assert _resolve(calls, proposed, action=True) == ("action", 1)


@pytest.mark.parametrize("proposed", ANY_PROPOSAL)
def test_row_2_denied_repair_is_refusal_even_with_data(proposed: str | None) -> None:
    calls = [ToolStatus(KPI, Status.OK), ToolStatus(REPAIR, Status.DENIED)]
    assert _resolve(calls, proposed, cited=True) == ("refusal", 2)


@pytest.mark.parametrize("proposed", ANY_PROPOSAL)
@pytest.mark.parametrize(
    "signal",
    [
        ToolStatus(DOCUMENT, Status.DENIED),
        ToolStatus(KPI, Status.DENIED),
        ToolStatus(SEARCH, Status.OK, best_match_closed=True),
        ToolStatus(KPI, Status.EMPTY, partially_denied=True),
    ],
    ids=["документ", "таблица", "закрытое совпадение", "часть участков"],
)
def test_row_3_refusal_signal_without_used_data_is_refusal(
    signal: ToolStatus, proposed: str | None
) -> None:
    # Поиск без принятой цитаты использованными данными не считается.
    calls = [ToolStatus(SEARCH, Status.OK), signal]
    assert _resolve(calls, proposed) == ("refusal", 3)


@pytest.mark.parametrize(
    ("proposed", "expected"),
    [("answer", "answer"), (None, "answer"), ("refusal", "refusal"), ("no_data", "refusal")],
)
@pytest.mark.parametrize(
    ("calls", "cited"),
    [
        ([ToolStatus(SEARCH, Status.OK, best_match_closed=True)], True),
        ([ToolStatus(KPI, Status.OK, partially_denied=True)], False),
    ],
    ids=["цитата", "КХД"],
)
def test_row_4_refusal_signal_with_used_data(
    calls: list[ToolStatus], cited: bool, proposed: str | None, expected: str
) -> None:
    assert _resolve(calls, proposed, cited=cited) == (expected, 4)


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([], "refusal"),
        ([ToolStatus(CALCULATE, Status.OK)], "refusal"),
        ([ToolStatus(SEARCH, Status.INVALID)], "refusal"),
        ([ToolStatus(KPI, Status.NOT_FOUND)], "no_data"),
        ([ToolStatus(KPI, Status.EMPTY)], "no_data"),
        ([ToolStatus(SEARCH, Status.OK)], "refusal"),
        ([ToolStatus(SEARCH, Status.EMPTY)], "no_data"),
        ([ToolStatus(DOCUMENT, Status.NOT_FOUND)], "no_data"),
        ([ToolStatus(DOCUMENT, Status.OK)], "no_data"),
    ],
    ids=[
        "без инструментов",
        "расчёт",
        "неверные аргументы",
        "не найдено",
        "пусто",
        "поиск без цитаты",
        "пустой поиск",
        "нет документа",
        "документ",
    ],
)
def test_row_5_model_refusal_without_signal(calls: list[ToolStatus], expected: str) -> None:
    assert _resolve(calls, "refusal") == (expected, 5)


def test_row_5_search_with_accepted_citation_reached_data() -> None:
    assert _resolve([ToolStatus(SEARCH, Status.OK)], "refusal", cited=True) == ("no_data", 5)


@pytest.mark.parametrize("cited", [False, True])
def test_row_6_model_no_data_without_signal(cited: bool) -> None:
    assert _resolve([ToolStatus(KPI, Status.EMPTY)], "no_data", cited=cited) == ("no_data", 6)


def test_row_7_model_answer_without_signal() -> None:
    assert _resolve([], "answer") == ("answer", 7)
    assert _resolve([ToolStatus(KPI, Status.EMPTY)], "answer") == ("answer", 7)


@pytest.mark.parametrize(
    ("calls", "cited", "expected"),
    [
        ([], False, "no_data"),
        ([ToolStatus(KPI, Status.EMPTY)], False, "no_data"),
        ([ToolStatus(SEARCH, Status.EMPTY)], False, "no_data"),
        ([ToolStatus(SEARCH, Status.OK)], False, "answer"),
        ([ToolStatus(KPI, Status.OK)], False, "answer"),
        ([ToolStatus(CALCULATE, Status.OK)], False, "answer"),
        ([], True, "answer"),
    ],
    ids=["ничего", "пусто КХД", "пусто поиск", "поиск", "КХД", "расчёт", "цитата"],
)
def test_row_8_outcome_not_named(calls: list[ToolStatus], cited: bool, expected: str) -> None:
    assert _resolve(calls, None, cited=cited) == (expected, 8)


@pytest.mark.parametrize("proposed", ["action", "error", "budget_exceeded", "ANSWER", ""])
def test_outcome_the_model_may_not_propose_counts_as_not_named(proposed: str) -> None:
    # `action`, `budget_exceeded` и `error` ставит только код.
    assert _resolve([], proposed) == ("no_data", 8)
    assert _resolve([ToolStatus(SEARCH, Status.OK)], proposed) == ("answer", 8)


def test_rows_apply_top_down() -> None:
    # Черновик важнее отказа в заявке, отказ в заявке — важнее данных и предложения модели.
    denied_repair = ToolStatus(REPAIR, Status.DENIED)
    assert _resolve([denied_repair], "answer", cited=True, action=True) == ("action", 1)
    assert _resolve([denied_repair, ToolStatus(KPI, Status.OK)], "answer") == ("refusal", 2)
    # Сигнал отказа важнее предложения модели.
    closed = ToolStatus(SEARCH, Status.EMPTY, best_match_closed=True)
    assert _resolve([closed], "no_data") == ("refusal", 3)
