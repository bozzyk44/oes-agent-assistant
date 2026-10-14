"""Заглушка ТОиР как приёмник: настоящее приложение над базой `toir` под ролью заглушки.

База между тестами и запусками не очищается, поэтому каждый тест берёт свои ключи
и смотрит только на свои заявки.
"""

import threading
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from tests.conftest import Database
from toir_stub.app import ConfigError, build_app, create_app

pytestmark = pytest.mark.db

TOKEN = "test-toir-stub-token-marker-77c1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"equipment_id": "XY-7", "description": "течь гидросистемы", "priority": 2}


@pytest.fixture
def engine(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.toir_url)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine: Engine) -> TestClient:
    return TestClient(build_app(engine, TOKEN))


def _key() -> str:
    return f"k-{uuid4()}"


def _post(client: TestClient, key: str | None, body: Any = BODY, headers: Any = AUTH) -> Any:
    extra = {} if key is None else {"Idempotency-Key": key}
    return client.post("/requests", json=body, headers={**headers, **extra})


def _mine(client: TestClient, key: str) -> list[dict[str, Any]]:
    response = client.get("/requests", headers=AUTH, params={"idempotency_key": key})
    assert response.status_code == 200
    rows: list[dict[str, Any]] = response.json()
    return rows


def test_list_without_filter_has_all_requests_in_order(client: TestClient) -> None:
    keys = [_key(), _key()]
    created = [_post(client, key).json() for key in keys]
    rows = client.get("/requests", headers=AUTH).json()
    assert [row for row in rows if row["idempotency_key"] in keys] == created


def test_new_key_creates_request(client: TestClient) -> None:
    key = _key()
    response = _post(client, key)
    assert response.status_code == 201
    created = response.json()
    assert created["request_id"].startswith("REQ-")
    assert {name: created[name] for name in BODY} == BODY
    assert _mine(client, key) == [created]


def test_repeat_with_same_key_returns_same_request(client: TestClient) -> None:
    key = _key()
    first = _post(client, key)
    second = _post(client, key)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert len(_mine(client, key)) == 1


def test_same_key_with_other_body_is_409(client: TestClient) -> None:
    key = _key()
    _post(client, key)
    other = _post(client, key, {**BODY, "priority": 1})
    assert other.status_code == 409
    assert [row["priority"] for row in _mine(client, key)] == [2]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": "Bearer "},
        {"Authorization": f"Basic {TOKEN}"},
    ],
    ids=["нет", "неверный", "без схемы", "пустой", "другая схема"],
)
def test_request_without_valid_token_is_rejected(client: TestClient, headers: Any) -> None:
    key = _key()
    # Отказ раньше проверки ключа и тела: и неверное тело, и неразборчивый JSON без ключа
    # без токена — 401, а не 422 или 400.
    assert _post(client, key, headers=headers).status_code == 401
    assert _post(client, key, body={"x": 1}, headers=headers).status_code == 401
    garbage = client.post("/requests", content=b"{not json", headers=headers)
    assert garbage.status_code == 401
    assert client.get("/requests", headers=headers).status_code == 401
    assert _mine(client, key) == []


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "Bearer  "])
def test_bearer_scheme_is_case_insensitive(client: TestClient, scheme: str) -> None:
    key = _key()
    reply = _post(client, key, headers={"Authorization": f"{scheme} {TOKEN}"})
    assert reply.status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "priority": 4},
        {**BODY, "priority": "2"},
        {**BODY, "equipment_id": ""},
        {**BODY, "description": ""},
        {**BODY, "description": "нулевой \u0000 символ"},
        {"equipment_id": "XY-7", "priority": 2},
        {**BODY, "extra": "поле"},
    ],
    ids=[
        "приоритет 4",
        "приоритет строкой",
        "пустой идентификатор",
        "пустое описание",
        "нулевой символ",
        "без описания",
        "лишнее",
    ],
)
def test_invalid_body_is_rejected_as_client_error(client: TestClient, body: Any) -> None:
    # Отказ как неверному запросу: сервис переводит действие в `failed` и не повторяет.
    key = _key()
    assert _post(client, key, body).status_code == 422
    assert _mine(client, key) == []


@pytest.mark.parametrize("key", [None, "", "x" * 201])
def test_idempotency_key_is_required(client: TestClient, key: str | None) -> None:
    assert _post(client, key).status_code == 400
    # Ключ проверяется раньше тела.
    assert _post(client, key, body={"priority": 9}).status_code == 400


def test_lone_surrogate_is_422_not_500(client: TestClient) -> None:
    raw = b'{"equipment_id": "XY-7", "description": "\\ud800", "priority": 1}'
    reply = client.post("/requests", content=raw, headers={**AUTH, "Idempotency-Key": _key()})
    assert reply.status_code == 422


def test_invalid_json_with_token_is_422(client: TestClient) -> None:
    reply = client.post(
        "/requests", content=b"{not json", headers={**AUTH, "Idempotency-Key": _key()}
    )
    assert reply.status_code == 422


def test_requests_survive_restart(engine: Engine, database: Database) -> None:
    key = _key()
    created = _post(TestClient(build_app(engine, TOKEN)), key).json()
    # Новый экземпляр над той же базой — то же, что перезапущенный процесс.
    restarted = create_engine(database.toir_url)
    try:
        client = TestClient(build_app(restarted, TOKEN))
        assert _mine(client, key) == [created]
        repeated = _post(client, key)
        assert (repeated.status_code, repeated.json()) == (200, created)
    finally:
        restarted.dispose()


def test_concurrent_requests_with_one_key_give_one_request(engine: Engine) -> None:
    key = _key()
    app = build_app(engine, TOKEN)
    start = threading.Barrier(4, timeout=30)
    replies: list[Any] = []

    def send() -> None:
        client = TestClient(app)
        start.wait()
        replies.append(_post(client, key))

    threads = [threading.Thread(target=send) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(reply.status_code for reply in replies) == [200, 200, 200, 201]
    assert len({reply.json()["request_id"] for reply in replies}) == 1
    assert len(_mine(TestClient(app), key)) == 1


def test_key_uniqueness_is_a_table_constraint(engine: Engine, client: TestClient) -> None:
    key = _key()
    _post(client, key)
    with pytest.raises(Exception, match="unique"), engine.begin() as connection:
        connection.execute(
            text(
                "insert into toir.requests (idempotency_key, body_hash, equipment_id,"
                " description, priority) values (:k, 'h', 'XY-7', 'd', 1)"
            ),
            {"k": key},
        )


def test_schema_creation_is_repeatable(engine: Engine) -> None:
    build_app(engine, TOKEN)
    build_app(engine, TOKEN)


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("missing", "placeholder"),
    [
        ("TOIR_TOKEN", False),
        ("TOIR_DATABASE_URL", False),
        ("TOIR_TOKEN", True),
        ("TOIR_DATABASE_URL", True),
    ],
)
def test_process_does_not_start_without_real_settings(
    monkeypatch: pytest.MonkeyPatch, database: Database, missing: str, placeholder: bool
) -> None:
    monkeypatch.setenv("TOIR_DATABASE_URL", database.toir_url)
    monkeypatch.setenv("TOIR_TOKEN", TOKEN)
    if placeholder:
        monkeypatch.setenv(missing, "__GENERATE__")
    else:
        monkeypatch.delenv(missing)
    with pytest.raises(ConfigError, match=missing) as caught:
        create_app()
    assert TOKEN not in str(caught.value)
