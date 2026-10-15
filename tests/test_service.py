"""Каркас сервиса целиком: API в процессе тестов над настоящей базой.

Пользователь из заголовка, контракт ответа, захват сессии, предел времени, ошибки.
"""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from assistant.api.app import create_app
from assistant.config import ConfigError
from assistant.db.grants import grant_service
from assistant.db.migrate import upgrade
from assistant.db.session import transaction
from assistant.turn.capture import capture
from assistant.turn.runner import (
    INTERNAL_ERROR,
    LOOP_NOT_READY,
    LOST_CAPTURE_ERROR,
    MODEL_NOT_CONFIGURED,
    TIMEOUT_ERROR,
    CitationRef,
    StoredMessage,
    TurnContext,
    TurnResult,
    UsageMeter,
    empty_prefix_hash,
)
from tests.conftest import PROBE, SERVICE_PASSWORD, Database
from tests.servicekit import (
    MODEL_KEY,
    TOIR_TOKEN,
    Scripted,
    Service,
    make_service,
    new_session,
    with_handler,
)

pytestmark = pytest.mark.db

TURN_KEYS = {"turn_id", "outcome", "answer", "citations", "pending_action", "usage"}
USAGE_KEYS = {
    "model_calls",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "max_call_input_tokens",
    "prefix_hash",
    "latency_ms",
}


@pytest.fixture
def service(database: Database, small: Path) -> Service:
    return make_service(database.service, database.service_url)


def _session(database: Database, user: str, session: str) -> tuple[object, object] | None:
    with database.owner.connect() as connection:
        row = connection.execute(
            text(
                "select busy_until, capture_token from app.sessions"
                " where user_id = :u and session_id = :s"
            ),
            {"u": user, "s": session},
        ).first()
    return None if row is None else (row[0], row[1])


def _turns(database: Database, session: str) -> list[tuple[str, str, str]]:
    with database.owner.connect() as connection:
        rows = connection.execute(
            text(
                "select user_id, outcome, answer from app.turns where session_id = :s"
                " order by created_at, turn_id"
            ),
            {"s": session},
        ).all()
    return [(row[0], row[1], row[2]) for row in rows]


# --- пользователь из заголовка


def test_no_header_is_401_not_422_and_no_turn_starts(service: Service, database: Database) -> None:
    session = new_session()
    reply = service.turn(None, session)
    assert reply.status == 401
    # Хода от имени «по умолчанию» нет — ни от чьего: сессия не появилась ни у кого.
    with database.owner.connect() as connection:
        created = connection.scalar(
            text("select count(*) from app.sessions where session_id = :s"), {"s": session}
        )
    assert created == 0
    for path in ("/actions/a-1/confirm", "/actions/a-1/reject"):
        assert service.client.post(path).status_code == 401


@pytest.mark.parametrize("user", ["u-nobody", "U-ENG", " u-eng", "u-eng' or '1'='1"])
def test_unknown_user_is_401_not_a_default_user(
    service: Service, database: Database, user: str
) -> None:
    session = new_session()
    reply = service.turn(user, session)
    assert reply.status == 401
    assert _turns(database, session) == []
    assert (
        service.client.post("/actions/a-1/confirm", headers={"X-User-ID": user}).status_code == 401
    )


# --- контракт ответа


def test_turn_response_matches_contract(service: Service, database: Database) -> None:
    session = new_session()
    reply = service.turn("u-eng", session)
    assert reply.status == 200
    assert set(reply.body) == TURN_KEYS
    usage = reply.body["usage"]
    assert isinstance(usage, dict) and set(usage) == USAGE_KEYS
    assert reply.body["citations"] == []
    assert reply.body["pending_action"] is None
    assert usage["cached_input_tokens"] is None
    assert usage["prefix_hash"] == empty_prefix_hash()
    # Без переменных модели сервис стартует, а ход завершается с error и понятной причиной.
    assert reply.body["outcome"] == "error"
    assert reply.body["answer"] == MODEL_NOT_CONFIGURED
    assert _turns(database, session) == [("u-eng", "error", MODEL_NOT_CONFIGURED)]
    assert _session(database, "u-eng", session) == (None, None)


