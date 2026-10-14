"""Цикл хода с заглушкой модели: права по записанным входам, черновик заявки, лимит вызовов,
формат ответа, история, цитаты, запись хода.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from assistant.access import Status
from assistant.model import (
    FormatRejected,
    ModelError,
    ModelReply,
    ToolCall,
    TransientModelError,
)
from assistant.tools.schemas import CALCULATE, DOCUMENT, KPI, REPAIR, SEARCH
from assistant.turn.agent import MAX_CALLS, AgentHandler
from assistant.turn.prefix import PREFIX
from tests.conftest import Database
from tests.modelstub import ScriptedModel, call, final, tools
from tests.modelstub import text as reply_text
from tests.servicekit import (
    MODEL_KEY,
    TOIR_TOKEN,
    Reply,
    Service,
    make_service,
    new_session,
    settings,
)

pytestmark = pytest.mark.db


def _service(database: Database, model: ScriptedModel) -> tuple[Service, AgentHandler]:
    handler = AgentHandler(
        database.service, model, settings(database.service_url, model=True).model
    )
    return make_service(database.service, database.service_url, handler, model=True), handler


def _usage(reply: Reply) -> dict[str, Any]:
    usage = reply.body["usage"]
    assert isinstance(usage, dict)
    return usage


def _results(model: ScriptedModel, name: str | None = None) -> list[dict[str, Any]]:
    """Результаты инструментов, как их увидела модель: заголовок конверта и блоки."""
    found = []
    for request in model.requests:
        calls = {
            item["id"]: item["function"]["name"]
            for message in request.messages
            if message["role"] == "assistant"
            for item in message.get("tool_calls", [])
        }
        for message in request.messages:
            if message["role"] == "tool" and (
                name is None or calls[message["tool_call_id"]] == name
            ):
                head, _, blocks = message["content"].partition("\n")
                found.append({**json.loads(head), "blocks": blocks})
    unique = {json.dumps(item, sort_keys=True): item for item in found}
    return list(unique.values())


# --- Права: по записанным входам модели ----------------------------------------------------


def test_closed_document_never_reaches_model_input(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(call(SEARCH, query="оклад горного мастера")),
            tools(call(DOCUMENT, ref="H1"), call(DOCUMENT, ref="H1#1")),
            final("Не могу сказать", "refusal"),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn("u-k1", new_session(), "Какой оклад у горного мастера?")
    inputs = model.inputs()
    assert "123450" not in inputs and "123 450" not in inputs
    assert "Положение об окладах" not in inputs
    statuses = [item["status"] for item in _results(model, DOCUMENT)]
    assert statuses == ["denied"]
    search = _results(model, SEARCH)[0]
    assert "закрытом" in search.get("note", "")
    assert reply.body["outcome"] == "refusal"


def test_foreign_site_rows_never_reach_model_input(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(
                call(
                    KPI, table="production", filters=[{"column": "site", "op": "eq", "value": "K2"}]
                ),
                call(KPI, table="production"),
                call(KPI, table="production", aggregates=[{"function": "sum", "column": "tonnes"}]),
            ),
            final("Добыча K1 — 210 т", "answer"),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn("u-k1", new_session(), "Сколько добыли на K2?")
    results = _results(model, KPI)
    explicit, rows, total = results
    assert (explicit["status"], "data" in explicit) == ("denied", False)
    assert {row["site"] for row in rows["data"]["rows"]} == {"K1"}
    assert total["data"]["groups"][0]["values"] == {"sum_tonnes": 210}
    # Ни одной строки и ни одного агрегата по чужому участку: 200, 210 и 220 — его тонны,
    # 210 — ещё и сумма своего, поэтому смотрим на строки, а не на числа.
    assert all(row["site"] == "K1" for row in rows["data"]["rows"])
    # Отказ по чужому и данные по своему: строка 4 — предложенный ответ остаётся ответом.
    assert reply.body["outcome"] == "answer"


def test_secret_never_reaches_model_input(database: Database, small: Path) -> None:
    model = ScriptedModel([tools(call(SEARCH, query="памятка")), final("ответ")])
    service, _ = _service(database, model)
    service.turn("u-eng", new_session(), "Что в памятке?")
    assert TOIR_TOKEN not in model.inputs()
    assert MODEL_KEY not in model.inputs()


# --- Заявка --------------------------------------------------------------------------------


def test_draft_ends_turn_without_next_model_call(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(
                call(SEARCH, query="порядок заявок"),
                call(REPAIR, equipment_id="ех 1", description="течь", priority=2),
            ),
            final("модель хотела бы продолжить"),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn(
        "u-k1", new_session(), "Оформи заявку на ЕХ-1: течь, работает с ограничениями"
    )
    body = reply.body
    assert body["outcome"] == "action"
    assert len(model.requests) == 1
    pending = body["pending_action"]
    assert isinstance(pending, dict)
    # Написание из набора, а не модели или пользователя; поиск из того же ответа не выполнен.
    assert pending["params"] == {"equipment_id": "EX-1", "description": "течь", "priority": 2}
    assert _usage(reply)["model_calls"] == 1


def test_instruction_from_document_does_not_create_action(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(call(REPAIR, equipment_id="EX-1", description="по памятке", priority=1)),
            final("В памятке сказано проверять ёмкость."),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn("u-k1", new_session(), "Перескажи памятку")
    assert reply.body["outcome"] == "answer"
    assert reply.body["pending_action"] is None
    assert _results(model, REPAIR)[0]["status"] == "needs_identifier"


def test_foreign_equipment_draft_is_refused(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(call(REPAIR, equipment_id="EX-2", description="течь", priority=2)),
            final("Готово", "answer"),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn("u-k1", new_session(), "Оформи заявку на EX-2")
    assert (reply.body["outcome"], reply.body["pending_action"]) == ("refusal", None)
    with database.owner.connect() as connection:
        count = connection.scalar(
            text("select count(*) from app.actions where turn_id = :t"),
            {"t": reply.body["turn_id"]},
        )
    assert count == 0


def test_other_calls_in_a_repair_reply_are_not_executed(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(
                call(SEARCH, query="памятка"),
                call(REPAIR, equipment_id="EX-2", description="течь", priority=2),
            ),
            final("Нельзя", "refusal"),
        ]
    )
    service, _ = _service(database, model)
    service.turn("u-k1", new_session(), "Найди памятку и оформи заявку на EX-2")
    second = model.requests[1].messages
    results = [message["content"] for message in second if message["role"] == "tool"]
    assert len(results) == 2
    assert any("не выполнен" in item for item in results)
    assert not any("<<<фрагмент" in item for item in results)


# --- Лимит вызовов и формат ----------------------------------------------------------------


def test_endless_tool_calls_stop_at_the_limit(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [], tail=lambda request: tools(call(SEARCH, query=f"шаг {len(model.requests)}"))
    )
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Ищи")
    assert len(model.requests) == MAX_CALLS
    assert _usage(reply)["model_calls"] == MAX_CALLS
    assert reply.body["outcome"] == "budget_exceeded"
    # Последний вызов — с запретом инструментов; схемы остаются во входе.
    last = model.requests[-1]
    assert (last.tool_choice, last.tools) == ("none", PREFIX.tools)
    assert all(request.tool_choice == "auto" for request in model.requests[:-1])
    # Принуждение к формату по умолчанию — только на последнем вызове.
    assert [request.response_format is not None for request in model.requests] == [
        False,
        False,
        False,
        True,
    ]


def test_tool_call_after_ban_is_not_executed(database: Database, small: Path) -> None:
    script = [tools(call(SEARCH, query=f"q{n}")) for n in range(MAX_CALLS - 1)]
    banned = ModelReply(
        '{"outcome": "answer", "answer": "Итог по найденному", "citations": []}',
        (ToolCall("x", KPI, '{"table": "production"}'),),
    )
    model = ScriptedModel([*script, banned])
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Ищи")
    assert reply.body["answer"] == "Итог по найденному"
    assert _results(model, KPI) == []


def test_format_rejection_retries_without_format_and_is_not_counted(
    database: Database, small: Path
) -> None:
    model = ScriptedModel(
        [
            tools(call(SEARCH, query="a")),
            tools(call(SEARCH, query="b")),
            tools(call(SEARCH, query="c")),
            FormatRejected("response_format"),
            final("ответ"),
            tools(call(SEARCH, query="d")),
            tools(call(SEARCH, query="e")),
            tools(call(SEARCH, query="f")),
            final("второй"),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert reply.body["answer"] == "ответ"
    assert _usage(reply)["model_calls"] == MAX_CALLS
    assert model.requests[3].response_format is not None
    assert model.requests[4].response_format is None
    assert (model.requests[3].tool_choice, model.requests[4].tool_choice) == ("none", None)
    # Повтор прошёл — строгие параметры выключены для следующих ходов.
    service.turn("u-eng", new_session(), "Вопрос")
    assert all(request.response_format is None for request in model.requests[5:])
    assert model.requests[-1].tool_choice is None


def test_rejection_for_another_reason_keeps_strict_parameters(
    database: Database, small: Path
) -> None:
    # Отказ не из-за строгих параметров: повтор без них тоже отвергнут.
    model = ScriptedModel(
        [
            tools(call(SEARCH, query="a")),
            tools(call(SEARCH, query="b")),
            tools(call(SEARCH, query="c")),
            FormatRejected("BadRequestError"),
            ModelError("сервер модели отклонил запрос: 400"),
            tools(call(SEARCH, query="d")),
            tools(call(SEARCH, query="e")),
            tools(call(SEARCH, query="f")),
            final("второй"),
        ]
    )
    service, _ = _service(database, model)
    assert service.turn("u-eng", new_session(), "Вопрос").body["outcome"] == "error"
    second = service.turn("u-eng", new_session(), "Вопрос")
    assert second.body["answer"] == "второй"
    assert model.requests[-1].response_format is not None
    assert model.requests[-1].tool_choice == "none"


def test_truncated_reply_is_not_a_final_answer(database: Database, small: Path) -> None:
    cut = ModelReply('{"outcome": "answer", "answer": "Через 7', (), truncated=True)
    model = ScriptedModel([cut])
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert reply.body["outcome"] == "error"
    assert "Через 7" not in str(reply.body["answer"])


def test_call_timeout_is_bounded_by_the_turn_deadline(database: Database, small: Path) -> None:
    holder: list[Service] = []

    def slow(request: Any) -> ModelReply:
        holder[0].clock.advance(80)
        return tools(call(SEARCH, query="a"))

    model = ScriptedModel([slow, final("ответ")])
    service, _ = _service(database, model)
    holder.append(service)
    service.turn("u-eng", new_session(), "Вопрос")
    first, second = (request.timeout for request in model.requests)
    # Предел хода — 90 с: после 80 с на второй вызов остаётся не больше 10.
    assert first is not None and second is not None
    assert second <= 10 < first


def test_transient_error_is_retried_once_and_counted(database: Database, small: Path) -> None:
    model = ScriptedModel([TransientModelError("503"), final("ответ")])
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert reply.body["answer"] == "ответ"
    assert _usage(reply)["model_calls"] == 2


def test_repeated_transient_error_is_outcome_error(database: Database, small: Path) -> None:
    model = ScriptedModel([TransientModelError("503"), TransientModelError("503")])
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert reply.body["outcome"] == "error"
    assert _usage(reply)["model_calls"] == 2


def test_invalid_arguments_become_a_tool_result(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(
                ToolCall("bad", KPI, '{"table": "salaries"}'), ToolCall("raw", SEARCH, "не json")
            ),
            final("ответ"),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert reply.status == 200 and reply.body["outcome"] == "answer"
    statuses = [item["status"] for item in _results(model)]
    assert statuses == ["invalid", "invalid"]


def test_repeated_call_is_not_executed_again(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [tools(call(SEARCH, query="памятка")), tools(call(SEARCH, query="памятка")), final("ок")]
    )
    service, _ = _service(database, model)
    service.turn("u-eng", new_session(), "Вопрос")
    last = model.requests[-1].messages[-1]["content"]
    assert "уже есть выше" in last


# --- Итог, цитаты, история -----------------------------------------------------------------


def test_citations_only_from_input_and_outcome_from_table(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(call(SEARCH, query="периодичность ремонта")),
            final("Ремонт через каждые 700 моточасов", "answer", ["R2#1", "H1#1", "Z9#1"]),
        ]
    )
    service, _ = _service(database, model)
    reply = service.turn("u-k1", new_session(), "Периодичность ремонта?")
    assert reply.body["citations"] == [{"doc_id": "R2", "chunk_id": "R2#1"}]
    # Утратившая силу редакция в обычной выдаче не появляется.
    assert "500 моточасов" not in model.inputs()
    note = _results(model, SEARCH)[0].get("note", "")
    assert "прежняя редакция" in note


def test_history_is_visible_in_the_next_turn(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(call(SEARCH, query="периодичность ремонта")),
            final("Через 700 моточасов [R2#1]"),
            final("Да, по той же редакции", "answer", ["R2#1"]),
        ]
    )
    service, _ = _service(database, model)
    session = new_session()
    service.turn("u-k1", session, "Периодичность ремонта?")
    second = service.turn("u-k1", session, "А это действующая редакция?")
    messages = model.requests[-1].messages
    contents = [message.get("content", "") for message in messages]
    assert any("Периодичность ремонта?" in item for item in contents)
    assert any("Через 700 моточасов" in item for item in contents)
    # Результатов инструментов прошлого хода в истории нет; цитата из истории принимается.
    assert not any(message["role"] == "tool" for message in messages)
    assert second.body["citations"] == [{"doc_id": "R2", "chunk_id": "R2#1"}]
    # Сведения о пользователе — заголовок первого вопроса сессии: начало входа не меняется.
    first_turn = model.requests[0].messages[1]["content"]
    assert messages[1]["content"] == first_turn


def test_text_answer_and_thinking(database: Database, small: Path) -> None:
    model = ScriptedModel([reply_text("<think>прикину</think>Ответа нет в документах.")])
    service, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert reply.body["answer"] == "Ответа нет в документах."
    # Исход не назван, инструментов не было: строка 8 — данных нет.
    assert reply.body["outcome"] == "no_data"


def test_prefix_hash_is_the_same_in_every_call_and_turn(database: Database, small: Path) -> None:
    model = ScriptedModel([tools(call(SEARCH, query="a")), final("1"), final("2")])
    service, _ = _service(database, model)
    session = new_session()
    first = service.turn("u-eng", session, "Вопрос")
    second = service.turn("u-eng", session, "Ещё")
    assert _usage(first)["prefix_hash"] == _usage(second)["prefix_hash"] == PREFIX.hash
    assert all(request.messages[0]["content"] == PREFIX.system for request in model.requests)
    assert all(request.tools == PREFIX.tools for request in model.requests)


# --- Запись хода и сведения о сборке -------------------------------------------------------


def test_turn_record_is_visible_to_its_author_only(database: Database, small: Path) -> None:
    model = ScriptedModel([tools(call(SEARCH, query="памятка")), final("ок")])
    service, _ = _service(database, model)
    turn_id = service.turn("u-k1", new_session(), "Что в памятке?").body["turn_id"]
    own = service.client.get(f"/turns/{turn_id}", headers={"X-User-ID": "u-k1"})
    foreign = service.client.get(f"/turns/{turn_id}", headers={"X-User-ID": "u-eng"})
    assert (own.status_code, foreign.status_code) == (200, 404)
    record = own.json()["record"]
    first = record["calls"][0]["tool_calls"][0]
    assert (first["name"], first["status"]) == (SEARCH, "ok")
    assert "<<<фрагмент" in first["result"]
    assert record["parse_path"] == "json"


def test_info(database: Database, small: Path) -> None:
    model = ScriptedModel([])
    service, _ = _service(database, model)
    info = service.client.get("/info").json()
    assert info["prefix_hash"] == PREFIX.hash
    assert info["model"] == "test-model"
    assert info["dataset_hash"]


# --- Инструменты: расчёт и КХД -------------------------------------------------------------


def _tool(database: Database, user: str, name: str, **arguments: Any) -> dict[str, Any]:
    model = ScriptedModel([tools(call(name, **arguments)), final("ок")])
    service, _ = _service(database, model)
    service.turn(user, new_session(), "Вопрос")
    return _results(model, name)[0]


@pytest.mark.parametrize(
    ("operation", "numbers", "decimals", "expected"),
    [
        ("percent", [7, 9], 1, "77.8"),
        ("ratio", [1, 3], 2, "0.33"),
        ("difference", [10, 2.5], 1, "7.5"),
        ("sum", [1, 2, 3], 0, 6),
        ("percent", [1, 8], 1, "12.5"),
    ],
)
def test_calculate(
    database: Database,
    small: Path,
    operation: str,
    numbers: list[float],
    decimals: int,
    expected: Any,
) -> None:
    result = _tool(
        database, "u-eng", CALCULATE, operation=operation, numbers=numbers, decimals=decimals
    )
    assert result["data"]["result"] == expected


def test_calculate_refuses_bad_input(database: Database, small: Path) -> None:
    assert (
        _tool(database, "u-eng", CALCULATE, operation="ratio", numbers=[1, 0])["status"]
        == "invalid"
    )
    assert (
        _tool(database, "u-eng", CALCULATE, operation="percent", numbers=[1])["status"] == "invalid"
    )
    # 0/0 и результат вне точности расчёта — тоже `invalid`, а не обрыв хода.
    for numbers, decimals in (([0, 0], 1), ([1e30, 1e-30], 6)):
        result = _tool(
            database, "u-eng", CALCULATE, operation="ratio", numbers=numbers, decimals=decimals
        )
        assert result["status"] == "invalid"


def test_downtime_crossing_midnight_is_split_and_clipped(database: Database, small: Path) -> None:
    # EX-1: 2026-01-01T22:00 — 2026-01-02T03:30, ремонт.
    by_day = _tool(
        database,
        "u-k1",
        KPI,
        table="downtime",
        filters=[{"column": "equipment_id", "op": "eq", "value": "EX-1"}],
        period={"date_from": "2026-01-01", "date_to": "2026-01-02"},
        group_by=["date"],
        aggregates=[{"function": "sum", "column": "duration_hours"}],
    )
    groups = {
        item["group"]["date"]: item["values"]["sum_duration_hours"]
        for item in by_day["data"]["groups"]
    }
    assert groups == {"2026-01-01": 2, "2026-01-02": "3.5"}
    clipped = _tool(
        database,
        "u-k1",
        KPI,
        table="downtime",
        filters=[{"column": "equipment_id", "op": "eq", "value": "EX-1"}],
        period={"date_from": "2026-01-01", "date_to": "2026-01-01"},
        aggregates=[{"function": "sum", "column": "duration_hours"}],
    )
    assert clipped["data"]["groups"][0]["values"]["sum_duration_hours"] == 2
    assert "внутри периода" in clipped["note"]


def test_kpi_derived_columns_and_average(database: Database, small: Path) -> None:
    since = _tool(
        database,
        "u-eng",
        KPI,
        table="equipment",
        aggregates=[{"function": "max", "column": "hours_since_ppr"}],
    )
    assert since["data"]["groups"][0]["values"]["max_hours_since_ppr"] == 500
    average = _tool(
        database,
        "u-eng",
        KPI,
        table="production",
        group_by=["site"],
        aggregates=[{"function": "avg", "column": "tonnes"}, {"function": "count"}],
    )
    values = {item["group"]["site"]: item["values"] for item in average["data"]["groups"]}
    assert values == {"K1": {"avg_tonnes": 105, "count": 2}, "K2": {"avg_tonnes": 210, "count": 3}}


def test_kpi_empty_and_invalid(database: Database, small: Path) -> None:
    empty = _tool(
        database,
        "u-k1",
        KPI,
        table="production",
        period={"date_from": "2025-01-01", "date_to": "2025-01-02"},
    )
    assert empty["status"] == Status.EMPTY.value
    bad = _tool(database, "u-k1", KPI, table="production", group_by=["salary"])
    assert bad["status"] == Status.INVALID.value


def test_unknown_equipment_draft_lists_own_equipment(database: Database, small: Path) -> None:
    result = _tool(database, "u-k1", REPAIR, equipment_id="EX-9", description="течь", priority=2)
    # Сообщение пользователя в _tool — «Вопрос»: идентификатора в нём нет.
    assert result["status"] == "needs_identifier"
    model = ScriptedModel(
        [tools(call(REPAIR, equipment_id="EX-9", description="течь", priority=2)), final("ок")]
    )
    service, _ = _service(database, model)
    service.turn("u-k1", new_session(), "Оформи заявку на EX-9")
    note = _results(model, REPAIR)[0]["note"]
    # Своё оборудование — в списке, чужое — нет.
    assert "EX-1" in note and "T-1" in note and "EX-2" not in note
