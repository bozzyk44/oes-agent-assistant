"""Права роли сервиса на объекты схемы. Сами миграции проверяет `test_db_migrations.py`."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import Database

pytestmark = pytest.mark.db


def test_service_role_owns_nothing(database: Database) -> None:
    with database.service.connect() as connection:
        owned = connection.scalar(
            text(
                "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname in ('app', 'dwh', 'kb', 'public')"
                " and pg_get_userbyid(c.relowner) = current_user"
            )
        )
    assert owned == 0


@pytest.mark.parametrize(
    "statement",
    [
        "insert into app.users values ('u-x', 'x', 'x')",
        "update app.users set name = 'x'",
        "delete from app.users",
        "insert into app.user_groups values ('u-eng', 'hr')",
        "insert into app.kpi_grants values ('u-hr', 'production', 'K1')",
        "insert into app.repair_grants values ('u-hr', 'K1')",
        "update app.dataset_version set content_hash = 'x'",
        "delete from app.actions",
        "delete from app.sessions",
        "update app.turns set answer = 'x'",
        "update dwh.production set tonnes = 0",
        "insert into dwh.equipment (equipment_id, type, model, site, motor_hours,"
        " last_ppr_motor_hours) values ('X', 'x', 'x', 'K1', 1, 1)",
        "delete from kb.chunks",
        "select doc_id from kb.documents",
        "create table app.probe (x int)",
        "create table public.probe (x int)",
        "alter table dwh.production disable row level security",
        "set role test_owner",
    ],
)
def test_service_role_cannot(database: Database, small: object, statement: str) -> None:
    # Полномочия и набор роль сервиса читает, но не меняет; схемой не распоряжается.
    with database.service.connect() as connection, pytest.raises(DBAPIError) as caught:
        connection.execute(text(statement))
    assert "permission denied" in str(caught.value) or "must be owner" in str(caught.value)


def test_service_role_reads_grants(database: Database, small: object) -> None:
    with database.service.connect() as connection:
        assert connection.scalar(text("select count(*) from app.users")) == 4
        assert connection.scalar(text("select count(*) from app.kpi_grants")) == 11
        assert connection.scalar(text("select count(*) from app.repair_grants")) == 3
        assert connection.scalar(text("select count(*) from app.user_groups")) == 4


@pytest.mark.parametrize("lexeme", ["слово", "a\\b'c", "'", "\\", "a:b&c|d!e", "д'Артаньян"])
def test_lexeme_is_quoted_for_tsquery(database: Database, lexeme: str) -> None:
    # Любая строка в записи лексемы приводится к запросу без ошибки и остаётся собой.
    with database.owner.connect() as connection:
        query = connection.scalar(text("select kb.lexeme(:l)::tsquery::text"), {"l": lexeme})
        same = connection.scalar(
            text("select array_to_tsvector(array[cast(:l as text)]) @@ kb.lexeme(:l)::tsquery"),
            {"l": lexeme},
        )
    assert query
    assert same is True
    with database.service.connect() as connection, pytest.raises(DBAPIError, match="permission"):
        connection.execute(text("select kb.lexeme('x')"))


def test_bypass_functions_are_callable_only_by_service_role(database: Database) -> None:
    functions = (
        "kb.document_exists(text)",
        "dwh.equipment_site(text)",
        "kb.best_match_is_closed(text)",
        "dwh.equipment_id(text)",
    )
    with database.owner.connect() as connection:
        for function in functions:
            row = connection.execute(
                text(
                    "select has_function_privilege('test_app', :f, 'execute'),"
                    " has_function_privilege('test_toir', :f, 'execute'),"
                    " has_function_privilege('public', :f, 'execute'),"
                    " p.prosecdef, p.proconfig"
                    " from pg_proc p where p.oid = cast(:f as regprocedure)"
                ),
                {"f": function},
            ).one()
            assert row[:3] == (True, False, False), function
            # Права владельца и фиксированный путь поиска схем.
            assert row[3] is True, function
            assert row[4] == ["search_path=pg_catalog, pg_temp"], function