def test_configured_model_without_loop_says_so(database: Database, small: Path) -> None:
    service = make_service(database.service, database.service_url, model=True)
    reply = service.turn("u-eng", new_session())
    assert (reply.body["outcome"], reply.body["answer"]) == ("error", LOOP_NOT_READY)
    assert MODEL_KEY not in reply.text and TOIR_TOKEN not in reply.text


def test_successful_turn_is_returned_and_stored(database: Database, small: Path) -> None:
    def behave(context: TurnContext) -> TurnResult:
        # Два вызова модели: расход записывается сразу после каждого.
        context.usage.record(input_tokens=3480, output_tokens=110, cached=2048)
        context.usage.record(input_tokens=2640, output_tokens=100, cached=0)
        return TurnResult(
            outcome="answer",
            answer="8 часов",
            prefix_hash="hash-1",
            citations=(CitationRef("A1", "A1#1"),),
            record={"calls": 2},
            messages=(
                StoredMessage("user", {"text": context.message}),
                StoredMessage("assistant", {"text": "8 часов"}, ("A1#1",)),
            ),
        )

    service = make_service(database.service, database.service_url, Scripted(behave))
    session = new_session()
    reply = service.turn("u-eng", session, "какой срок реакции?")
    assert reply.status == 200
    assert reply.body["outcome"] == "answer"
    assert reply.body["citations"] == [{"doc_id": "A1", "chunk_id": "A1#1"}]
    assert reply.body["usage"] == {
        "model_calls": 2,
        "input_tokens": 6120,
        "cached_input_tokens": 2048,
        "output_tokens": 210,
        "max_call_input_tokens": 3480,
        "prefix_hash": "hash-1",
        "latency_ms": 0,
    }
    with transaction(database.service, "u-eng") as connection:
        stored = connection.execute(
            text(
                "select question, citations, input_tokens, record from app.turns where turn_id = :t"
            ),
            {"t": reply.body["turn_id"]},
        ).one()
        roles = connection.scalars(
            text("select role from app.messages where turn_id = :t order by seq"),
            {"t": reply.body["turn_id"]},
        ).all()
    assert stored == (
        "какой срок реакции?",
        [{"doc_id": "A1", "chunk_id": "A1#1"}],
        6120,
        {"calls": 2},
    )
    assert roles == ["user", "assistant"]
    with transaction(database.service, "u-k1") as connection:
        # История принадлежит автору: под другим пользователем её не видно.
        hidden = connection.scalar(
            text("select count(*) from app.turns where session_id = :s"), {"s": session}
        )
    assert hidden == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"session_id": "s"},
        {"message": "м"},
        {"session_id": "", "message": "м"},
        {"session_id": "s", "message": ""},
        {"session_id": "s", "message": "м" * 201},
        {"session_id": "s" * 201, "message": "м"},
    ],
)
def test_bad_body_is_422_and_nothing_happens(
    service: Service, database: Database, body: dict[str, str]
) -> None:
    response = service.client.post("/turn", json=body, headers={"X-User-ID": "u-eng"})
    assert response.status_code == 422
    assert _session(database, "u-eng", "s") is None


def test_user_is_checked_before_body(service: Service) -> None:
    # Без заголовка и для неизвестного пользователя — 401, каким бы ни было тело.
    for headers in ({}, {"X-User-ID": "u-nobody"}):
        for body in ({}, {"session_id": "s", "message": "м" * 201}, {"лишнее": 1}):
            response = service.client.post("/turn", json=body, headers=headers)
            assert response.status_code == 401, (headers, body)


