"""Роль базы с обходом построчной защиты — только для мутантов без политик.

Права на объекты — те же, что у роли сервиса; отличие одно — `bypassrls`. Роль создаётся
в базе тестов и в отдельном проекте compose для мутантов со своим томом; в базе запуска
`make run` её нет, и учётных данных этой роли у сервиса нет.
"""

import psycopg
from psycopg import sql
from sqlalchemy import Engine

from assistant.db.grants import grant_service
from assistant.ingest.bootstrap import Account


def ensure_bypass_role(admin: psycopg.Connection, owner: Engine, account: Account) -> None:
    """Идемпотентно: роль, пароль, вход в базу сервиса и права роли сервиса."""
    admin.execute("set log_statement = 'none'")
    admin.execute("set log_min_error_statement = 'panic'")
    role = sql.Identifier(account.role)
    exists = admin.execute("select 1 from pg_roles where rolname = %s", (account.role,)).fetchone()
    if exists is None:
        admin.execute(sql.SQL("create role {} login").format(role))
    admin.execute(
        sql.SQL(
            "alter role {} with login nosuperuser nocreatedb nocreaterole noreplication"
            " bypassrls password {}"
        ).format(role, sql.Literal(account.password))
    )
    admin.execute(
        sql.SQL("grant connect on database {} to {}").format(sql.Identifier(account.database), role)
    )
    with owner.begin() as connection:
        grant_service(connection, account.role)
