"""Ход от захвата сессии до завершающей транзакции.

Что делает ход внутри — обращается к модели, зовёт инструменты, — решает обработчик;
здесь только рамка, одинаковая для любого обработчика: сессия занята на время хода,
у хода есть общий предел времени, итог записывается одной транзакцией под своей меткой
захвата, а любая ошибка внутри становится `outcome: error`, но не ответом 500.
"""

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
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
MODEL_NOT_CONFIGURED = "Модель не настроена: задайте переменные MODEL_BASE_URL и MODEL_NAME."
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
class UsageTotals:
    model_calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int | None = None
    output_tokens: int = 0
    max_call_input_tokens: int = 0


class UsageMeter:
    """Расход хода. Обработчик записывает сюда каждый вызов модели сразу после ответа,
    поэтому расход уже сделанных вызовов сохраняется, чем бы ход ни кончился —
    ответом, превышением срока или исключением.
    """

    def __init__(self) -> None:
        self._totals = UsageTotals()

    def record(self, input_tokens: int, output_tokens: int, cached: int | None = None) -> None:
        """Фактические числа одного вызова из ответа провайдера."""
        before = self._totals
        known = before.cached_input_tokens is not None or cached is not None
        self._totals = UsageTotals(
            model_calls=before.model_calls + 1,
            input_tokens=before.input_tokens + input_tokens,
            # Провайдер не отдаёт кэшированный вход ни в одном вызове — остаётся null.
            cached_input_tokens=(before.cached_input_tokens or 0) + (cached or 0)
            if known
            else None,
            output_tokens=before.output_tokens + output_tokens,
            max_call_input_tokens=max(before.max_call_input_tokens, input_tokens),
        )

    @property
    def totals(self) -> UsageTotals:
        return self._totals


@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    user_id: str
    session_id: str
    message: str
    deadline: Deadline
    usage: UsageMeter


@dataclass(frozen=True)
class CitationRef:
    doc_id: str
    chunk_id: str


