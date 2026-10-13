"""Построчная защита: запросы под ролью сервиса напрямую, в обход слоя доступа.

Тест кода доступа убеждается, что запрос построен верно. Эти тесты убеждаются, что
неверно построенный запрос всё равно ничего не отдаст.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from assistant.db.binding import bind_user
from datagen.world import World
from tests.conftest import Database
from tests.smallset import DOCS, USERS

pytestmark = pytest.mark.db

KPI = ("production", "downtime", "equipment")
PROTECTED = ("dwh.production", "dwh.downtime", "dwh.equipment", "kb.chunks", "app.sessions")


def _sites(connection: Connection, table: str) -> list[str]:
    return sorted(connection.scalars(text(f"select site from dwh.{table}")).all())  # noqa: S608


def _docs(connection: Connection) -> set[str]:
    return set(connection.scalars(text("select distinct doc_id from kb.chunks")).all())


def _file_sites(root: Path, table: str, allowed: list[str]) -> list[str]:
    lines = (root / f"{table}.csv").read_text(encoding="utf-8").splitlines()
    column = lines[0].split(",").index("site")
    sites = [line.split(",")[column] for line in lines[1:]]
    return sorted(site for site in sites if site in allowed)


@pytest.mark.parametrize("user", USERS, ids=lambda user: user["user_id"])
def test_user_sees_only_granted_rows(database: Database, small: Path, user: dict) -> None:  # type: ignore[type-arg]
    with database.service.begin() as connection:
        bind_user(connection, user["user_id"])
        for table in KPI:
            allowed = user["kpi"].get(table, [])
            assert _sites(connection, table) == _file_sites(small, table, allowed), table
        expected = {
            doc for doc, acl in DOCS.items() if "all" in acl or set(acl) & set(user["groups"])
        }
        assert _docs(connection) == expected


def test_every_user_of_generated_dataset_sees_only_granted_rows(
    database: Database, dataset: tuple[Path, World]
) -> None:
    # Инвариант на любом наборе: ожидание считается из файлов набора, а не из его знания.
    root, world = dataset
    database.load(root)
    acl = {doc.doc_id: set(doc.acl) for doc in world.docs.values()}
    users = json.loads((root / "users.json").read_text(encoding="utf-8"))
    for user in users:
        with database.service.begin() as connection:
            bind_user(connection, user["user_id"])
            for table in KPI:
                allowed = user["kpi"].get(table, [])
                assert _sites(connection, table) == _file_sites(root, table, allowed)
            groups = {"all", *user["groups"]}
            assert _docs(connection) == {doc for doc, value in acl.items() if value & groups}


@pytest.mark.parametrize("binding", [None, "", "u-nobody"], ids=["нет", "пустая", "неизвестный"])
def test_without_user_nothing_is_visible(
    database: Database, small: Path, binding: str | None
) -> None:
    # Забытая привязка даёт пустой результат, а не полный — включая документы для всех.
    with database.service.begin() as connection:
        if binding is not None:
            bind_user(connection, binding)
        for table in PROTECTED:
            assert connection.scalar(text(f"select count(*) from {table}")) == 0, table  # noqa: S608


def test_binding_does_not_outlive_transaction(database: Database, small: Path) -> None:
    with database.service.connect() as connection:
        with connection.begin():
            bind_user(connection, "u-eng")
            assert connection.scalar(text("select count(*) from dwh.production")) == 5
            first = connection.scalar(text("select pg_backend_pid()"))
        with connection.begin():
            # То же соединение, новая транзакция: привязки нет, параметр читается пустым.
            assert connection.scalar(text("select pg_backend_pid()")) == first
            assert connection.scalar(text("select current_setting('app.user_id', true)")) == ""
            assert connection.scalar(text("select count(*) from dwh.production")) == 0


def test_binding_is_passed_as_value(database: Database, small: Path) -> None:
    # Идентификатор с кавычкой остаётся значением: текст запроса он не меняет.
    with database.service.begin() as connection:
        bind_user(connection, "u-eng' or '1'='1")
        assert connection.scalar(text("select count(*) from dwh.production")) == 0


def test_sessions_turns_and_messages_belong_to_owner(database: Database, small: Path) -> None:
    turn = (
        "insert into app.turns (turn_id, user_id, session_id, question, outcome, answer,"
        " model_calls, input_tokens, output_tokens, max_call_input_tokens, prefix_hash,"
        " latency_ms) values (:t, :u, 's-1', 'вопрос', 'answer', 'ответ', 1, 10, 5, 10, 'h', 1)"
    )
    message = (
        "insert into app.messages (user_id, session_id, turn_id, seq, role, content)"
        " values (:u, 's-1', :t, 1, 'user', '{}')"
    )
    with database.service.begin() as connection:
        bind_user(connection, "u-eng")
        connection.execute(
            text("insert into app.sessions (user_id, session_id) values ('u-eng', 's-1')")
        )
        connection.execute(text(turn), {"t": "t-rls-eng", "u": "u-eng"})
        connection.execute(text(message), {"t": "t-rls-eng", "u": "u-eng"})
    with database.service.begin() as connection:
        # Тот же session_id у другого пользователя — другая сессия: чужой он не видит.
        bind_user(connection, "u-k1")
        for table in ("sessions", "turns", "messages"):
            assert connection.scalar(text(f"select count(*) from app.{table}")) == 0  # noqa: S608
        connection.execute(
            text("insert into app.sessions (user_id, session_id) values ('u-k1', 's-1')")
        )
        changed = connection.execute(text("update app.sessions set busy_until = null"))
        assert changed.rowcount == 1
    for statement, params in (
        ("insert into app.sessions (user_id, session_id) values ('u-eng', 's-2')", {}),
        (turn, {"t": "t-rls-foreign", "u": "u-eng"}),
        (message, {"t": "t-rls-eng", "u": "u-eng"}),
    ):
        with database.service.begin() as connection:
            bind_user(connection, "u-k1")
            with pytest.raises(DBAPIError, match="row-level security"):
                connection.execute(text(statement), params)
    with database.service.begin() as connection:
        bind_user(connection, "u-eng")
        mine = "select count(*) from app.{} where session_id in ('s-1', 's-2')"
        assert connection.scalar(text(mine.format("sessions"))) == 1
        assert connection.scalar(text(mine.format("messages"))) == 1


def test_document_exists_does_not_depend_on_access(database: Database, small: Path) -> None:
    # Отличает «доступа нет» от «не найдено»; содержимого не возвращает.
    with database.service.begin() as connection:
        bind_user(connection, "u-k1")
        assert connection.scalar(text("select kb.document_exists('H1')")) is True
        assert connection.scalar(text("select kb.document_exists('H9')")) is False
        assert _docs(connection) == {"A1", "R1", "R2"}


def test_equipment_site_is_known_without_table_grant(database: Database, small: Path) -> None:
    # Право на заявку задаётся отдельно от права на таблицу оборудования.
    with database.service.begin() as connection:
        bind_user(connection, "u-hr")
        assert connection.scalar(text("select count(*) from dwh.equipment")) == 0
        assert connection.scalar(text("select dwh.equipment_site('EX-2')")) == "K2"
        assert connection.scalar(text("select dwh.equipment_site('EX-9')")) is None


@pytest.mark.parametrize(
    ("user", "query", "expected"),
    [
        ("u-k1", "какой оклад у горного мастера", True),
        ("u-eng", "премия горного мастера", True),
        ("u-hr", "какой оклад у горного мастера", False),
        # Числа в сравнении не участвуют: подобрать оклад перебором нельзя.
        ("u-k1", "123450", False),
        ("u-k1", "123450 рублей", False),
        # Совпадение одного слова признаком не считается.
        ("u-k1", "оклад", False),
        # Лучшее совпадение — в доступном документе.
        ("u-k1", "диспетчер отвечает по рации", False),
        ("u-k1", "", False),
        ("u-k1", "совершенно посторонний запрос", False),
        # Тому, кого приложение не знает, функция не отвечает — как и политики.
        ("u-nobody", "какой оклад у горного мастера", False),
        ("", "какой оклад у горного мастера", False),
    ],
)
def test_best_match_is_closed(
    database: Database, small: Path, user: str, query: str, expected: bool
) -> None:
    with database.service.begin() as connection:
        bind_user(connection, user)
        assert (
            connection.scalar(text("select kb.best_match_is_closed(:q)"), {"q": query}) is expected
        )


def test_best_match_flag_ignores_accessible_results(database: Database, small: Path) -> None:
    # Признак не зависит от того, пуста ли доступная выдача: слова запроса есть
    # и в доступном документе, и в закрытом, но лучшее совпадение — закрытое.
    query = "оклад горного мастера и ёмкость экскаватора"
    with database.service.begin() as connection:
        bind_user(connection, "u-k1")
        visible = connection.scalar(
            text(
                "select count(*) from kb.chunks where tsv @@ plainto_tsquery('russian', 'емкость')"
            )
        )
        assert visible == 1
        assert connection.scalar(text("select kb.best_match_is_closed(:q)"), {"q": query}) is True
    with database.service.begin() as connection:
        assert connection.scalar(text("select kb.best_match_is_closed(:q)"), {"q": query}) is False
