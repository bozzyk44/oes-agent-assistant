"""Захват сессии: одна сессия — один ход одновременно.

Ход начинается с условного обновления, которое ставит отметку «занята до» и случайную
метку захвата, если сессия свободна или прежний срок истёк. Завершающая транзакция
снимает отметку и проходит, только если метка всё ещё своя: ход, переживший свой срок,
ничего не запишет поверх чужого. Отметка упавшего процесса истекает сама.

Время приходит от вызывающего, а не от базы: в тестах срок истекает переводом часов.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, text

_CAPTURE = text(
    """
    insert into app.sessions (user_id, session_id, busy_until, capture_token)
    values (:user_id, :session_id, :until, :token)
    on conflict (user_id, session_id) do update
        set busy_until = excluded.busy_until, capture_token = excluded.capture_token
        where app.sessions.busy_until is null or app.sessions.busy_until <= :now
    returning capture_token
    """
)
_RELEASE = text(
    """
    update app.sessions set busy_until = null, capture_token = null
    where user_id = :user_id and session_id = :session_id and capture_token = :token
    """
)


def capture(
    connection: Connection,
    user_id: str,
    session_id: str,
    token: UUID,
    now: datetime,
    lease: timedelta,
) -> bool:
    """Занимает сессию; `False` — в ней уже идёт ход и его срок не истёк."""
    row = connection.execute(
        _CAPTURE,
        {
            "user_id": user_id,
            "session_id": session_id,
            "until": now + lease,
            "token": token,
            "now": now,
        },
    ).first()
    return row is not None


def release(connection: Connection, user_id: str, session_id: str, token: UUID) -> bool:
    """Снимает отметку; `False` — метка уже не своя: сессию занял другой ход."""
    result = connection.execute(
        _RELEASE, {"user_id": user_id, "session_id": session_id, "token": token}
    )
    return result.rowcount == 1
