"""Бюджет хода: оценка токенов, проверка до отправки, отбрасывание изменяемой части,
потолок результата инструмента, поправка оценки в базе.

Поправка хранится по имени модели в общей тестовой базе, поэтому каждый тест берёт своё
имя: завышенный расход заглушки в одном тесте не раздувает оценки в остальных.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from assistant.access import Status
from assistant.config import CharCosts, ConfigError, load_settings
from assistant.db.session import transaction
from assistant.ingest.chunks import TOOL_RESULT_TOKENS
from assistant.model import ChatRequest, ModelReply, TransientModelError, UsageReport
from assistant.tools.box import CUT_MARK, ToolBox, ToolResult
from assistant.tools.schemas import KPI, SEARCH
from assistant.turn import agent
from assistant.turn.agent import (
    CALL_TOKENS,
    DROPPED,
    MAX_CALLS,
    TURN_TOKENS,
    AgentHandler,
    _State,
)
from assistant.turn.context import HistoryMessage
from assistant.turn.prefix import PREFIX
from assistant.turn.tokens import MAX_FACTOR, Estimator, text_tokens
from tests.conftest import Database
from tests.modelstub import ScriptedModel, call, final, tools
from tests.servicekit import (
    MODEL_KEY,
    TOIR_TOKEN,
    Service,
    make_service,
    new_session,
    settings,
)

pytestmark = pytest.mark.db
SYSTEM = PREFIX.system_message()


def _service(
    database: Database, model: ScriptedModel, name: str | None = None
) -> tuple[Service, AgentHandler, str]:
    name = name or f"budget-{uuid4().hex[:8]}"
    config = replace(settings(database.service_url, model=True).model, name=name)
    handler = AgentHandler(database.service, model, config)
    service = make_service(database.service, database.service_url, handler, model=True)
    return service, handler, name


def _with_usage(reply: ModelReply, input_tokens: int | None) -> ModelReply:
    return replace(reply, usage=UsageReport(input_tokens, 10, None))


def _estimate(request: ChatRequest) -> int:
    return Estimator(CharCosts()).request(request)


def _record(service: Service, user: str, turn_id: object) -> dict[str, Any]:
    response = service.client.get(f"/turns/{turn_id}", headers={"X-User-ID": user})
    record: dict[str, Any] = response.json()["record"]
    return record


# --- Оценка -------------------------------------------------------------------------------


def test_estimate_prices_digits_and_markup_above_letters() -> None:
    costs = CharCosts()
    assert text_tokens("1234567890", costs) > text_tokens("абвгдежзик", costs)
    assert text_tokens('{"a":[1]}', costs) > text_tokens("абвгдежзи", costs)


def test_estimate_covers_the_whole_request() -> None:
    estimator = Estimator(CharCosts())
    bare = ChatRequest(({"role": "user", "content": "вопрос"},), ())
    full = ChatRequest(
        (PREFIX.system_message(), {"role": "user", "content": "вопрос"}), PREFIX.tools
    )
    assert estimator.request(full) > estimator.request(bare) + len(PREFIX.system) // 4


def test_estimate_is_not_below_recorded_usage() -> None:
    # Образец записан пробой 2026-10-14 через ИИ-роутер: системная часть, пять схем
    # инструментов и короткий вопрос. Фактический вход — 577, 648 и 616 токенов у трёх
    # моделей; оценка с коэффициентами по умолчанию должна быть не меньше наибольшего.
    question = {"role": "user", "content": "Привет, какие у меня права?"}
    request = ChatRequest((PREFIX.system_message(), question), PREFIX.tools, "none")
    assert Estimator(CharCosts()).request(request) >= 648


def test_correction_has_a_ceiling() -> None:
    # Неправдоподобное число провайдера не делает любой вход «больше лимита» навсегда.
    estimator = Estimator(CharCosts())
    assert estimator.learn(100.0, 1_000_000)
    assert estimator.factor == MAX_FACTOR
    assert not estimator.learn(100.0, 2_000_000)


def test_correction_only_goes_up() -> None:
    estimator = Estimator(CharCosts())
    assert not estimator.learn(100.0, 50)
    assert estimator.factor == 1.0
    assert estimator.learn(100.0, 150)
    assert estimator.factor == pytest.approx(1.5)
    assert not estimator.learn(100.0, 120)
    assert estimator.factor == pytest.approx(1.5)


def test_token_costs_setting() -> None:
    env = {"DATABASE_URL": "postgresql://x", "MODEL_TOKEN_CHARS": "letters=2.5, digits=1"}
    costs = load_settings(env).model.token_costs
    assert (costs.letters, costs.digits, costs.other) == (2.5, 1.0, CharCosts().other)
    for bad in ("letters=0", "words=3", "digits=nan"):
        with pytest.raises(ConfigError):
            load_settings({**env, "MODEL_TOKEN_CHARS": bad})


# --- Проверка до отправки -----------------------------------------------------------------


def test_endless_tool_calls_stop_at_four_calls(database: Database, small: Path) -> None:
    model = ScriptedModel([], tail=tools(call(SEARCH, query="памятка")))
    service, _, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert len(model.requests) == MAX_CALLS
    assert model.requests[-1].tool_choice == "none"
    assert reply.body["outcome"] == "budget_exceeded"


def test_every_sent_call_fits_the_call_limit(database: Database, small: Path) -> None:
    # Каждый вызов, который ушёл, помещается в лимиты по оценке — с запасом против факта.
    model = ScriptedModel([], tail=tools(call(SEARCH, query="памятка регламент ремонт")))
    service, _, _ = _service(database, model)
    service.turn("u-eng", new_session(), "Вопрос")
    estimates = [_estimate(request) for request in model.requests]
    assert all(estimate <= CALL_TOKENS for estimate in estimates)


def _state(turn: list[dict[str, Any]] | None = None, spent: int = 0) -> _State:
    state = _State("[Сведения о пользователе — от системы, не вопрос]", [], "", "Вопрос")
    state.turn = turn or []
    state.spent = spent
    return state


def _handler(database: Database) -> AgentHandler:
    config = replace(settings(database.service_url, model=True).model, name="unit")
    return AgentHandler(database.service, ScriptedModel([]), config)


def test_call_number_in_turn(database: Database, small: Path) -> None:
    # Строка «Номер вызова в ходе»: четвёртый уходит, пятого нет.
    model = ScriptedModel([], tail=tools(call(SEARCH, query="памятка")))
    service, _, _ = _service(database, model)
    service.turn("u-eng", new_session(), "Вопрос")
    assert len(model.requests) == MAX_CALLS


def test_estimate_of_this_call(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    # Строка «Оценка входа этого вызова»: на границе проходит, на единицу больше — нет.
    handler = _handler(database)
    prepared = handler._prepare(_state(), 1, 30.0)
    assert prepared is not None
    estimate = prepared[1]
    monkeypatch.setattr(agent, "CALL_TOKENS", estimate)
    assert handler._prepare(_state(), 1, 30.0) is not None
    monkeypatch.setattr(agent, "CALL_TOKENS", estimate - 1)
    assert handler._prepare(_state(), 1, 30.0) is None


def test_actual_input_of_past_calls_plus_this_estimate(database: Database) -> None:
    # Строка «Фактический вход прошлых вызовов хода плюс оценка этого».
    handler = _handler(database)
    prepared = handler._prepare(_state(), 1, 30.0)
    assert prepared is not None
    # Последний вызов: формат ответа добавляет к оценке, поэтому граница — по нему.
    last = handler._prepare(_state(spent=TURN_TOKENS - prepared[1] - 500), 1, 30.0)
    assert last is not None and last[3] is True
    boundary = TURN_TOKENS - last[1]
    assert handler._prepare(_state(spent=boundary), 1, 30.0) is not None
    assert handler._prepare(_state(spent=boundary + 1), 1, 30.0) is None


def test_history_is_dropped_before_tool_results(database: Database) -> None:
    handler = _handler(database)
    history = [
        HistoryMessage("user", "Старый вопрос", (), "t-old"),
        HistoryMessage("assistant", "Старый ответ. " * 2500, (), "t-old"),
        HistoryMessage("user", "Недавний вопрос", (), "t-new"),
        HistoryMessage("assistant", "Недавний ответ", (), "t-new"),
    ]
    tool: dict[str, Any] = {"role": "tool", "tool_call_id": "a", "content": "результат"}
    state = _State("[Сведения]", history, "", "Вопрос", "t-now")
    state.turn = [{"role": "assistant", "content": "", "tool_calls": []}, tool]
    prepared = handler._prepare(state, 1, 30.0)
    assert prepared is not None
    assert state.history_from == 2
    assert tool["content"] == "результат"
    assert state.record["history_start"] == "t-new"


def test_dropped_tool_result_can_be_requested_again(database: Database) -> None:
    state = _state([{"role": "tool", "tool_call_id": "a", "content": "результат"}])
    state.calls["ключ"] = ToolResult(Status.OK, "результат")
    state.call_keys["a"] = "ключ"
    assert state.drop_tool_result()
    assert state.turn[0]["content"] == DROPPED
    assert "ключ" not in state.calls


def test_call_is_last_when_the_next_would_not_fit(database: Database) -> None:
    handler = _handler(database)
    prepared = handler._prepare(_state(), 1, 30.0)
    assert prepared is not None
    estimate = prepared[1]
    assert prepared[3] is False
    # Следующий вызов не поместился бы: потрачено + этот + (этот + потолок результата).
    spent = TURN_TOKENS - 2 * estimate - TOOL_RESULT_TOKENS + 1
    prepared = handler._prepare(_state(spent=spent), 1, 30.0)
    assert prepared is not None
    request, _, _, last = prepared
    assert last is True
    assert request.tool_choice == "none"


def test_early_tool_result_is_dropped_before_the_call_is_refused(database: Database) -> None:
    handler = _handler(database)
    assistant: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": []}
    turn: list[dict[str, Any]] = [
        assistant,
        {"role": "tool", "tool_call_id": "a", "content": "раннее " * 600},
        {"role": "tool", "tool_call_id": "b", "content": "позднее"},
    ]
    dropped = [assistant, {**turn[1], "content": DROPPED}, turn[2]]
    small_estimate = _estimate(handler._request(_state(dropped).messages(SYSTEM), True, 30.0))
    state = _state([dict(item) for item in turn], spent=TURN_TOKENS - small_estimate)
    prepared = handler._prepare(state, 1, 30.0)
    assert prepared is not None
    tool_contents = [m["content"] for m in prepared[0].messages if m["role"] == "tool"]
    # Протокол требует ответа на каждый вызов: отброшенный заменён пометкой, не удалён.
    assert tool_contents == [DROPPED, "позднее"]
    # Отбрасывать больше нечего, а не помещается — вызов не собирается.
    assert handler._prepare(_state(spent=TURN_TOKENS), 1, 30.0) is None


def test_call_that_does_not_fit_is_not_sent(database: Database, small: Path) -> None:
    model = ScriptedModel([_with_usage(tools(call(SEARCH, query="памятка")), 23_000)])
    service, _, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert len(model.requests) == 1
    assert reply.body["outcome"] == "budget_exceeded"
    record = _record(service, "u-eng", reply.body["turn_id"])
    # Перед отказом отброшен и результат инструмента этого хода — не помогло.
    assert record["dropped_tool_results"] == 1
    assert record["stopped"] == "вход не помещается в бюджет"


def test_turn_ends_with_the_last_model_text_on_the_limit(database: Database, small: Path) -> None:
    preliminary = json.dumps(
        {"outcome": "answer", "answer": "Предварительно: см. памятку", "citations": []},
        ensure_ascii=False,
    )
    reply_with_text = replace(
        _with_usage(tools(call(SEARCH, query="памятка")), 23_000), content=preliminary
    )
    model = ScriptedModel([reply_with_text])
    service, _, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert len(model.requests) == 1
    assert reply.body["answer"] == "Предварительно: см. памятку"
    assert reply.body["outcome"] != "budget_exceeded"


def test_old_history_is_dropped_first_and_not_retold(database: Database, small: Path) -> None:
    long_answer = "Длинный ответ. " * 2000
    model = ScriptedModel([final(long_answer), final("коротко"), final("снова")])
    service, _, _ = _service(database, model)
    session = new_session()
    service.turn("u-eng", session, "Первый вопрос")
    second = service.turn("u-eng", session, "Второй вопрос")
    third = service.turn("u-eng", session, "Третий вопрос")
    last_input = json.dumps(list(model.requests[-1].messages), ensure_ascii=False)
    assert "Длинный ответ." not in last_input
    assert "Первый вопрос" not in last_input
    # Ход после отброшенного остаётся, и сведения о пользователе переехали к нему.
    assert "Второй вопрос" in last_input and "коротко" in last_input
    assert "[Сведения о пользователе" in last_input
    assert _estimate(model.requests[-1]) <= CALL_TOKENS
    dropped_on = _record(service, "u-eng", second.body["turn_id"])
    assert dropped_on["dropped_history"] == 2
    # Третий ход начинает историю там же, где второй: начало входа не сдвигается
    # от хода к ходу, и отбрасывать заново не нужно.
    record = _record(service, "u-eng", third.body["turn_id"])
    assert "dropped_history" not in record
    assert record["history_start"] == dropped_on["history_start"]
    # Пересказа нет: дополнительного вызова модели не было.
    assert len(model.requests) == 3


def test_dropped_history_chunks_are_not_citable(database: Database, small: Path) -> None:
    filler = "Длинный ответ. " * 2000
    model = ScriptedModel(
        [
            tools(call(SEARCH, query="периодичность ремонта")),
            final(filler, citations=["R2#1"]),
            final("Через 700 моточасов", citations=["R2#1"]),
        ]
    )
    service, _, _ = _service(database, model)
    session = new_session()
    service.turn("u-eng", session, "Периодичность ремонта?")
    second = service.turn("u-eng", session, "А точнее?")
    assert second.body["citations"] == []


# --- Расход -------------------------------------------------------------------------------


def test_usage_is_what_the_provider_reported(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [_with_usage(tools(call(SEARCH, query="памятка")), 3000), _with_usage(final("ок"), 3500)]
    )
    service, _, _ = _service(database, model)
    usage = service.turn("u-eng", new_session(), "Вопрос").body["usage"]
    assert isinstance(usage, dict)
    assert (usage["input_tokens"], usage["max_call_input_tokens"]) == (6500, 3500)


def test_missing_usage_counts_the_estimate(database: Database, small: Path) -> None:
    model = ScriptedModel([_with_usage(final("ок"), None)])
    service, _, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    usage = reply.body["usage"]
    assert isinstance(usage, dict)
    assert usage["input_tokens"] == _estimate(model.requests[0])
    call_record = _record(service, "u-eng", reply.body["turn_id"])["calls"][0]
    assert call_record["input_estimated"] is True


def test_zero_usage_counts_the_estimate(database: Database, small: Path) -> None:
    model = ScriptedModel([_with_usage(final("ок"), 0)])
    service, _, _ = _service(database, model)
    usage = service.turn("u-eng", new_session(), "Вопрос").body["usage"]
    assert isinstance(usage, dict)
    assert usage["input_tokens"] == _estimate(model.requests[0])


def test_failed_call_counts_its_estimate_in_the_turn(
    database: Database, small: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Повтор после временной ошибки проверяется с оценкой неудавшегося вызова в бюджете:
    # бюджет хода с ней не помещает второй вызов.
    probe = ScriptedModel([final("ок")])
    probe_service, _, _ = _service(database, probe)
    probe_service.turn("u-eng", new_session(), "Вопрос")
    first = _estimate(probe.requests[0])
    monkeypatch.setattr(agent, "TURN_TOKENS", 2 * first - 1)
    model = ScriptedModel([TransientModelError("503"), final("ок")])
    service, _, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Вопрос")
    assert len(model.requests) == 1
    assert reply.body["outcome"] == "budget_exceeded"


def test_correction_is_stored_by_model_and_survives_restart(
    database: Database, small: Path
) -> None:
    model = ScriptedModel([_with_usage(final("ок"), 4000)])
    service, _, name = _service(database, model)
    service.turn("u-eng", new_session(), "Вопрос")
    first = _estimate(model.requests[0])
    with database.service.begin() as connection:
        factor = connection.scalar(
            text("select factor from app.token_calibration where model = :m"), {"m": name}
        )
    assert factor == pytest.approx(4000 / Estimator(CharCosts()).raw(model.requests[0]))

    # Новый процесс с той же моделью: оценка того же входа уже не меньше факта.
    again = ScriptedModel([_with_usage(final("ок"), 100)])
    restarted, handler, _ = _service(database, again, name)
    restarted.turn("u-eng", new_session(), "Вопрос")
    assert handler._estimator.request(again.requests[0]) >= 4000 > first
    # Меньший факт поправку не уменьшает.
    with database.service.begin() as connection:
        after = connection.scalar(
            text("select factor from app.token_calibration where model = :m"), {"m": name}
        )
    assert after == pytest.approx(factor)


# --- Результат инструмента ----------------------------------------------------------------


def _box(database: Database, user: str, limit: int) -> ToolBox:
    from assistant.access import load_grants

    with transaction(database.service, user) as connection:
        grants = load_grants(connection, user)
    assert grants is not None
    estimator = Estimator(CharCosts())
    return ToolBox(
        lambda: transaction(database.service, user),
        grants,
        "",
        count=estimator.text,
        limit=limit,
    )


def test_search_result_keeps_whole_fragments_within_the_limit(
    database: Database, small: Path
) -> None:
    box = _box(database, "u-k1", 260)
    result = box.run(SEARCH, json.dumps({"query": "ремонт памятка экскаватор диспетчер"}))
    assert result.status is Status.OK
    assert box.count(result.content) <= 260
    header = json.loads(result.content.splitlines()[0])
    shown = header["data"]["chunks"]
    assert list(result.chunk_ids) == shown
    # Не поместившиеся названы идентификаторами: продолжение — тем же путём, с правами.
    assert "Не поместились в лимит результата" in header["note"]
    assert CUT_MARK not in result.content


def test_kpi_rows_beyond_the_limit_are_counted_not_shown(database: Database, small: Path) -> None:
    box = _box(database, "u-eng", 120)
    result = box.run(KPI, json.dumps({"table": "production"}))
    assert box.count(result.content) <= 120
    header = json.loads(result.content.splitlines()[0])
    assert header["data"]["more_rows"] is True
    assert "не поместились в лимит" in header["note"]


def test_any_result_is_cut_to_the_limit_with_a_mark(database: Database, small: Path) -> None:
    box = _box(database, "u-eng", 24)
    result = box.run(SEARCH, json.dumps({"query": "x" * 400}))
    assert box.count(result.content) <= 24
    assert result.content.endswith(CUT_MARK)


def test_tool_results_in_model_input_fit_the_limit(database: Database, small: Path) -> None:
    model = ScriptedModel(
        [
            tools(
                call(SEARCH, query="ремонт памятка"),
                call(KPI, table="production"),
                call(KPI, table="downtime"),
            ),
            final("ок"),
        ]
    )
    service, handler, _ = _service(database, model)
    service.turn("u-eng", new_session(), "Вопрос")
    results = [m for m in model.requests[-1].messages if m["role"] == "tool"]
    assert len(results) == 3
    assert all(handler._estimator.text(m["content"]) <= TOOL_RESULT_TOKENS for m in results)


# --- Секреты и prefix_hash ----------------------------------------------------------------


def test_toir_token_is_absent_from_model_input_and_api_responses(
    database: Database, small: Path
) -> None:
    model = ScriptedModel([tools(call(SEARCH, query="памятка")), final("ответ")])
    service, _, _ = _service(database, model)
    reply = service.turn("u-eng", new_session(), "Что в памятке?")
    record = service.client.get(f"/turns/{reply.body['turn_id']}", headers={"X-User-ID": "u-eng"})
    info = service.client.get("/info")
    for secret in (TOIR_TOKEN, MODEL_KEY):
        assert secret not in model.inputs()
        assert secret not in reply.text
        assert secret not in record.text
        assert secret not in info.text


def test_prefix_hash_is_stable_across_budget_drops(database: Database, small: Path) -> None:
    long_answer = "Длинный ответ. " * 2000
    model = ScriptedModel([final(long_answer), final("коротко")])
    service, handler, _ = _service(database, model)
    session = new_session()
    first = service.turn("u-eng", session, "Первый")
    second = service.turn("u-eng", session, "Второй")
    assert (first.status, second.status) == (200, 200), (first.text[:300], second.text[:300])
    hashes = {first.body["usage"]["prefix_hash"], second.body["usage"]["prefix_hash"]}  # type: ignore[index]
    assert hashes == {handler.prefix_hash}
    for request in model.requests:
        assert request.messages[0] == PREFIX.system_message()
        assert request.tools == PREFIX.tools