def test_extra_fields_in_body_are_ignored(service: Service) -> None:
    # Клиент может слать служебные поля; пользователя из тела сервис не берёт.
    response = service.client.post(
        "/turn",
        json={"session_id": new_session(), "message": "м", "user_id": "u-hr", "trace": {"a": 1}},
        headers={"X-User-ID": "u-eng"},
    )
    assert response.status_code == 200
    assert set(response.json()) == TURN_KEYS


def test_unparseable_body_is_rejected_without_touching_the_database(
    service: Service, database: Database
) -> None:
    for headers in ({}, {"X-User-ID": "u-eng"}):
        response = service.client.post(
            "/turn",
            content=b'{"session_id":',
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert "Traceback" not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        b'{"session_id": "s", "message": "abc\\u0000"}',
        b'{"session_id": "s\\u0000", "message": "abc"}',
        # Половинка суррогатной пары: в UTF-8 не кодируется, в базу не записывается.
        b'{"session_id": "s", "message": "abc\\ud800"}',
    ],
)
def test_text_the_database_cannot_store_is_422_before_the_turn(
    service: Service, database: Database, raw: bytes
) -> None:
    response = service.client.post(
        "/turn", content=raw, headers={"X-User-ID": "u-eng", "Content-Type": "application/json"}
    )
    assert response.status_code == 422
    # Ответ называет поле и причину, но не возвращает сам ввод.
    detail = response.json()["detail"]
    assert detail and all(set(item) == {"loc", "msg", "type"} for item in detail)
    assert "abc" not in response.text
    assert _session(database, "u-eng", "s") is None


def test_usage_meter_sums_calls_and_keeps_unknown_cache_as_null() -> None:
    meter = UsageMeter()
    assert meter.totals.model_calls == 0 and meter.totals.cached_input_tokens is None
    meter.record(input_tokens=500, output_tokens=20)
    # Провайдер не отдаёт кэшированный вход — остаётся null, а не ноль.
    assert meter.totals.cached_input_tokens is None
    meter.record(input_tokens=800, output_tokens=30, cached=300)
    meter.record(input_tokens=600, output_tokens=10)
    totals = meter.totals
    assert (totals.model_calls, totals.input_tokens, totals.output_tokens) == (3, 1900, 60)
    assert (totals.cached_input_tokens, totals.max_call_input_tokens) == (300, 800)


# --- захват сессии


def test_second_turn_in_same_session_is_409(database: Database, small: Path) -> None:
    session = new_session()
    seen: dict[str, int] = {}

    handler = Scripted()
    service = make_service(database.service, database.service_url, handler)

    def behave(context: TurnContext) -> TurnResult:
        if len(handler.seen) > 1:
            return TurnResult(outcome="answer", answer="вложенный", prefix_hash="h")
        # Пока идёт этот ход, приходит второй запрос в ту же сессию.
        seen["same"] = service.turn("u-eng", session).status
        # Тот же session_id у другого пользователя — другая сессия: отказа нет.
        seen["other"] = service.turn("u-k1", session).status
        return TurnResult(outcome="answer", answer="первый", prefix_hash="h")

    handler.behave = behave
    reply = service.turn("u-eng", session)
    assert reply.status == 200 and reply.body["answer"] == "первый"
    assert seen == {"same": 409, "other": 200}
    assert sorted(_turns(database, session)) == [
        ("u-eng", "answer", "первый"),
        ("u-k1", "answer", "вложенный"),
    ]
    # Ход завершён — сессия свободна: следующий запрос проходит.
    assert service.turn("u-eng", session).status == 200


def test_crashed_turn_blocks_session_only_until_lease_expires(
    service: Service, database: Database
) -> None:
    session = new_session()
    lease = timedelta(seconds=service.runtime.settings.session_lease_seconds)
    with transaction(database.service, "u-eng") as connection:
        # Процесс упал посреди хода: отметка осталась, снять её некому.
        assert capture(connection, "u-eng", session, uuid4(), service.clock.now, lease)
    assert service.turn("u-eng", session).status == 409
    service.clock.advance(lease.total_seconds() - 1)
    assert service.turn("u-eng", session).status == 409
    service.clock.advance(1)
    # Срок истёк сам: сессия не заблокирована навсегда.
    assert service.turn("u-eng", session).status == 200
    assert _session(database, "u-eng", session) == (None, None)


