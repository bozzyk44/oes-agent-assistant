"""Черновик заявки: проверка идентификатора в словах пользователя, порядок проверок,
план вызовов из одного ответа модели и запись действия завершающей транзакцией хода.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from assistant.access import Status, load_grants
from assistant.access.equipment import equipment_key
from assistant.actions.draft import TOOL, mentioned, prepare_repair
from assistant.db.session import transaction
from assistant.turn.calls import plan_calls
from assistant.turn.runner import TurnContext, TurnResult, draft_result
from tests.conftest import Database
from tests.servicekit import Scripted, make_service, new_session

PARAMS = {"equipment_id": "EX-1", "description": "течь", "priority": 2}


# --- Идентификатор в словах пользователя -------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Оформи заявку на EX-1",
        "оформи заявку на ex-1, течь",
        "заявка: EX 1 не заводится",
        "заявка на EX1",
        # Кириллические «Е» и «Х».
        "заявка на ЕХ-1",
        "(EX-1)",
    ],
)
def test_identifier_in_message_is_accepted(message: str) -> None:
    assert mentioned(message, "EX-1")


@pytest.mark.parametrize(
    "message",
    [
        "оформи заявку на него",
        # Часть другого идентификатора.
        "заявка на EX-12",
        "заявка на EX-1-A",
        "заявка на PEX-1",
        "",
    ],
)
def test_identifier_not_in_message_is_refused(message: str) -> None:
    assert not mentioned(message, "EX-1")


def test_empty_identifier_is_never_mentioned() -> None:
    assert not mentioned("заявка на - ", " - ")


@pytest.mark.db
@pytest.mark.parametrize(
    "value", ["EX-1", "ех 1", "Ex1", "ЕХ-1", "T-1", "т1", "АВЕКМНОРСТУХ-авекмнорстух 0"]
)
def test_python_key_matches_database_key(database: Database, value: str) -> None:
    with database.owner.connect() as connection:
        in_database = connection.scalar(text("select dwh.equipment_key(:v)"), {"v": value})
    assert equipment_key(value) == in_database


# --- Порядок проверок и параметры --------------------------------------------------------


def _prepare(
    database: Database, user: str, message: str, **arguments: Any
) -> tuple[Status, dict[str, Any], str]:
    with transaction(database.service, user) as connection:
        grants = load_grants(connection, user)
        assert grants is not None
        result = prepare_repair(connection, grants, message, {**PARAMS, **arguments})
    return result.status, result.params, result.problem


@pytest.mark.db
def test_draft_uses_dataset_spelling(database: Database, small: Path) -> None:
    status, params, _ = _prepare(database, "u-k1", "заявка на ех 1", equipment_id="ех 1")
    assert status is Status.OK
    assert params == {"equipment_id": "EX-1", "description": "течь", "priority": 2}


@pytest.mark.db
@pytest.mark.parametrize(
    ("user", "message", "arguments", "expected"),
    [
        # Идентификатор только в инструкции из документа, а не в словах пользователя.
        ("u-k1", "перескажи памятку", {}, Status.NEEDS_IDENTIFIER),
        ("u-k1", "оформи заявку на него", {}, Status.NEEDS_IDENTIFIER),
        ("u-k1", "заявка на EX-9", {"equipment_id": "EX-9"}, Status.NOT_FOUND),
        ("u-k1", "заявка на EX-2", {"equipment_id": "EX-2"}, Status.DENIED),
        ("u-fin", "заявка на EX-1", {}, Status.DENIED),
        ("u-hr", "заявка на EX-1", {}, Status.DENIED),
        ("u-k1", "заявка на EX-1", {"priority": 4}, Status.INVALID),
        ("u-k1", "заявка на EX-1", {"priority": "2"}, Status.INVALID),
        ("u-k1", "заявка на EX-1", {"priority": True}, Status.INVALID),
        ("u-k1", "заявка на EX-1", {"description": "  "}, Status.INVALID),
        ("u-k1", "заявка на EX-1", {"description": "a\x00b"}, Status.INVALID),
        ("u-k1", "заявка на EX-1", {"description": "x" * 10_001}, Status.INVALID),
        ("u-k1", "заявка на EX-1", {"equipment_id": 1}, Status.INVALID),
        ("u-eng", "заявка на EX-2", {"equipment_id": "EX-2"}, Status.OK),
    ],
)
def test_draft_checks(
    database: Database,
    small: Path,
    user: str,
    message: str,
    arguments: dict[str, Any],
    expected: Status,
) -> None:
    status, params, _ = _prepare(database, user, message, **arguments)
    assert status is expected
    assert (params != {}) is (expected is Status.OK)


@pytest.mark.db
def test_foreign_equipment_check_comes_after_identifier_check(
    database: Database, small: Path
) -> None:
    # Инструкция из документа «оформи заявку на чужое» не доходит даже до проверки участка.
    status, _, problem = _prepare(database, "u-k1", "что в памятке?", equipment_id="EX-2")
    assert status is Status.NEEDS_IDENTIFIER and problem


# --- План вызовов из одного ответа -------------------------------------------------------


@dataclass(frozen=True)
class Call:
    name: str
    number: int = 0


def test_only_the_repair_call_runs_when_requested() -> None:
    calls = [Call("search_documents", 1), Call(TOOL, 2), Call("query_kpi", 3), Call(TOOL, 4)]
    plan = plan_calls(calls)
    assert plan.run == (Call(TOOL, 2),)
    assert plan.skipped == (Call("search_documents", 1), Call("query_kpi", 3), Call(TOOL, 4))
    assert plan.drafts


def test_calls_without_repair_all_run() -> None:
    calls = [Call("search_documents"), Call("query_kpi")]
    plan = plan_calls(calls)
    assert (plan.run, plan.skipped, plan.drafts) == (tuple(calls), (), False)


# --- Действие в итоге хода ---------------------------------------------------------------


@pytest.mark.db
def test_draft_turn_stores_proposed_action_with_the_turn(database: Database, small: Path) -> None:
    def behave(context: TurnContext) -> TurnResult:
        return draft_result("h", TOOL, dict(PARAMS))

    service = make_service(database.service, database.service_url, Scripted(behave))
    reply = service.turn("u-k1", new_session(), "заявка на EX-1")
    body = reply.body
    assert body["outcome"] == "action"
    pending = body["pending_action"]
    assert isinstance(pending, dict)
    assert (pending["tool"], pending["params"]) == (TOOL, PARAMS)
    assert "подтвержд" in str(body["answer"])
    with database.owner.connect() as connection:
        row = connection.execute(
            text("select status, turn_id, user_id, params from app.actions where action_id = :a"),
            {"a": pending["action_id"]},
        ).one()
    assert tuple(row) == ("proposed", body["turn_id"], "u-k1", PARAMS)


@pytest.mark.db
def test_action_with_other_outcome_is_an_error(database: Database, small: Path) -> None:
    def behave(context: TurnContext) -> TurnResult:
        draft = draft_result("h", TOOL, dict(PARAMS))
        return TurnResult(outcome="answer", answer="ответ", prefix_hash="h", action=draft.action)

    service = make_service(database.service, database.service_url, Scripted(behave))
    reply = service.turn("u-k1", new_session(), "заявка на EX-1")
    assert (reply.body["outcome"], reply.body["pending_action"]) == ("error", None)


@pytest.mark.db
def test_unstored_turn_leaves_no_action(database: Database, small: Path) -> None:
    # Ход, переживший свою отметку, не записан — значит, и действия нет.
    session = new_session()

    def behave(context: TurnContext) -> TurnResult:
        with transaction(database.service, "u-k1") as connection:
            connection.execute(
                text(
                    "update app.sessions set capture_token = gen_random_uuid()"
                    " where user_id = 'u-k1' and session_id = :s"
                ),
                {"s": session},
            )
        return draft_result("h", TOOL, dict(PARAMS))

    service = make_service(database.service, database.service_url, Scripted(behave))
    reply = service.turn("u-k1", session, "заявка на EX-1")
    assert (reply.body["outcome"], reply.body["pending_action"]) == ("error", None)
    with database.owner.connect() as connection:
        count = connection.scalar(
            text("select count(*) from app.actions where session_id = :s"), {"s": session}
        )
    assert count == 0
