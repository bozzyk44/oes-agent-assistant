"""Роли и базы сервера: идемпотентный шаг загрузчика под администратором PostgreSQL.

Сценарии инициализации образа PostgreSQL выполняются только на пустом каталоге данных,
поэтому поверх существующего тома не сработали бы. Этот шаг работает одинаково на новом
и на существующем томе: создаёт недостающее и при каждом запуске выставляет ролям пароли
из окружения — иначе новый `.env` поверх старого тома дал бы отказ входа.
"""

from dataclasses import dataclass

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from assistant.ingest.startup import StartupError


@dataclass(frozen=True)
class Account:
    """Роль и её база из адреса подключения. Пароль в текст ошибок не попадает."""

    variable: str
    role: str
    password: str
    database: str


@dataclass(frozen=True)
class Accounts:
    owner: Account
    service: Account
    toir: Account


def read_account(variable: str, url: str) -> Account:
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise StartupError(f"{variable}: адрес базы не разобран") from exc
    if not (parsed.username and parsed.password and parsed.database):
        raise StartupError(f"{variable}: в адресе базы нужны роль, пароль и имя базы")
    return Account(variable, parsed.username, parsed.password, parsed.database)


def read_accounts(owner_url: str, service_url: str, toir_url: str) -> Accounts:
    accounts = Accounts(
        owner=read_account("DATABASE_OWNER_URL", owner_url),
        service=read_account("DATABASE_URL", service_url),
        toir=read_account("TOIR_DATABASE_URL", toir_url),
    )
    roles = {accounts.owner.role, accounts.service.role, accounts.toir.role}
    if len(roles) != 3 or "postgres" in roles:
        raise StartupError(
            "DATABASE_OWNER_URL, DATABASE_URL и TOIR_DATABASE_URL должны называть три разные"
            " роли, и ни одна из них — не администратор сервера"
        )
    if accounts.owner.database != accounts.service.database:
        raise StartupError("DATABASE_OWNER_URL и DATABASE_URL должны вести в одну базу")
    if accounts.toir.database == accounts.service.database:
        raise StartupError("TOIR_DATABASE_URL должен вести в отдельную базу")
    return accounts


def ensure_roles_and_databases(admin: psycopg.Connection, accounts: Accounts) -> None:
    """Создаёт роли и базы, если их нет, и раздаёт право подключения поимённо.

    Соединение администратора должно быть в режиме автофиксации: базу нельзя создать
    внутри транзакции.
    """
    # Команды с паролями не должны попасть в журнал сервера — ни как выполненные,
    # ни как упавшие с ошибкой.
    admin.execute("set log_statement = 'none'")
    admin.execute("set log_min_error_statement = 'panic'")
    admin.execute("set log_min_duration_statement = -1")
    for account in (accounts.owner, accounts.service, accounts.toir):
        role = sql.Identifier(account.role)
        exists = admin.execute(
            "select 1 from pg_roles where rolname = %s", (account.role,)
        ).fetchone()
        if exists is None:
            admin.execute(sql.SQL("create role {} login").format(role))
        # Пароль и запрет привилегий — при каждом запуске. В SQL-команду пароль входит
        # литералом: управляющие команды не принимают параметров.
        admin.execute(
            sql.SQL(
                "alter role {} with login nosuperuser nocreatedb nocreaterole"
                " noreplication nobypassrls password {}"
            ).format(role, sql.Literal(account.password))
        )

    service_db, toir_db = accounts.service.database, accounts.toir.database
    for database, owner in ((service_db, accounts.owner.role), (toir_db, accounts.toir.role)):
        exists = admin.execute(
            "select 1 from pg_database where datname = %s", (database,)
        ).fetchone()
        if exists is None:
            admin.execute(
                sql.SQL("create database {} owner {}").format(
                    sql.Identifier(database), sql.Identifier(owner)
                )
            )
        else:
            admin.execute(
                sql.SQL("alter database {} owner to {}").format(
                    sql.Identifier(database), sql.Identifier(owner)
                )
            )

    # Право подключения отозвано у всех и выдано поимённо: роль сервиса не входит
    # в базу заглушки, роль заглушки — в базу сервиса.
    allowed = {
        service_db: (accounts.owner.role, accounts.service.role),
        toir_db: (accounts.toir.role,),
    }
    for database, roles in allowed.items():
        name = sql.Identifier(database)
        admin.execute(sql.SQL("revoke all on database {} from public").format(name))
        for account in (accounts.owner, accounts.service, accounts.toir):
            role = sql.Identifier(account.role)
            if account.role in roles:
                admin.execute(sql.SQL("grant connect on database {} to {}").format(name, role))
            else:
                admin.execute(sql.SQL("revoke all on database {} from {}").format(name, role))
