"""Проверка миграций — отдельный шаг CI: `make migrate-check`.

Миграции применяются на пустую базу, повторно, откатываются до нуля и применяются снова;
модели сверяются со схемой; загрузчик набора на отставшей схеме останавливается. Работа
идёт в своей базе, созданной на время теста: общую базу тестов откат бы опустошил.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg import sql
from sqlalchemy import Connection, Engine, create_engine, inspect, text

from assistant.db import models
from assistant.db.grants import grant_service
from assistant.db.migrate import (
    LOCK_KEY,
    SchemaMismatch,
    current_revision,
    downgrade,
    head_revision,
    require_head,
    revisions,
    upgrade,
    upgrade_to,
)
from assistant.ingest.__main__ import load, main, migrate
from assistant.ingest.startup import StartupError, connect_admin
from tests.conftest import OWNER_PASSWORD, SERVICE_PASSWORD, Database
from tests.smallset import write_small_dataset

pytestmark = [pytest.mark.db, pytest.mark.migrations]

PROBE = "migrations_probe"


@pytest.fixture
def empty(database: Database) -> Iterator[Engine]:
    """Пустая база владельца: в ней нет ни одной миграции."""
    name = sql.Identifier(PROBE)
    with connect_admin(database.host, database.port, database.admin_password) as admin:
        admin.execute(sql.SQL("drop database if exists {} with (force)").format(name))
        admin.execute(sql.SQL("create database {} owner test_owner").format(name))
        admin.execute(sql.SQL("revoke all on database {} from public").format(name))
        admin.execute(sql.SQL("grant connect on database {} to test_app").format(name))
    engine = create_engine(database.url("test_owner", OWNER_PASSWORD, PROBE))
    yield engine
    engine.dispose()
    with connect_admin(database.host, database.port, database.admin_password) as admin:
        admin.execute(sql.SQL("drop database if exists {} with (force)").format(name))


def _schemas(engine: Engine) -> set[str]:
    return set(inspect(engine).get_schema_names()) & {"app", "dwh", "kb"}


def _queries() -> tuple[str, ...]:
    """Слепок схемы: всё, что миграция создаёт и что откат обязан убрать или вернуть.

    Кроме трёх схем проекта в слепок входят объекты схемы `public`: миграция, которая
    оставит там тип или последовательность, тоже должна быть замечена.
    """
    queries = (
        "select 'column', table_schema, table_name, column_name, data_type, is_nullable,"
        " coalesce(column_default, ''), is_identity, coalesce(generation_expression, '')"
        " from information_schema.columns where table_schema in OURS"
        " and table_name <> 'alembic_version'",
        # Флаги построчной защиты — на самой таблице, а не в политиках.
        "select 'table', n.nspname, c.relname, c.relkind::text, c.relrowsecurity::text,"
        " c.relforcerowsecurity::text, coalesce(c.relacl::text, ''), '', ''"
        " from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname in OURS and c.relkind in ('r', 'S', 'v', 'm')"
        " and c.relname <> 'alembic_version'",
        "select 'index', schemaname, tablename, indexname, indexdef, '', '', '', ''"
        " from pg_indexes where schemaname in OURS and tablename <> 'alembic_version'",
        "select 'policy', schemaname, tablename, policyname, cmd, coalesce(qual, ''),"
        " coalesce(with_check, ''), roles::text, permissive from pg_policies",
        "select 'function', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),"
        " pg_get_functiondef(p.oid), coalesce(p.proacl::text, ''), '', '', ''"
        " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname in OURS",
        "select 'trigger', event_object_schema, event_object_table, trigger_name,"
        " event_manipulation, action_timing, action_statement, '', ''"
        " from information_schema.triggers",
        "select 'constraint', n.nspname, c.conrelid::regclass::text, c.conname, c.contype::text,"
        " pg_get_constraintdef(c.oid), '', '', '' from pg_constraint c"
        " join pg_namespace n on n.oid = c.connamespace where n.nspname in OURS"
        " and c.conrelid::regclass::text <> 'alembic_version'",
        "select 'type', n.nspname, t.typname, t.typtype::text, '', '', '', '', ''"
        " from pg_type t join pg_namespace n on n.oid = t.typnamespace"
        " where n.nspname in OURS and t.typtype in ('e', 'd')",
    )
    return tuple(query.replace("OURS", "('app', 'dwh', 'kb', 'public')") for query in queries)


def _snapshot_in(connection: Connection) -> list[tuple[str, ...]]:
    return [
        tuple(str(cell) for cell in row)
        for query in _queries()
        for row in connection.execute(text(query))
    ]


def _objects(engine: Engine) -> list[tuple[str, ...]]:
    with engine.connect() as connection:
        return sorted(_snapshot_in(connection))


def test_snapshot_sees_what_a_careless_migration_could_lose(empty: Engine) -> None:
    # Слепок годен как проверка отката, только если замечает каждое из этих изменений.
    with empty.begin() as connection:
        upgrade(connection)
    base = _objects(empty)
    kinds = {row[0] for row in base}
    assert {"column", "table", "index", "policy", "function", "trigger", "constraint"} <= kinds
    changes = (
        "drop index kb.chunks_tsv",
        "alter table dwh.production disable row level security",
        "alter table app.actions alter column status drop default",
        "grant execute on function kb.document_exists(text) to public",
        "alter function kb.document_exists(text) reset search_path",
        "drop trigger actions_guard on app.actions",
        "alter policy sessions_by_owner on app.sessions with check (true)",
        "create sequence public.leftover",
        "create type public.leftover as enum ('a')",
    )
    for change in changes:
        with empty.connect() as connection:
            transaction = connection.begin()
            connection.execute(text(change))
            rows = _snapshot_in(connection)
            transaction.rollback()
        assert sorted(rows) != base, change
    assert _objects(empty) == base


def test_migrations_apply_to_empty_database_and_repeat_changes_nothing(empty: Engine) -> None:
    with empty.connect() as connection:
        assert current_revision(connection) is None
    assert _schemas(empty) == set()
    with empty.begin() as connection:
        assert upgrade(connection) == (None, head_revision())
    with empty.connect() as connection:
        assert current_revision(connection) == head_revision()
    assert _schemas(empty) == {"app", "dwh", "kb"}
    first = _objects(empty)
    assert first
    with empty.begin() as connection:
        # Повторное применение — пустой шаг.
        assert upgrade(connection) == (head_revision(), head_revision())
    assert _objects(empty) == first


def test_each_migration_is_undone_by_its_own_downgrade(empty: Engine) -> None:
    # Откат проверяется по одной версии: откат до нуля скрыл бы неполный откат поздней
    # миграции за каскадным удалением схем в первой.
    applied = revisions()
    assert applied[-1] == head_revision()
    for revision in applied:
        before = _objects(empty)
        with empty.begin() as connection:
            upgrade_to(connection, revision)
        after = _objects(empty)
        assert after != before, f"миграция {revision} ничего не меняет в схеме"
        with empty.begin() as connection:
            downgrade(connection, "-1")
        assert _objects(empty) == before, f"откат {revision} не вернул схему к прежнему виду"
        with empty.begin() as connection:
            upgrade_to(connection, revision)
        # Повторное применение после отката даёт ту же схему, что и первое.
        assert _objects(empty) == after, revision
    with empty.connect() as connection:
        assert current_revision(connection) == head_revision()


def test_downgrade_to_base_leaves_nothing(empty: Engine) -> None:
    with empty.begin() as connection:
        upgrade(connection)
    with empty.begin() as connection:
        downgrade(connection)
    with empty.connect() as connection:
        assert current_revision(connection) is None
    assert _schemas(empty) == set()
    assert _objects(empty) == []


def test_migration_holds_a_lock_until_commit(database: Database, empty: Engine) -> None:
    # Два шага миграций одновременно идут по очереди: блокировка берётся до чтения версии
    # и держится до конца транзакции.
    held = text(
        "select count(*) from pg_locks where locktype = 'advisory' and objid = :key and granted"
    )
    observer = create_engine(database.url("test_owner", OWNER_PASSWORD, PROBE))
    try:
        with empty.begin() as connection:
            upgrade(connection)
            with observer.connect() as other:
                assert other.scalar(held, {"key": LOCK_KEY}) == 1
        with observer.connect() as other:
            assert other.scalar(held, {"key": LOCK_KEY}) == 0
    finally:
        observer.dispose()


def test_models_match_migrated_schema(empty: Engine) -> None:
    with empty.begin() as connection:
        upgrade(connection)
    inspector = inspect(empty)
    in_database = {
        f"{schema}.{table}"
        for schema in ("app", "dwh", "kb")
        for table in inspector.get_table_names(schema)
    }
    # Таблица без модели и модель без таблицы — одинаково расхождение.
    assert in_database == set(models.Base.metadata.tables)
    for table in models.Base.metadata.tables.values():
        columns = {col["name"]: col for col in inspector.get_columns(table.name, table.schema)}
        assert set(columns) == {column.name for column in table.columns}, table.fullname
        for column in table.columns:
            if column.name == "tsv":
                continue
            assert columns[column.name]["nullable"] == column.nullable, (
                f"{table.fullname}.{column.name}"
            )
        primary = inspector.get_pk_constraint(table.name, table.schema)["constrained_columns"]
        assert set(primary) == {column.name for column in table.primary_key}, table.fullname


def test_migrate_step_reports_what_it_did(database: Database, empty: Engine) -> None:
    env = {
        "DATABASE_OWNER_URL": database.url("test_owner", OWNER_PASSWORD, PROBE),
        "DATABASE_URL": database.url("test_app", SERVICE_PASSWORD, PROBE),
    }
    assert migrate(env) == f"миграции применены: пустая база -> {head_revision()}"
    assert migrate(env) == f"схема уже на последней версии: {head_revision()}"
    # Права роли сервиса выданы тем же шагом.
    service = create_engine(env["DATABASE_URL"])
    try:
        with service.connect() as connection:
            assert connection.scalar(text("select count(*) from app.users")) == 0
    finally:
        service.dispose()


def test_loader_stops_on_outdated_schema(
    database: Database,
    empty: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Загрузчик набора миграции не применяет: на базе без них он останавливается
    # и называет команду, которая доведёт схему.
    env = {
        "DATABASE_OWNER_URL": database.url("test_owner", OWNER_PASSWORD, PROBE),
        "DATA_DIR": str(write_small_dataset(tmp_path)),
    }
    with pytest.raises(SchemaMismatch, match="make migrate"):
        load(env)
    assert _schemas(empty) == set()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert main(["load"]) == 1
    message = capsys.readouterr().err
    assert "load: схема базы не на последней версии" in message
    assert OWNER_PASSWORD not in message

    with empty.begin() as connection:
        upgrade(connection)
        grant_service(connection, database.accounts.service.role)
        require_head(connection)
    assert load(env).startswith("набор загружен")


def test_database_newer_than_code_stops_both_steps(
    database: Database,
    empty: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Базу мигрировала более новая сборка: её версии нет среди миграций этой. Это не
    # «схема отстала», и совет применить миграции был бы неверным.
    with empty.begin() as connection:
        upgrade(connection)
        connection.execute(text("update alembic_version set version_num = '9999_future'"))
    env = {
        "DATABASE_OWNER_URL": database.url("test_owner", OWNER_PASSWORD, PROBE),
        "DATABASE_URL": database.url("test_app", SERVICE_PASSWORD, PROBE),
        "DATA_DIR": str(write_small_dataset(tmp_path)),
    }
    for step in (migrate, load):
        with pytest.raises(SchemaMismatch, match="база новее кода") as caught:
            step(env)
        assert "make migrate" not in str(caught.value)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert main(["migrate"]) == 1
    message = capsys.readouterr().err
    assert message.startswith("migrate: в базе версия схемы 9999_future")
    assert "Traceback" not in message
    with empty.connect() as connection:
        assert current_revision(connection) == "9999_future"


@pytest.mark.parametrize(
    ("service", "message"),
    [
        (("test_app", SERVICE_PASSWORD, "assistant_test"), "в одну базу"),
        (("test_owner", OWNER_PASSWORD, PROBE), "две разные роли"),
        (("postgres", "x", PROBE), "не администратор"),
    ],
)
def test_migrate_step_checks_its_addresses(
    database: Database, empty: Engine, service: tuple[str, str, str], message: str
) -> None:
    # Шаг запускается и отдельно, поэтому сам сверяет, кому и в какой базе выдаёт права.
    env = {
        "DATABASE_OWNER_URL": database.url("test_owner", OWNER_PASSWORD, PROBE),
        "DATABASE_URL": database.url(*service),
    }
    with pytest.raises(StartupError, match=message):
        migrate(env)
    with empty.connect() as connection:
        assert current_revision(connection) is None
