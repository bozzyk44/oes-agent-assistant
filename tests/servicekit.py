"""Сервис в процессе тестов: часы и идентификаторы подставлены, обработчик хода — свой."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import count
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from assistant.actions.confirm import StoreResult, store_result
from assistant.actions.toir import Transport
from assistant.api.app import build_app
from assistant.config import ModelSettings, Settings, ToirSecret
from assistant.turn.runner import (
    NotReadyHandler,
    Runtime,
    TurnContext,
    TurnHandler,
    TurnResult,
    empty_prefix_hash,
)

TOIR_TOKEN = "test-toir-token-marker-5d19"
MODEL_KEY = "test-model-key-marker-8a02"
# Идентификаторы свои на каждый запуск: база между тестами и запусками не откатывается.
RUN = uuid4().hex[:8]
_turns = count(1)
_sessions = count(1)


def new_session() -> str:
    return f"s-{RUN}-{next(_sessions)}"


@dataclass
class Clock:
    """Часы тестов: срок отметки и предел времени истекают переводом, а не паузой."""

    now: datetime = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    mono: float = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


@dataclass
class Scripted:
    """Обработчик хода со сценарием: возвращает результат или делает то, что велено."""

    behave: Callable[[TurnContext], TurnResult] | None = None
    prefix_hash: str = field(default_factory=empty_prefix_hash)
    seen: list[TurnContext] = field(default_factory=list)

    def __call__(self, context: TurnContext) -> TurnResult:
        self.seen.append(context)
        if self.behave is None:
            return TurnResult(outcome="answer", answer="ответ", prefix_hash=self.prefix_hash)
        return self.behave(context)


def settings(database_url: str, *, model: bool = False) -> Settings:
    return Settings(
        database_url=database_url,
        data_dir="data/",
        model=ModelSettings(
            base_url="http://model.invalid/v1" if model else "",
            api_key=MODEL_KEY if model else "",
            name="test-model" if model else "",
        ),
        toir_url="http://toir.invalid",
        max_message_chars=200,
        turn_timeout_seconds=90,
        session_lease_seconds=120,
    )


@dataclass
class Service:
    runtime: Runtime
    clock: Clock
    client: TestClient

    def turn(self, user: str | None, session: str, message: str = "вопрос") -> Reply:
        headers = {} if user is None else {"X-User-ID": user}
        response = self.client.post(
            "/turn", json={"session_id": session, "message": message}, headers=headers
        )
        return Reply(response.status_code, response.json(), response.text)


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict[str, object]
    text: str


def make_service(
    engine: Engine,
    database_url: str,
    handler: TurnHandler | None = None,
    *,
    model: bool = False,
    raise_server_exceptions: bool = True,
    toir: Transport | None = None,
    store: StoreResult = store_result,
    config: Settings | None = None,
) -> Service:
    clock = Clock()
    config = config or settings(database_url, model=model)
    runtime = Runtime(
        settings=config,
        engine=engine,
        handler=handler or NotReadyHandler(config),
        clock=lambda: clock.now,
        monotonic=lambda: clock.mono,
        new_turn_id=lambda: f"t-{RUN}-{next(_turns)}",
        new_token=uuid4,
    )
    app = build_app(runtime, ToirSecret(TOIR_TOKEN), toir, store)
    client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    return Service(runtime, clock, client)


def with_handler(service: Service, handler: TurnHandler) -> Service:
    runtime = replace(service.runtime, handler=handler)
    app = build_app(runtime, ToirSecret(TOIR_TOKEN))
    return Service(runtime, service.clock, TestClient(app))
