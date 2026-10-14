"""Запись: строки таблицы «Подтверждение» из ARCHITECTURE.md и точки сбоя из TESTING.md.

ТОиР — настоящее приложение заглушки над базой `toir_test`, поднятое в процессе тестов;
транспорт до него подставляется вместо HTTP. Сбои моделируются обёртками транспорта
и записи результата, рестарт — новым экземпляром приложения над той же базой.
"""

import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from assistant.actions.draft import TOOL
from assistant.actions.toir import Reply, Transport
from assistant.turn.runner import TurnContext, TurnResult, draft_result
from tests.conftest import Database
from tests.servicekit import TOIR_TOKEN, Scripted, Service, make_service, new_session
from toir_stub.app import build_app as build_toir

pytestmark = pytest.mark.db

PARAMS = {"equipment_id": "EX-1", "description": "течь гидросистемы", "priority": 2}


@dataclass
class Toir:
    """Заглушка ТОиР в процессе тестов и транспорт до неё со счётчиком вызовов."""

    client: TestClient
    calls: int = 0
    # Обёртка, которая может сломать вызов: до отправки, после неё или подменить ответ.
    fault: Callable[[Transport, str, str, Mapping[str, str], Any], Reply] | None = None
    sent: list[str] = field(default_factory=list)

    def _send(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> Reply:
        self.sent.append(headers.get("Idempotency-Key", ""))
        response = self.client.request(method, path, headers=dict(headers), json=body)
        return Reply(response.status_code, response.json() if response.content else None)

    def transport(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> Reply:
        self.calls += 1
        if self.fault is not None:
            return self.fault(self._send, method, path, headers, body)
        return self._send(method, path, headers, body)

    def requests(self, key: str) -> list[dict[str, Any]]:
        response = self.client.get(
            "/requests",
            headers={"Authorization": f"Bearer {TOIR_TOKEN}"},
            params={"idempotency_key": key},
        )
        rows: list[dict[str, Any]] = response.json()
        return rows


@pytest.fixture
def toir_engine(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.toir_url)
    yield engine
    engine.dispose()


@pytest.fixture
def toir(toir_engine: Engine) -> Toir:
    return Toir(TestClient(build_toir(toir_engine, TOIR_TOKEN)))


def _drafting(params: Mapping[str, Any] = PARAMS) -> Scripted:
    def behave(context: TurnContext) -> TurnResult:
        return draft_result("h", TOOL, dict(params))

    return Scripted(behave)


def _service(database: Database, toir: Toir, **kwargs: Any) -> Service:
    return make_service(
        database.service,
        database.service_url,
        _drafting(),
        toir=toir.transport,
        raise_server_exceptions=False,
        **kwargs,
    )


def _propose(service: Service, user: str = "u-k1") -> str:
    reply = service.turn(user, new_session(), "оформи заявку на EX-1: течь")
    assert reply.status == 200 and reply.body["outcome"] == "action", reply.body
    pending = reply.body["pending_action"]
    assert isinstance(pending, dict)
    action_id: str = pending["action_id"]
    return action_id


def _state(database: Database, action_id: str) -> tuple[str, str]:
    with database.owner.connect() as connection:
        row = connection.execute(
            text("select status, idempotency_key::text from app.actions where action_id = :a"),
            {"a": action_id},
        ).one()
    return row[0], row[1]


def _confirm(service: Service, action_id: str, user: str = "u-k1") -> Any:
    return service.client.post(f"/actions/{action_id}/confirm", headers={"X-User-ID": user})


def _reject(service: Service, action_id: str, user: str = "u-k1") -> Any:
    return service.client.post(f"/actions/{action_id}/reject", headers={"X-User-ID": user})


# --- Таблица «Подтверждение» -------------------------------------------------------------


def test_no_confirmation_no_toir_call(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    status, key = _state(database, action_id)
    assert status == "proposed"
    assert toir.calls == 0
    assert toir.requests(key) == []


def test_repeated_confirmation_after_done(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    first = _confirm(service, action_id)
    second = _confirm(service, action_id)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json() and first.json()["status"] == "done"
    # Повтор после `done` ТОиР не вызывает.
    assert toir.calls == 1
    status, key = _state(database, action_id)
    assert status == "done"
    assert [row["request_id"] for row in toir.requests(key)] == [first.json()["request_id"]]
    # В ТОиР ушли параметры из действия и его ключ.
    assert toir.sent == [key]


def test_crash_between_toir_call_and_result(database: Database, small: object, toir: Toir) -> None:
    def crash_once(connection: Any, action_id: str, request_id: str) -> None:
        raise RuntimeError("процесс упал после ответа ТОиР")

    service = _service(database, toir, store=crash_once)
    action_id = _propose(service)
    assert _confirm(service, action_id).status_code == 500
    status, key = _state(database, action_id)
    assert status == "confirming"
    # Рестарт: новый экземпляр приложения над той же базой.
    restarted = _service(database, toir)
    reply = _confirm(restarted, action_id)
    assert reply.status_code == 200
    rows = toir.requests(key)
    assert len(rows) == 1 and rows[0]["request_id"] == reply.json()["request_id"]
    assert _state(database, action_id)[0] == "done"


def test_two_confirmations_at_once(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    start = threading.Barrier(2, timeout=30)
    replies: list[Any] = []

    def confirm() -> None:
        client = TestClient(service.client.app, raise_server_exceptions=False)
        start.wait()
        replies.append(client.post(f"/actions/{action_id}/confirm", headers={"X-User-ID": "u-k1"}))

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert [reply.status_code for reply in replies] == [200, 200]
    assert len({reply.json()["request_id"] for reply in replies}) == 1
    key = _state(database, action_id)[1]
    assert len(toir.requests(key)) == 1
    assert set(toir.sent) == {key}


def _failing(status: int) -> Callable[..., Reply]:
    def fault(send: Transport, *args: Any) -> Reply:
        return Reply(status, {"detail": "сбой"})

    return fault


def _unreachable(send: Transport, *args: Any) -> Reply:
    raise ConnectionRefusedError("нет соединения")


def _lost_reply(send: Transport, *args: Any) -> Reply:
    send(*args)
    raise TimeoutError("ответ не получен")


@pytest.mark.parametrize(
    "fault",
    [_unreachable, _lost_reply, _failing(500), _failing(503), _failing(401)],
    ids=["недоступен", "ответ потерян", "500", "503", "отказ в авторизации"],
)
def test_toir_unavailable_keeps_confirming_and_retry_succeeds(
    database: Database, small: object, toir: Toir, fault: Callable[..., Reply]
) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    toir.fault = fault
    reply = _confirm(service, action_id)
    assert reply.status_code == 502
    status, key = _state(database, action_id)
    # Назад в `proposed` действие не возвращается: неизвестно, создана ли заявка.
    assert status == "confirming"
    toir.fault = None
    again = _confirm(service, action_id)
    assert again.status_code == 200
    assert len(toir.requests(key)) == 1
    # Каждый вызов ТОиР — с одним и тем же ключом из строки действия.
    assert set(toir.sent) == {key}
    assert _state(database, action_id)[0] == "done"


@pytest.mark.parametrize("code", [400, 409, 422])
def test_toir_rejected_the_request(
    database: Database, small: object, toir: Toir, code: int
) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    toir.fault = _failing(code)
    assert _confirm(service, action_id).status_code == 502
    assert _state(database, action_id)[0] == "failed"
    calls = toir.calls
    toir.fault = None
    # Повтор возвращает тот же ответ без вызова ТОиР.
    assert _confirm(service, action_id).status_code == 502
    assert toir.calls == calls
    with database.owner.connect() as connection:
        reason = connection.scalar(
            text("select failure_reason from app.actions where action_id = :a"), {"a": action_id}
        )
    assert reason and TOIR_TOKEN not in reason


def test_grant_revoked_before_confirmation(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    with database.owner.begin() as connection:
        connection.execute(
            text("delete from app.repair_grants where user_id = 'u-k1' and site = 'K1'")
        )
    try:
        assert _confirm(service, action_id).status_code == 403
        assert _state(database, action_id)[0] == "proposed"
        assert toir.calls == 0
    finally:
        with database.owner.begin() as connection:
            connection.execute(
                text("insert into app.repair_grants (user_id, site) values ('u-k1', 'K1')")
            )


def test_grant_revoked_after_intent_retry_completes(
    database: Database, small: object, toir: Toir
) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    # Заявка создана, ответ потерян: ради этого случая повтор доводит намерение.
    toir.fault = _lost_reply
    assert _confirm(service, action_id).status_code == 502
    toir.fault = None
    with database.owner.begin() as connection:
        connection.execute(
            text("delete from app.repair_grants where user_id = 'u-k1' and site = 'K1'")
        )
    try:
        # Намерение было разрешено: повтор доводит его, а не оставляет заявку незаписанной.
        again = _confirm(service, action_id)
        assert again.status_code == 200
        assert _state(database, action_id)[0] == "done"
        assert len(toir.requests(_state(database, action_id)[1])) == 1
    finally:
        with database.owner.begin() as connection:
            connection.execute(
                text("insert into app.repair_grants (user_id, site) values ('u-k1', 'K1')")
            )


def test_confirmation_or_rejection_by_another_user(
    database: Database, small: object, toir: Toir
) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    # Чужой — и тот, у кого есть право на этот участок.
    for user in ("u-eng", "u-hr"):
        assert _confirm(service, action_id, user).status_code == 403
        assert _reject(service, action_id, user).status_code == 403
    assert _state(database, action_id)[0] == "proposed"
    assert toir.calls == 0


def test_confirmation_after_rejection(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    first, second = _reject(service, action_id), _reject(service, action_id)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json() == {"status": "rejected"}
    assert _confirm(service, action_id).status_code == 409
    assert _state(database, action_id)[0] == "rejected"
    assert toir.calls == 0


def test_rejection_after_confirmation(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    assert _confirm(service, action_id).status_code == 200
    assert _reject(service, action_id).status_code == 409
    assert _state(database, action_id)[0] == "done"


def test_unknown_action_is_404(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    for verb in (_confirm, _reject):
        assert verb(service, "a-нет-такого").status_code == 404


# --- Точки сбоя из TESTING.md, которых нет в таблице выше ----------------------------------


def test_crash_after_intent_before_toir_call(database: Database, small: object, toir: Toir) -> None:
    def crash(send: Transport, *args: Any) -> Reply:
        raise RuntimeError("процесс упал до вызова ТОиР")

    service = _service(database, toir)
    action_id = _propose(service)
    toir.fault = crash
    assert _confirm(service, action_id).status_code == 500
    status, key = _state(database, action_id)
    assert status == "confirming"
    assert toir.requests(key) == []
    toir.fault = None
    assert _confirm(_service(database, toir), action_id).status_code == 200
    assert len(toir.requests(key)) == 1


def test_confirmation_and_rejection_at_once(database: Database, small: object, toir: Toir) -> None:
    service = _service(database, toir)
    action_id = _propose(service)
    start = threading.Barrier(2, timeout=30)
    replies: dict[str, Any] = {}

    def run(verb: str) -> None:
        client = TestClient(service.client.app, raise_server_exceptions=False)
        start.wait()
        replies[verb] = client.post(f"/actions/{action_id}/{verb}", headers={"X-User-ID": "u-k1"})

    threads = [threading.Thread(target=run, args=(verb,)) for verb in ("confirm", "reject")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    status, key = _state(database, action_id)
    codes = (replies["confirm"].status_code, replies["reject"].status_code)
    # Побеждает одно, итог согласован.
    if status == "done":
        assert codes == (200, 409)
        assert len(toir.requests(key)) == 1
    else:
        assert (status, codes) == ("rejected", (409, 200))
        assert toir.requests(key) == []


def test_confirm_responses_and_log_carry_no_secret(
    database: Database, small: object, toir: Toir, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    service = _service(database, toir)
    action_id = _propose(service)
    toir.fault = _failing(503)
    texts = [_confirm(service, action_id).text]
    toir.fault = _unreachable
    texts.append(_confirm(service, action_id).text)
    toir.fault = None
    texts += [_confirm(service, action_id).text, _reject(service, action_id).text]
    assert all(TOIR_TOKEN not in body for body in texts)
    # Отказ ТОиР попал в журнал — без токена.
    assert "ТОиР ответил 503" in caplog.text
    assert "ТОиР недоступен: ConnectionRefusedError" in caplog.text
    assert TOIR_TOKEN not in caplog.text