def test_error_inside_turn_is_outcome_error_and_releases_at_once(
    database: Database, small: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def behave(context: TurnContext) -> TurnResult:
        # Два вызова модели уже сделаны и оплачены, когда инструмент падает.
        context.usage.record(input_tokens=900, output_tokens=40)
        context.usage.record(input_tokens=1200, output_tokens=60)
        raise RuntimeError("секретная подробность сбоя")

    service = make_service(database.service, database.service_url, Scripted(behave))
    session = new_session()
    reply = service.turn("u-eng", session)
    # Не 500 и не трассировка: ответ по контракту.
    assert reply.status == 200
    assert set(reply.body) == TURN_KEYS
    assert (reply.body["outcome"], reply.body["answer"]) == ("error", INTERNAL_ERROR)
    assert "секретная подробность" not in reply.text and "Traceback" not in reply.text
    # Подробности — в журнале сервиса.
    assert "секретная подробность" in caplog.text
    assert _turns(database, session) == [("u-eng", "error", INTERNAL_ERROR)]
    # Расход уже сделанных вызовов сохраняется и в ответе, и в записи хода.
    usage = reply.body["usage"]
    assert isinstance(usage, dict)
    assert (usage["model_calls"], usage["input_tokens"], usage["output_tokens"]) == (2, 2100, 100)
    assert usage["max_call_input_tokens"] == 1200 and usage["cached_input_tokens"] is None
    with database.owner.connect() as connection:
        stored = connection.execute(
            text("select model_calls, input_tokens from app.turns where turn_id = :t"),
            {"t": reply.body["turn_id"]},
        ).one()
    assert stored == (2, 2100)
    # Отметка снята сразу, а не по сроку: часы не переводились.
    assert _session(database, "u-eng", session) == (None, None)
    assert service.turn("u-eng", session).status == 200


def test_turn_that_outlived_its_lease_writes_nothing(database: Database, small: Path) -> None:
    session = new_session()
    other = uuid4()
    handler = Scripted()
    service = make_service(database.service, database.service_url, handler)

    def behave(context: TurnContext) -> TurnResult:
        lease = timedelta(seconds=service.runtime.settings.session_lease_seconds)
        service.clock.now += lease + timedelta(seconds=1)
        # Срок истёк, и сессию занял другой ход.
        with transaction(database.service, "u-eng") as connection:
            assert capture(connection, "u-eng", session, other, service.clock.now, lease)
        return TurnResult(outcome="answer", answer="запоздалый ответ", prefix_hash="h")

    handler.behave = behave
    reply = service.turn("u-eng", session)
    # Ответ, который не записан, клиенту не отдаётся.
    assert reply.status == 200
    assert (reply.body["outcome"], reply.body["answer"]) == ("error", LOST_CAPTURE_ERROR)
    assert "запоздалый ответ" not in reply.text
    assert _turns(database, session) == []
    # Чужая отметка цела: метка захвата проверена в завершающей транзакции.
    held = _session(database, "u-eng", session)
    assert held is not None and held[1] == other


def test_failed_write_is_an_internal_error_not_a_lost_capture(
    database: Database, small: Path
) -> None:
    def behave(context: TurnContext) -> TurnResult:
        context.usage.record(input_tokens=700, output_tokens=30)
        # Исход, которого база не примет: запись хода падает на ограничении.
        return TurnResult(outcome="выдуманный", answer="ответ вовремя", prefix_hash="h")

    service = make_service(database.service, database.service_url, Scripted(behave))
    session = new_session()
    reply = service.turn("u-eng", session)
    assert reply.status == 200
    # Ход уложился в срок: клиенту не говорят, что он опоздал.
    assert (reply.body["outcome"], reply.body["answer"]) == ("error", INTERNAL_ERROR)
    assert "ответ вовремя" not in reply.text
    assert _turns(database, session) == []
    # Отметка снята отдельной транзакцией: сессия не ждёт истечения срока.
    assert _session(database, "u-eng", session) == (None, None)
    assert service.turn("u-eng", session).status == 200


def test_turn_has_an_overall_time_limit(database: Database, small: Path) -> None:
    handler = Scripted()
    service = make_service(database.service, database.service_url, handler)

    def behave(context: TurnContext) -> TurnResult:
        assert context.deadline.remaining() == service.runtime.settings.turn_timeout_seconds
        service.clock.mono += service.runtime.settings.turn_timeout_seconds
        assert context.deadline.expired()
        for _ in range(3):
            context.usage.record(input_tokens=100, output_tokens=10)
        return TurnResult(outcome="answer", answer="поздно", prefix_hash="h")

    handler.behave = behave
    session = new_session()
    reply = service.turn("u-eng", session)
    assert (reply.body["outcome"], reply.body["answer"]) == ("error", TIMEOUT_ERROR)
    usage = reply.body["usage"]
    # Расход уже сделанных вызовов сохраняется.
    assert isinstance(usage, dict) and usage["model_calls"] == 3
    assert usage["latency_ms"] == service.runtime.settings.turn_timeout_seconds * 1000
    assert _turns(database, session) == [("u-eng", "error", TIMEOUT_ERROR)]
    assert _session(database, "u-eng", session) == (None, None)


def test_database_failure_before_turn_is_outcome_error(database: Database, small: Path) -> None:
    # База недоступна уже при проверке пользователя: всё равно ответ по контракту.
    broken = create_engine(
        database.url("test_app", SERVICE_PASSWORD, "нет-такой-базы"),
        connect_args={"connect_timeout": 2},
    )
    try:
        service = make_service(broken, database.service_url)
        reply = service.turn("u-eng", new_session())
        assert reply.status == 200
        assert set(reply.body) == TURN_KEYS
        assert (reply.body["outcome"], reply.body["answer"]) == ("error", INTERNAL_ERROR)
        assert SERVICE_PASSWORD not in reply.text and "Traceback" not in reply.text
        assert service.client.get("/healthz").status_code == 503
    finally:
        broken.dispose()


def test_unhandled_error_elsewhere_is_500_without_traceback(database: Database) -> None:
    broken = create_engine(
        database.url("test_app", SERVICE_PASSWORD, "нет-такой-базы"),
        connect_args={"connect_timeout": 2},
    )
    try:
        service = make_service(broken, database.service_url, raise_server_exceptions=False)
        response = service.client.post("/actions/a-1/confirm", headers={"X-User-ID": "u-eng"})
        assert response.status_code == 500
        assert response.json() == {"detail": "внутренняя ошибка сервиса"}
        assert SERVICE_PASSWORD not in response.text and "Traceback" not in response.text
    finally:
        broken.dispose()


# --- привязка к пользователю


def test_every_transaction_is_bound_to_the_user(database: Database, small: Path) -> None:
    with transaction(database.service, "u-k1") as connection:
        assert connection.scalar(text("select current_setting('app.user_id', true)")) == "u-k1"
        assert connection.scalar(text("select count(*) from dwh.production")) == 2
    with database.service.connect() as connection:
        # Вне такой транзакции привязки нет, и строк не видно.
        assert connection.scalar(text("select count(*) from dwh.production")) == 0


# --- действия: каркас маршрутов


# --- готовность и старт процесса


def test_readiness_follows_schema_and_dataset(database: Database, empty: Engine) -> None:
    engine = create_engine(database.url("test_app", SERVICE_PASSWORD, PROBE))
    try:
        client = make_service(engine, database.service_url).client
        # Схемы нет вовсе.
        assert client.get("/healthz").status_code == 503
        with empty.begin() as connection:
            upgrade(connection)
            grant_service(connection, database.accounts.service.role)
        # Схема есть, набор не загружен: сервису нечем отвечать.
        assert client.get("/healthz").json() == {"status": "набор не загружен"}
        with empty.begin() as connection:
            connection.execute(text("insert into app.dataset_version (content_hash) values ('x')"))
        assert client.get("/healthz").json() == {"status": "ok"}
        with empty.begin() as connection:
            connection.execute(text("update alembic_version set version_num = '9999_future'"))
        response = client.get("/healthz")
        assert (response.status_code, response.json()) == (503, {"status": "схема не той версии"})
    finally:
        engine.dispose()


def test_process_starts_from_environment_without_model(
    database: Database, small: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", database.service_url)
    monkeypatch.setenv("TOIR_TOKEN", TOIR_TOKEN)
    client = TestClient(create_app())
    assert client.get("/healthz").json() == {"status": "ok"}
    # Схема API наружу не публикуется: контракт задан заданием, а не автодокументацией.
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    reply = client.post(
        "/turn",
        json={"session_id": new_session(), "message": "вопрос"},
        headers={"X-User-ID": "u-eng"},
    )
    assert reply.status_code == 200
    assert reply.json()["outcome"] == "error"
    assert reply.json()["turn_id"].startswith("t-")
    assert TOIR_TOKEN not in reply.text


@pytest.mark.parametrize(
    ("name", "value"),
    [("DATABASE_URL", ""), ("TOIR_TOKEN", ""), ("TOIR_TOKEN", "__GENERATE__")],
)
def test_process_does_not_start_with_placeholder_or_missing_secret(
    database: Database, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", database.service_url)
    monkeypatch.setenv("TOIR_TOKEN", TOIR_TOKEN)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        create_app()


def test_handler_can_be_replaced_without_touching_the_frame(service: Service) -> None:
    # Каркас — рамка: тело хода подменяется, не трогая захват сессии и запись. Действие
    # в итог хода ещё не входит: рамка расширится вместе с записью.
    swapped = with_handler(service, Scripted())
    reply = swapped.turn("u-eng", new_session())
    assert (reply.body["outcome"], reply.body["answer"]) == ("answer", "ответ")


def test_data_transactions_are_opened_only_through_the_bound_helper() -> None:
    # Правило «транзакция с данными — только с привязкой к пользователю» держит не база
    # (у app.actions построчной защиты нет), а то, что в коде сервиса нет другого способа
    # открыть транзакцию. Исключения — проверка готовности и сведения о сборке: обе читают
    # только служебные записи без данных пользователя — версию схемы и хэш набора; и список
    # пользователей для страницы: справочник без полномочий и групп, без построчной защиты.
    source = Path(__file__).resolve().parents[1] / "src" / "assistant"
    found = []
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source).as_posix()
        if relative.startswith(("db/", "ingest/")):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if ".begin(" in line or ".connect(" in line:
                found.append(f"{relative}:{number}")
    assert len(found) == 3 and all(item.startswith("api/app.py:") for item in found), found


# --- Страница для демонстрации -------------------------------------------------------------


def test_page_is_served_without_data_or_rights_logic(service: Service) -> None:
    reply = service.client.get("/")
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/html")
    page = reply.text
    # Всё, что страница показывает, она берёт из API: пользователей в ней нет.
    for path in ("/users", "/turn", "/actions/", "/turns/", "X-User-ID"):
        assert path in page
    users = service.client.get("/users").json()
    assert users
    for user in users:
        assert user["user_id"] not in page


def test_users_list_has_no_rights(service: Service) -> None:
    users = service.client.get("/users").json()
    assert [user["user_id"] for user in users] == sorted(user["user_id"] for user in users)
    assert all(set(user) == {"user_id", "name", "position"} for user in users)
