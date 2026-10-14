"""Права роли сервиса на объекты схемы.

Выдаются шагом миграций при каждом запуске, а не самой миграцией: имя роли приходит из окружения
и может смениться поверх существующего тома базы. Роль сервиса таблицами не владеет:
полномочия пользователей читает, но не меняет; набор читает под построчной защитой;
реестр документов не видит вовсе и узнаёт о существовании документа только функцией.
"""

from psycopg import sql
from sqlalchemy import Connection

_GRANTS = (
    "grant usage on schema app, dwh, kb to {role}",
    "grant select on app.users, app.user_groups, app.kpi_grants, app.repair_grants,"
    " app.dataset_version to {role}",
    # Версия схемы — для проверки готовности: сервис не отвечает «готов» на чужой схеме.
    "grant select on public.alembic_version to {role}",
    "grant select, insert, update on app.sessions, app.actions, app.token_calibration to {role}",
    "grant select, insert on app.turns, app.messages to {role}",
    "grant select on dwh.production, dwh.downtime, dwh.equipment, kb.chunks to {role}",
    "grant execute on function kb.document_exists(text), dwh.equipment_site(text),"
    " kb.best_match_is_closed(text), dwh.equipment_id(text) to {role}",
)


def grant_service(connection: Connection, role: str) -> None:
    """Идемпотентно: повторная выдача тех же прав ничего не меняет."""
    name = sql.Identifier(role).as_string(None)
    for statement in _GRANTS:
        connection.exec_driver_sql(statement.format(role=name))
