"""Приложение сервиса ассистента: маршруты, пользователь из заголовка, ошибки."""

import logging
import os
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, NoReturn
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text

from assistant.api.schemas import (
    ActionDone,
    ActionRejected,
    Citation,
    TurnRequest,
    TurnResponse,
    Usage,
)
from assistant.config import Settings, ToirSecret, load_settings, load_toir_secret
from assistant.db.migrate import head_revision
from assistant.db.session import service_engine, transaction
from assistant.turn.runner import (
    INTERNAL_ERROR,
    NotReadyHandler,
    Runtime,
    SessionBusy,
    TurnHandler,
    run_turn,
)

log = logging.getLogger(__name__)


class TurnNotStarted(Exception):
    """Ход не начат из-за сбоя до его начала; ответ всё равно по контракту."""


_USER_EXISTS = text("select 1 from app.users where user_id = :user_id")
_ACTION_AUTHOR = text("select user_id from app.actions where action_id = :action_id")
_READY = text(
    "select (select version_num from public.alembic_version),"
    " (select count(*) from app.dataset_version)"
)


def default_runtime(settings: Settings, engine: Engine, handler: TurnHandler) -> Runtime:
    return Runtime(
        settings=settings,
        engine=engine,
        handler=handler,
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
        new_turn_id=lambda: f"t-{secrets.token_hex(6)}",
        new_token=uuid4,
    )


def create_app() -> FastAPI:
    """Точка входа процесса: настройки из окружения. С меткой вместо секрета не стартует."""
    settings = load_settings(os.environ)
    secret = load_toir_secret(os.environ)
    engine = service_engine(settings.database_url)
    return build_app(default_runtime(settings, engine, NotReadyHandler(settings)), secret)


def build_app(runtime: Runtime, toir_secret: ToirSecret) -> FastAPI:
    app = FastAPI(title="Ассистент сотрудника", docs_url=None, redoc_url=None, openapi_url=None)
    # Секрет ТОиР лежит отдельно от того, что получает ход: до модели ему не добраться.
    app.state.toir_secret = toir_secret
    engine = runtime.engine
    # Последняя версия схемы постоянна на всю жизнь процесса: читается один раз.
    schema_head = head_revision()

    def authenticate(x_user_id: str | None) -> str:
        """Одна проверка пользователя на все маршруты. Заголовок объявлен необязательным
        и проверяется здесь: обязательный фреймворк отклонил бы с кодом 422, а нужен 401.
        Хода от имени «по умолчанию» не бывает.
        """
        if not x_user_id:
            raise HTTPException(status_code=401, detail="нет заголовка X-User-ID")
        with transaction(engine, x_user_id) as connection:
            known = connection.execute(_USER_EXISTS, {"user_id": x_user_id}).first()
        if known is None:
            raise HTTPException(status_code=401, detail="неизвестный пользователь")
        return x_user_id

    def current_user(x_user_id: Annotated[str | None, Header()] = None) -> str:
        return authenticate(x_user_id)

    def turn_user(x_user_id: Annotated[str | None, Header()] = None) -> str:
        # Зависимость выполняется раньше проверки полей тела: без заголовка и для
        # неизвестного пользователя ответ — 401, какими бы ни были поля. Тело, которое
        # не разбирается как JSON вовсе, фреймворк отклоняет ещё раньше. Если база
        # недоступна уже здесь, клиент получает ответ по контракту, а не 500.
        try:
            return authenticate(x_user_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise TurnNotStarted from exc

    user = Annotated[str, Depends(current_user)]

    def not_started() -> TurnResponse:
        # Идентификатор выдан, но хода в базе нет: записать его было некуда.
        return TurnResponse(
            turn_id=runtime.new_turn_id(),
            outcome="error",
            answer=INTERNAL_ERROR,
            usage=Usage(prefix_hash=runtime.handler.prefix_hash),
        )

    @app.exception_handler(TurnNotStarted)
    def turn_not_started(request: Request, exc: TurnNotStarted) -> JSONResponse:
        log.error("ход не начат", exc_info=exc.__cause__)
        return JSONResponse(content=not_started().model_dump())

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Где ошибка и какая — без самого ввода: возвращать его незачем, а текст, который
        # не кодируется в UTF-8, сломал бы и этот ответ.
        details = [
            {
                "loc": [str(part) for part in error["loc"]],
                "msg": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Трассировка — в журнал; клиенту — только код и общая фраза.
        log.error("необработанная ошибка на %s", request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "внутренняя ошибка сервиса"})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Готовность для compose: база отвечает, схема на последней версии, набор загружен."""
        try:
            with engine.connect() as connection:
                version, datasets = connection.execute(_READY).one()
        except Exception:
            log.exception("проверка готовности: база недоступна")
            return JSONResponse(status_code=503, content={"status": "база недоступна"})
        if version != schema_head:
            return JSONResponse(status_code=503, content={"status": "схема не той версии"})
        if not datasets:
            return JSONResponse(status_code=503, content={"status": "набор не загружен"})
        return JSONResponse(content={"status": "ok"})

    @app.post("/turn")
    def turn(body: TurnRequest, user_id: Annotated[str, Depends(turn_user)]) -> TurnResponse:
        if len(body.message) > runtime.settings.max_message_chars:
            raise HTTPException(
                status_code=422,
                detail=f"сообщение длиннее {runtime.settings.max_message_chars} символов",
            )
        try:
            finished = run_turn(runtime, user_id, body.session_id, body.message)
        except SessionBusy:
            raise HTTPException(status_code=409, detail="в сессии уже идёт ход") from None
        except Exception:
            # До тела хода дело не дошло — например, база недоступна при захвате сессии.
            log.exception("ход не начат")
            return not_started()
        result, usage = finished.result, finished.usage
        return TurnResponse(
            turn_id=finished.turn_id,
            outcome=result.outcome,  # type: ignore[arg-type]
            answer=result.answer,
            citations=[
                Citation(doc_id=item.doc_id, chunk_id=item.chunk_id) for item in result.citations
            ],
            pending_action=None,
            usage=Usage(
                model_calls=usage.model_calls,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                max_call_input_tokens=usage.max_call_input_tokens,
                prefix_hash=result.prefix_hash,
                latency_ms=finished.latency_ms,
            ),
        )

    def not_yet(action_id: str, user_id: str) -> NoReturn:
        with transaction(engine, user_id) as connection:
            row = connection.execute(_ACTION_AUTHOR, {"action_id": action_id}).first()
        if row is None:
            raise HTTPException(status_code=404, detail="нет такого действия")
        if row[0] != user_id:
            raise HTTPException(status_code=403, detail="действие подтверждает его автор")
        # Сам переход и вызов ТОиР появляются вместе с записью; каркас дальше не идёт.
        raise HTTPException(status_code=501, detail="подтверждение и отказ ещё не реализованы")

    @app.post("/actions/{action_id}/confirm")
    def confirm(action_id: str, user_id: user) -> ActionDone:
        return not_yet(action_id, user_id)

    @app.post("/actions/{action_id}/reject")
    def reject(action_id: str, user_id: user) -> ActionRejected:
        return not_yet(action_id, user_id)

    return app