@dataclass(frozen=True)
class StoredMessage:
    """Сообщение истории сессии для следующих ходов."""

    role: str
    content: dict[str, Any]
    chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposedAction:
    """Черновик действия: записывается завершающей транзакцией хода в статусе `proposed`."""

    tool: str
    params: dict[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """Итог тела хода. Расхода здесь нет: он копится в `TurnContext.usage`."""

    outcome: str
    answer: str
    prefix_hash: str
    citations: tuple[CitationRef, ...] = ()
    # Запись хода для диагностики и сообщения для истории; у хода с ошибкой истории нет.
    record: dict[str, Any] = field(default_factory=dict)
    messages: tuple[StoredMessage, ...] = ()
    # Не больше одного действия на ход; у хода с действием `outcome` — `action`.
    action: ProposedAction | None = None


DRAFT_ANSWER = (
    "Подготовлена заявка на ремонт оборудования {equipment_id}, приоритет {priority}:"
    " {description}. Заявка будет создана в ТОиР только после вашего подтверждения."
)


def draft_result(
    prefix_hash: str, tool: str, params: dict[str, Any], record: dict[str, Any] | None = None
) -> TurnResult:
    """Итог хода с черновиком: следующего вызова модели нет, текст ответа — шаблон кода."""
    return TurnResult(
        outcome="action",
        answer=DRAFT_ANSWER.format(**params),
        prefix_hash=prefix_hash,
        record=record or {},
        action=ProposedAction(tool, params),
    )


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
    new_action_id: Callable[[], str] = field(default=lambda: f"a-{secrets.token_hex(8)}")


class Stored(Enum):
    """Чем кончилась завершающая транзакция."""

    YES = "записан"
    # Метка захвата уже не своя: срок истёк, и сессию занял другой ход.
    LOST_CAPTURE = "пережил отметку"
    # Запись не удалась по другой причине: сбой базы, нарушение ограничения.
    FAILED = "не записан"


@dataclass(frozen=True)
class Finished:
    turn_id: str
    result: TurnResult
    usage: UsageTotals
    latency_ms: int
    stored: Stored
    # Идентификатор записанного действия; клиент получает его, только если ход записан.
    action_id: str | None = None


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
    meter = UsageMeter()
    context = TurnContext(turn_id, user_id, session_id, message, deadline, meter)
    try:
        result = runtime.handler(context)
        if deadline.expired():
            result = TurnResult(
                outcome="error",
                answer=TIMEOUT_ERROR,
                prefix_hash=result.prefix_hash,
                record=result.record,
            )
    except Exception:
        # Трассировка — в журнал сервиса; в ответ клиенту она не попадает.
        log.exception("ход %s завершился ошибкой", turn_id)
        result = TurnResult(
            outcome="error", answer=INTERNAL_ERROR, prefix_hash=runtime.handler.prefix_hash
        )

    if result.action is not None and result.outcome != "action":
        # Действие без исхода `action` — ошибка обработчика: ни то ни другое не отдаётся.
        log.error("ход %s: действие при исходе %s", turn_id, result.outcome)
        result = TurnResult(outcome="error", answer=INTERNAL_ERROR, prefix_hash=result.prefix_hash)
    action_id = runtime.new_action_id() if result.action is not None else None
    latency_ms = int((runtime.monotonic() - started) * 1000)
    stored = _finish(runtime, context, token, result, latency_ms, action_id)
    if stored is not Stored.YES and result.outcome != "error":
        # Ответ, который не записан, клиенту не отдаётся: в истории его не будет.
        result = TurnResult(
            outcome="error",
            answer=LOST_CAPTURE_ERROR if stored is Stored.LOST_CAPTURE else INTERNAL_ERROR,
            prefix_hash=result.prefix_hash,
        )
    if stored is not Stored.YES:
        action_id = None
    return Finished(turn_id, result, meter.totals, latency_ms, stored, action_id)


def _finish(
    runtime: Runtime,
    context: TurnContext,
    token: UUID,
    result: TurnResult,
    latency_ms: int,
    action_id: str | None,
) -> Stored:
    """Завершающая транзакция: снять отметку под своей меткой и записать ход и сообщения.

    Отметка снимается здесь же и при ошибке внутри хода — сразу, а не по сроку.
    """
    usage = context.usage.totals
    try:
        with transaction(runtime.engine, context.user_id) as connection:
            if not release(connection, context.user_id, context.session_id, token):
                log.warning("ход %s пережил свою отметку занятости", context.turn_id)
                return Stored.LOST_CAPTURE
            connection.execute(
                insert(models.Turn).values(
                    turn_id=context.turn_id,
                    user_id=context.user_id,
                    session_id=context.session_id,
                    question=context.message,
                    outcome=result.outcome,
                    answer=result.answer,
                    citations=[
                        {"doc_id": item.doc_id, "chunk_id": item.chunk_id}
                        for item in result.citations
                    ],
                    model_calls=usage.model_calls,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    max_call_input_tokens=usage.max_call_input_tokens,
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
            if result.action is not None and action_id is not None:
                # Действие — в той же транзакции, что и ход: не записан ход — нет и действия,
                # и клиент его идентификатора не получит.
                connection.execute(
                    insert(models.Action).values(
                        action_id=action_id,
                        user_id=context.user_id,
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        tool=result.action.tool,
                        params=result.action.params,
                    )
                )
        return Stored.YES
    except Exception:
        log.exception("ход %s не записан", context.turn_id)
    # Запись не прошла и откатилась вместе со снятием отметки: снимаем её отдельно,
    # чтобы сессия не ждала истечения срока.
    try:
        with transaction(runtime.engine, context.user_id) as connection:
            release(connection, context.user_id, context.session_id, token)
    except Exception:
        log.exception("отметка занятости хода %s не снята, истечёт по сроку", context.turn_id)
    return Stored.FAILED
