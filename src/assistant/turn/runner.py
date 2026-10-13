"""Ход от захвата сессии до завершающей транзакции.

Что делает ход внутри — обращается к модели, зовёт инструменты, — решает обработчик;
здесь только рамка, одинаковая для любого обработчика: сессия занята на время хода,
у хода есть общий предел времени, итог записывается одной транзакцией под своей меткой
захвата, а любая ошибка внутри становится `outcome: error`, но не ответом 500.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, insert

from assistant.config import Settings
from assistant.db import models
from assistant.db.session import transaction
from assistant.turn.capture import capture, release

log = logging.getLogger(__name__)

INTERNAL_ERROR = "Не удалось выполнить запрос: внутренняя ошибка сервиса."
TIMEOUT_ERROR = "Не удалось выполнить запрос: превышен предел времени хода."
LOST_CAPTURE_ERROR = "Не удалось выполнить запрос: ход не уложился в отведённый срок."
MODEL_NOT_CONFIGURED = (
    "Модель не настроена: задайте переменные MODEL_BASE_URL, MODEL_API_KEY и MODEL_NAME."
)
LOOP_NOT_READY = "Агентный цикл ещё не подключён: сервис принимает запросы, но не отвечает."


class SessionBusy(Exception):
    """В сессии уже идёт ход."""


@dataclass(frozen=True)
class Deadline:
    """Общий предел времени хода по монотонным часам."""

    now: Callable[[], float]
    at: float

    def remaining(self) -> float:
        return max(self.at - self.now(), 0.0)

    def expired(self) -> bool:
        return self.now() >= self.at


@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    user_id: str
    session_id: str
    message: str
    deadline: Deadline


@dataclass(frozen=True)
class UsageTotals:
    model_calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int | None = None
    output_tokens: int = 0
    max_call_input_tokens: int = 0


@dataclass(frozen=True)
class StoredMessage:
    """Сообщение истории сессии для следующих ходов."""

    role: str
    content: dict[str, Any]
    chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnResult:
    outcome: str
    answer: str
    prefix_hash: str
    citations: tuple[dict[str, str], ...] = ()
    usage: UsageTotals = field(default_factory=UsageTotals)
    # Запись хода для диагностики и сообщения для истории; у хода с ошибкой истории нет.
    record: dict[str, Any] = field(default_factory=dict)
    messages: tuple[StoredMessage, ...] = ()


class TurnHandler(Protocol):
    """Тело хода. Обязан уложиться в `context.deadline`; исключение — это `outcome: error`."""

    prefix_hash: str

    def __call__(self, context: TurnContext) -> TurnResult: ...


@dataclass(frozen=True)
class Runtime:
    """Всё, от чего зависит ход. Часы и генераторы идентификаторов подставляются."""

    settings: Settings
    engine: Engine
    handler: TurnHandler
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    new_turn_id: Callable[[], str]
    new_token: Callable[[], UUID]


@dataclass(frozen=True)
class Finished:
    turn_id: str
    result: TurnResult
    latency_ms: int
    # Ход записан в базу; `False` — он пережил свою отметку или запись не удалась.
    stored: bool


def empty_prefix_hash() -> str:
    """Хэш неизменной части входа, пока в ней нет ни системной части, ни инструментов."""
    canonical = json.dumps({"system": "", "tools": []}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class NotReadyHandler:
    """Обработчик каркаса: модели он не зовёт и честно говорит, почему ответа нет."""

    def __init__(self, settings: Settings) -> None:
        self._configured = settings.model.configured
        self.prefix_hash = empty_prefix_hash()

    def __call__(self, context: TurnContext) -> TurnResult:
        reason = LOOP_NOT_READY if self._configured else MODEL_NOT_CONFIGURED
        return TurnResult(outcome="error", answer=reason, prefix_hash=self.prefix_hash)


def run_turn(runtime: Runtime, user_id: str, session_id: str, message: str) -> Finished:
    """Выполняет ход. `SessionBusy` — единственное исключение, которое уходит наружу
    намеренно: всё остальное внутри хода превращается в `outcome: error`.
    """
    settings = runtime.settings
    turn_id = runtime.new_turn_id()
    token = runtime.new_token()
    started = runtime.monotonic()

    with transaction(runtime.engine, user_id) as connection:
        captured = capture(
            connection,
            user_id,
            session_id,
            token,
            now=runtime.clock(),
            lease=timedelta(seconds=settings.session_lease_seconds),
        )
    if not captured:
        raise SessionBusy

    deadline = Deadline(runtime.monotonic, started + settings.turn_timeout_seconds)
    context = TurnContext(turn_id, user_id, session_id, message, deadline)
    try:
        result = runtime.handler(context)
        if deadline.expired():
            # Расход уже сделанных вызовов сохраняется, ответ — нет.
            result = TurnResult(
                outcome="error",
                answer=TIMEOUT_ERROR,
                prefix_hash=result.prefix_hash,
                usage=result.usage,
                record=result.record,
            )
    except Exception:
        # Трассировка — в журнал сервиса; в ответ клиенту она не попадает.
        log.exception("ход %s завершился ошибкой", turn_id)
        result = TurnResult(
            outcome="error", answer=INTERNAL_ERROR, prefix_hash=runtime.handler.prefix_hash
        )

    latency_ms = int((runtime.monotonic() - started) * 1000)
    stored = _finish(runtime, context, token, result, latency_ms)
    if not stored and result.outcome != "error":
        # Ответ, который не записан, клиенту не отдаётся: в истории его не будет.
        result = TurnResult(
            outcome="error",
            answer=LOST_CAPTURE_ERROR,
            prefix_hash=result.prefix_hash,
            usage=result.usage,
        )
    return Finished(turn_id, result, latency_ms, stored)


def _finish(
    runtime: Runtime, context: TurnContext, token: UUID, result: TurnResult, latency_ms: int
) -> bool:
    """Завершающая транзакция: снять отметку под своей меткой и записать ход и сообщения.

    Отметка снимается здесь же и при ошибке внутри хода — сразу, а не по сроку.
    """
    try:
        with transaction(runtime.engine, context.user_id) as connection:
            if not release(connection, context.user_id, context.session_id, token):
                # Метка уже не своя: срок истёк, и сессию занял другой ход. Писать нечего.
                log.warning("ход %s пережил свою отметку занятости", context.turn_id)
                return False
            connection.execute(
                insert(models.Turn).values(
                    turn_id=context.turn_id,
                    user_id=context.user_id,
                    session_id=context.session_id,
                    question=context.message,
                    outcome=result.outcome,
                    answer=result.answer,
                    citations=list(result.citations),
                    model_calls=result.usage.model_calls,
                    input_tokens=result.usage.input_tokens,
                    cached_input_tokens=result.usage.cached_input_tokens,
                    output_tokens=result.usage.output_tokens,
                    max_call_input_tokens=result.usage.max_call_input_tokens,
                    prefix_hash=result.prefix_hash,
                    latency_ms=latency_ms,
                    record=result.record,
                )
            )
            if result.messages:
                connection.execute(
                    insert(models.Message),
                    [
                        {
                            "user_id": context.user_id,
                            "session_id": context.session_id,
                            "turn_id": context.turn_id,
                            "seq": seq,
                            "role": message.role,
                            "content": message.content,
                            "chunk_ids": list(message.chunk_ids),
                        }
                        for seq, message in enumerate(result.messages, start=1)
                    ],
                )
        return True
    except Exception:
        log.exception("ход %s не записан", context.turn_id)
    # Запись не прошла и откатилась вместе со снятием отметки: снимаем её отдельно,
    # чтобы сессия не ждала истечения срока.
    try:
        with transaction(runtime.engine, context.user_id) as connection:
            release(connection, context.user_id, context.session_id, token)
    except Exception:
        log.exception("отметка занятости хода %s не снята, истечёт по сроку", context.turn_id)
    return False
