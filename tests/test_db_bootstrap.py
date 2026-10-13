"""Роли и базы: шаг загрузчика под администратором сервера."""

from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from assistant.ingest.bootstrap import ensure_roles_and_databases, read_accounts
from assistant.ingest.startup import StartupError, connect_admin
from tests.conftest import OWNER_PASSWORD, SERVICE_PASSWORD, TOIR_PASSWORD, Database

pytestmark = pytest.mark.db


def _can_connect(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except OperationalError:
        return False
    finally:
        engine.dispose()
    return True


def test_second_run_changes_nothing(database: Database) -> None:
    with connect_admin(database.host, database.port, database.admin_password) as admin:
        before = admin.execute(
            "select rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls from pg_roles"
            " where rolname like 'test\\_%' order by 1"
        ).fetchall()
        ensure_roles_and_databases(admin, database.accounts)
        after = admin.execute(
            "select rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls from pg_roles"
            " where rolname like 'test\\_%' order by 1"
        ).fetchall()
    assert before == after
    assert [row[0] for row in after] == ["test_app", "test_owner", "test_toir"]
    # Ни одна роль не суперпользователь и не обходит построчную защиту.
    assert all(row[1:] == (False, False, False, False) for row in after)
    assert _can_connect(database.service_url)


def test_new_password_over_existing_volume_keeps_login_working(database: Database) -> None:
    # Новый .env поверх старого тома: пароль роли выставляется при каждом запуске.
    new_password = "test-rotated-password-marker-77f0"
    rotated = replace(
        database.accounts, service=replace(database.accounts.service, password=new_password)
    )
    with connect_admin(database.host, database.port, database.admin_password) as admin:
        ensure_roles_and_databases(admin, rotated)
        try:
            assert _can_connect(database.url("test_app", new_password, "assistant_test"))
            assert not _can_connect(database.service_url)
        finally:
            ensure_roles_and_databases(admin, database.accounts)
    assert _can_connect(database.service_url)


def test_connection_rights_are_granted_by_name(database: Database) -> None:
    assert _can_connect(database.url("test_app", SERVICE_PASSWORD, "assistant_test"))
    assert _can_connect(database.url("test_owner", OWNER_PASSWORD, "assistant_test"))
    assert _can_connect(database.url("test_toir", TOIR_PASSWORD, "toir_test"))
    # Роль сервиса не входит в базу заглушки, роль заглушки — в базу сервиса.
    assert not _can_connect(database.url("test_app", SERVICE_PASSWORD, "toir_test"))
    assert not _can_connect(database.url("test_owner", OWNER_PASSWORD, "toir_test"))
    assert not _can_connect(database.url("test_toir", TOIR_PASSWORD, "assistant_test"))


def test_toir_role_owns_its_database(database: Database) -> None:
    # Заглушка создаёт свою схему сама, под своей ролью.
    engine = create_engine(database.toir_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("create schema probe"))
            connection.execute(text("drop schema probe"))
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("owner", "service", "toir"),
    [
        ("postgresql+psycopg://a:p@h/x", "postgresql+psycopg://a:p@h/x", "postgresql://c:p@h/y"),
        ("postgresql+psycopg://a:p@h/x", "postgresql+psycopg://b:p@h/z", "postgresql://c:p@h/y"),
        ("postgresql+psycopg://a:p@h/x", "postgresql+psycopg://b:p@h/x", "postgresql://c:p@h/x"),
        (
            "postgresql+psycopg://a:p@h/x",
            "postgresql+psycopg://postgres:p@h/x",
            "postgresql://c:p@h/y",
        ),
        ("postgresql+psycopg://a@h/x", "postgresql+psycopg://b:p@h/x", "postgresql://c:p@h/y"),
        ("не адрес", "postgresql+psycopg://b:p@h/x", "postgresql://c:p@h/y"),
    ],
)
def test_inconsistent_addresses_are_rejected_without_echo(
    owner: str, service: str, toir: str
) -> None:
    with pytest.raises(StartupError) as caught:
        read_accounts(owner, service, toir)
    assert "p@h" not in str(caught.value)
