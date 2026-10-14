"""Изменяемая часть входа: сведения о пользователе, история сессии, действия, вопрос.

Порядок выбран под кэш префикса: то, что не меняется в сессии, стоит раньше. Сведения
о пользователе — заголовок первого вопроса сессии: от хода к ходу начало входа совпадает.
Статусы действий — блоком перед текущим вопросом, а не в истории: подтверждение заявки
не переписывает историю задним числом. Результатов инструментов прошлых ходов в истории
нет: модель запросит данные заново, и они снова пройдут проверку прав.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from assistant.access import Grants, Period

TABLE_NAMES = {
    "production": "добыча (production)",
    "downtime": "простои (downtime)",
    "equipment": "оборудование (equipment)",
}

_HISTORY = text(
    """
    select m.turn_id, m.role, m.content, m.chunk_ids
    from app.messages m join app.turns t on t.turn_id = m.turn_id
    where m.user_id = :user_id and m.session_id = :session_id
    order by t.created_at, m.seq
    """
)
_ACTIONS = text(
    """
    select action_id, tool, params, status, request_id from app.actions
    where user_id = :user_id and session_id = :session_id
    order by created_at
    """
)


_HISTORY_START = text(
    """
    select t.record ->> 'history_start' from app.turns t
    where t.user_id = :user_id and t.session_id = :session_id
      -- Точка — ход, оставивший историю: ход без сообщений точкой быть не может.
      and exists (
          select 1 from app.messages m
          where m.user_id = t.user_id and m.session_id = t.session_id
            and m.turn_id = t.record ->> 'history_start'
      )
    order by t.created_at desc
    limit 1
    """
)


@dataclass(frozen=True)
class HistoryMessage:
    role: str
    text: str
    chunk_ids: tuple[str, ...]
    turn_id: str = ""


def user_info(grants: Grants, periods: dict[str, Period]) -> str:
    """Подсказка модели о полномочиях, а не механизм прав: без неё ни одна гарантия
    не изменится — изменится число лишних вызовов.
    """
    lines = [
        "[Сведения о пользователе — от системы, не вопрос]",
        f"Пользователь: {grants.name}, {grants.position}.",
    ]
    tables = [
        f"{TABLE_NAMES.get(table, table)} — участки {', '.join(sorted(sites))}"
        for table, sites in sorted(grants.kpi.items())
        if sites
    ]
    lines.append("Доступ к данным КХД: " + ("; ".join(tables) if tables else "нет") + ".")
    repair = ", ".join(sorted(grants.repair_sites))
    lines.append(
        f"Заявки на ремонт: {'на оборудование участков ' + repair if repair else 'нельзя'}."
    )
    for table, period in sorted(periods.items()):
        lines.append(
            f"Данные «{TABLE_NAMES.get(table, table)}» — с {period.first.isoformat()}"
            f" по {period.last.isoformat()}."
        )
    return "\n".join(lines)


def load_history(connection: Connection, user_id: str, session_id: str) -> list[HistoryMessage]:
    rows = connection.execute(_HISTORY, {"user_id": user_id, "session_id": session_id})
    return [
        HistoryMessage(
            row.role, str(row.content.get("text", "")), tuple(row.chunk_ids or ()), row.turn_id
        )
        for row in rows
    ]


def history_start(connection: Connection, user_id: str, session_id: str) -> str | None:
    """С какого хода прошлый ход начинал историю: точка отбрасывания наследуется, чтобы
    начало входа не сдвигалось от хода к ходу и кэш префикса не сбрасывался."""
    value = connection.scalar(_HISTORY_START, {"user_id": user_id, "session_id": session_id})
    return str(value) if value else None


def actions_block(connection: Connection, user_id: str, session_id: str) -> str:
    rows = list(connection.execute(_ACTIONS, {"user_id": user_id, "session_id": session_id}))
    if not rows:
        return ""
    names = {
        "proposed": "ждёт подтверждения",
        "confirming": "подтверждается",
        "done": "создана",
        "rejected": "отклонена пользователем",
        "failed": "отвергнута ТОиР",
    }
    lines = ["[Действия этой сессии — от системы]"]
    for row in rows:
        params: dict[str, Any] = row.params or {}
        lines.append(
            f"Заявка на {params.get('equipment_id', '?')}, приоритет {params.get('priority', '?')}:"
            f" {names.get(row.status, row.status)}"
            + (f", номер {row.request_id}" if row.request_id else "")
            + "."
        )
    return "\n".join(lines)


def build_messages(
    info: str, history: list[HistoryMessage], actions: str, question: str
) -> list[dict[str, Any]]:
    """Изменяемая часть входа до вызовов инструментов этого хода."""
    messages: list[dict[str, Any]] = []
    first_user = True
    for item in history:
        content = item.text
        if item.role == "user" and first_user:
            content = f"{info}\n\n{content}"
            first_user = False
        if item.role == "assistant" and item.chunk_ids:
            content = f"{content}\n[Фрагменты: {', '.join(item.chunk_ids)}]"
        messages.append({"role": item.role, "content": content})
    current = question
    if actions:
        current = f"{actions}\n\n{current}"
    if first_user:
        current = f"{info}\n\n{current}"
    messages.append({"role": "user", "content": current})
    return messages
