"""Слой доступа: строки таблиц «Барьер в коде» и «Статусы инструментов».

Каждый тест идёт дважды. «Код» — под владельцем таблиц, на которого построчная защита
не действует: проверяется, что условие по правам стоит в самом запросе и без второго
барьера. «Код и база» — под ролью сервиса с привязкой к пользователю, как в работе.
Отдельно — что база держит права, когда код ошибся.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, event, text

from assistant.access import (
    Filter,
    Grants,
    KpiQuery,
    Status,
    check_repair,
    get_document,
    load_grants,
    query_rows,
    search,
)
from assistant.db.binding import bind_user
from tests.conftest import Database

pytestmark = pytest.mark.db

Barrier = Any


@pytest.fixture(params=["код", "код и база"])
def connect(request: pytest.FixtureRequest, database: Database, small: Path) -> Barrier:
    """Открывает транзакцию от имени пользователя на выбранных барьерах."""

    @contextmanager
    def opened(user_id: str) -> Iterator[tuple[Connection, Grants]]:
        engine = database.owner if request.param == "код" else database.service
        with engine.begin() as connection:
            bind_user(connection, user_id)
            grants = load_grants(connection, user_id)
            assert grants is not None
            yield connection, grants

    return opened


def _open(connect: Barrier, user: str) -> AbstractContextManager[tuple[Connection, Grants]]:
    opened: AbstractContextManager[tuple[Connection, Grants]] = connect(user)
    return opened


def _docs(chunks: tuple[Any, ...]) -> set[str]:
    return {chunk.doc_id for chunk in chunks}


# --- Барьер в коде -------------------------------------------------------------------------


def test_document_search_ranks_only_permitted_chunks(connect: Barrier) -> None:
    # Лучшее совпадение среди всех документов — закрытое: так говорит функция базы.
    # При отборе лучших до проверки прав выдача из одного фрагмента была бы пустой;
    # условие в запросе отдаёт лучший из разрешённых.
    query = "оклад горного мастера и ёмкость экскаватора"
    with _open(connect, "u-k1") as (connection, grants):
        found = search(connection, grants, query, limit=1)
    assert found.status is Status.OK
    assert [chunk.chunk_id for chunk in found.chunks] == ["A1#1"]
    assert found.best_match_closed is True
    with _open(connect, "u-hr") as (connection, grants):
        assert "H1" in _docs(search(connection, grants, query).chunks)


def test_document_or_chunk_by_identifier(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        for ref in ("H1", "H1#1", " H1 "):
            closed = get_document(connection, grants, ref)
            # Ни названия, ни даты, ни текста.
            assert (closed.status, closed.chunks) == (Status.DENIED, ()), ref
        whole = get_document(connection, grants, "A1")
        one = get_document(connection, grants, "A1#1")
    assert whole.status is Status.OK
    assert [chunk.chunk_id for chunk in whole.chunks] == ["A1#0", "A1#1", "A1#2"]
    assert [chunk.chunk_id for chunk in one.chunks] == ["A1#1"]


def test_kpi_table_rows(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        everything = query_rows(connection, grants, KpiQuery("production"))
        # Параметрами условие по участку не снять: названный чужой участок только отказан.
        widened = query_rows(
            connection, grants, KpiQuery("production", (Filter("site", "in", ["K1", "K2"]),))
        )
    assert everything.status is Status.OK
    assert {row["site"] for row in everything.rows} == {"K1"}
    assert everything.sites == ("K1",)
    assert [row["tonnes"] for row in widened.rows] == [100, 110]
    assert (widened.sites, widened.denied_sites) == (("K1",), ("K2",))


def test_kpi_table_without_grant_is_denied_and_not_queried(connect: Barrier) -> None:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    for user, table in (("u-hr", "production"), ("u-fin", "downtime"), ("u-fin", "equipment")):
        statements.clear()
        with _open(connect, user) as (connection, grants):
            event.listen(connection, "before_cursor_execute", record)
            result = query_rows(connection, grants, KpiQuery(table))
            event.remove(connection, "before_cursor_execute", record)
        assert (result.status, result.rows) == (Status.DENIED, ()), (user, table)
        assert statements == [], (user, table)


def test_repair_request(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        ok = check_repair(connection, grants, " EX-1 ")
        foreign = check_repair(connection, grants, "EX-2")
    assert (ok.status, ok.site) == (Status.OK, "K1")
    # Отказ не сообщает, на каком участке оборудование.
    assert (foreign.status, foreign.site) == (Status.DENIED, None)
    for user in ("u-fin", "u-hr"):
        with _open(connect, user) as (connection, grants):
            assert check_repair(connection, grants, "EX-1").status is Status.DENIED
    with _open(connect, "u-eng") as (connection, grants):
        assert check_repair(connection, grants, "EX-2").status is Status.OK


# --- Статусы инструментов ------------------------------------------------------------------


def test_status_ok(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        assert search(connection, grants, "диспетчер рация").status is Status.OK
        assert get_document(connection, grants, "R2").status is Status.OK
        rows = query_rows(
            connection, grants, KpiQuery("downtime", (Filter("equipment_id", "eq", "EX-1"),))
        )
    assert rows.status is Status.OK
    assert [row["equipment_id"] for row in rows.rows] == ["EX-1"]


def test_status_denied(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        cases = {
            "документ по идентификатору": get_document(connection, grants, "H1").status,
            "все названные участки": query_rows(
                connection, grants, KpiQuery("production", (Filter("site", "eq", "K2"),))
            ).status,
            "оборудование чужого участка в фильтре": query_rows(
                connection, grants, KpiQuery("downtime", (Filter("equipment_id", "eq", "EX-2"),))
            ).status,
            "заявка на чужой участок": check_repair(connection, grants, "EX-2").status,
        }
    with _open(connect, "u-hr") as (connection, grants):
        cases["нет полномочий на таблицу"] = query_rows(
            connection, grants, KpiQuery("production")
        ).status
    assert set(cases.values()) == {Status.DENIED}, cases


def test_status_not_found(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        assert get_document(connection, grants, "H9").status is Status.NOT_FOUND
        assert get_document(connection, grants, "").status is Status.NOT_FOUND
        # Документ доступен, такого раздела в нём нет.
        assert get_document(connection, grants, "A1#99").status is Status.NOT_FOUND
        assert check_repair(connection, grants, "EX-9").status is Status.NOT_FOUND
        missing = query_rows(
            connection, grants, KpiQuery("equipment", (Filter("equipment_id", "eq", "EX-9"),))
        )
    assert missing.status is Status.NOT_FOUND


def test_status_empty(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        # Второго января добыча есть только на чужом участке: у пользователя строк нет,
        # и это законное «пусто», а не отказ.
        day = query_rows(
            connection, grants, KpiQuery("production", (Filter("date", "eq", "2026-01-02"),))
        )
        # Своё оборудование, но простоев по нему нет.
        reason = query_rows(
            connection,
            grants,
            KpiQuery(
                "downtime",
                (Filter("equipment_id", "eq", "EX-1"), Filter("reason", "eq", "погода")),
            ),
        )
        nothing = search(connection, grants, "совершенно посторонний запрос")
    assert (day.status, day.denied_sites, day.sites) == (Status.EMPTY, (), ("K1",))
    assert (reason.status, reason.denied_equipment) == (Status.EMPTY, ())
    assert (nothing.status, nothing.best_match_closed) == (Status.EMPTY, False)


@pytest.mark.parametrize(
    "query",
    [
        KpiQuery("salaries"),
        KpiQuery("production", (Filter("row_id", "eq", 1),)),
        KpiQuery("production", (Filter("equipment_id", "eq", "EX-1"),)),
        KpiQuery("production", (Filter("tonnes", "gt", "много"),)),
        KpiQuery("production", (Filter("tonnes", "eq", True),)),
        KpiQuery("production", (Filter("date", "gte", "вчера"),)),
        KpiQuery("production", (Filter("site", "gt", "K1"),)),
        KpiQuery("production", (Filter("site", "in", "K1"),)),
        KpiQuery("production", (Filter("site", "eq", 1),)),
        KpiQuery("production", (Filter("site", "like", "K%"),)),  # type: ignore[arg-type]
        KpiQuery("production", (Filter("shift", "eq", 40_000),)),
        KpiQuery("production", (Filter("tonnes", "gt", "99999999999999999999"),)),
        KpiQuery("downtime", (Filter("start", "gte", "2026-01-01T00:00+03:00"),)),
        KpiQuery("production", (Filter(["site"], "eq", "K1"),)),  # type: ignore[arg-type]
        KpiQuery("production", (Filter("site", ["eq"], "K1"),)),  # type: ignore[arg-type]
        KpiQuery(["production"]),  # type: ignore[arg-type]
        KpiQuery(
            "downtime",
            (Filter("equipment_id", "eq", "EX-1"), Filter("equipment_id", "eq", "T-1")),
        ),
        KpiQuery("production", limit=0),
        KpiQuery("production", limit=10_000),
    ],
)
def test_status_invalid(connect: Barrier, query: KpiQuery) -> None:
    with _open(connect, "u-eng") as (connection, grants):
        result = query_rows(connection, grants, query)
    assert result.status is Status.INVALID
    assert result.problem and result.rows == ()


# --- Смешанные запросы и оборудование ------------------------------------------------------


def test_mixed_sites_give_own_data_and_list_denied(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        result = query_rows(
            connection,
            grants,
            KpiQuery(
                "downtime",
                (
                    Filter("site", "in", ["K1", "K2"]),
                    Filter("equipment_id", "in", [" T-1 ", "EX-2", "EX-9"]),
                ),
            ),
        )
    assert result.status is Status.OK
    assert [row["equipment_id"] for row in result.rows] == ["T-1"]
    assert (result.denied_sites, result.denied_equipment) == (("K2",), ("EX-2",))
    # Неизвестное оборудование не теряется молча: модель узнает, что его нет.
    assert result.missing_equipment == ("EX-9",)
    assert result.partially_denied


@pytest.mark.parametrize("written", ["K1", " k1 ", "К1", "к1"])
def test_site_code_is_matched_regardless_of_case_space_and_lookalike_letters(
    connect: Barrier, written: str
) -> None:
    # Модель переносит код из русского вопроса: кириллическая «К1» — свой участок, а не чужой.
    with _open(connect, "u-k1") as (connection, grants):
        result = query_rows(
            connection, grants, KpiQuery("production", (Filter("site", "eq", written),))
        )
    assert (result.status, result.sites, result.denied_sites) == (Status.OK, ("K1",), ())


@pytest.mark.parametrize("written", ["К2", "k2"])
def test_foreign_site_written_differently_is_still_denied(connect: Barrier, written: str) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        result = query_rows(
            connection, grants, KpiQuery("production", (Filter("site", "eq", written),))
        )
    assert result.status is Status.DENIED


def test_unknown_site_code_is_invalid_and_lists_all_codes(connect: Barrier) -> None:
    # Название вместо кода — неверный аргумент. В ответе все коды участков, а не только
    # свои: модель повторит вызов с чужим кодом и получит отказ, а не данные своего.
    with _open(connect, "u-k1") as (connection, grants):
        result = query_rows(
            connection, grants, KpiQuery("production", (Filter("site", "eq", "Карьер-2"),))
        )
        retried = query_rows(
            connection, grants, KpiQuery("production", (Filter("site", "eq", "K2"),))
        )
    assert result.status is Status.INVALID
    assert "K1" in result.problem and "K2" in result.problem
    assert retried.status is Status.DENIED


def test_site_filters_intersect_after_mapping_to_codes(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        result = query_rows(
            connection,
            grants,
            KpiQuery("production", (Filter("site", "eq", "K1"), Filter("site", "in", ["к1"]))),
        )
    assert (result.status, result.sites) == (Status.OK, ("K1",))


# Кириллические «Е» и «Х», пробелы, регистр и пропущенный дефис.
@pytest.mark.parametrize(
    ("own", "foreign"),
    [("EX-1", "EX-2"), ("ЕХ-1", "ЕХ-2"), (" ex-1 ", "ex-2"), ("ех1", "ех2"), ("Ex 1", "Ex 2")],
)
def test_equipment_is_matched_regardless_of_spelling(
    connect: Barrier, own: str, foreign: str
) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        rows = query_rows(
            connection, grants, KpiQuery("downtime", (Filter("equipment_id", "eq", own),))
        )
        denied = query_rows(
            connection, grants, KpiQuery("downtime", (Filter("equipment_id", "eq", foreign),))
        )
        repair_own = check_repair(connection, grants, own)
        repair_foreign = check_repair(connection, grants, foreign)
    assert rows.status is Status.OK
    assert [row["equipment_id"] for row in rows.rows] == ["EX-1"]
    # Чужое оборудование, написанное кириллицей, — отказ, а не «такого нет».
    assert denied.status is Status.DENIED
    assert (repair_own.status, repair_foreign.status) == (Status.OK, Status.DENIED)


def test_kpi_comparisons_narrow_within_grants(connect: Barrier) -> None:
    with _open(connect, "u-eng") as (connection, grants):
        result = query_rows(
            connection,
            grants,
            KpiQuery(
                "equipment",
                (Filter("motor_hours", "gte", 4000), Filter("type", "eq", "экскаватор")),
            ),
        )
        limited = query_rows(connection, grants, KpiQuery("production", limit=2))
    assert [row["equipment_id"] for row in result.rows] == ["EX-1", "EX-2"]
    assert "row_id" not in result.rows[0]
    assert (len(limited.rows), limited.truncated) == (2, True)


def test_search_shows_superseded_only_on_request(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        current = search(connection, grants, "периодичность ремонта")
        history = search(connection, grants, "периодичность ремонта", include_superseded=True)
    assert _docs(current.chunks) == {"R2"}
    assert _docs(history.chunks) == {"R1", "R2"}
    assert {chunk.superseded_by for chunk in history.chunks if chunk.doc_id == "R1"} == {"R2"}


def test_search_finds_identifiers_exactly(connect: Barrier) -> None:
    with _open(connect, "u-k1") as (connection, grants):
        found = search(connection, grants, "EX-1")
    # Идентификатор совпал точно — фрагмент с ним первый, хотя число «1» есть и в других.
    assert found.chunks[0].chunk_id == "A1#1"


@pytest.fixture
def lookalike(database: Database, small: Path) -> Iterator[None]:
    """На чужом участке — оборудование, чей ключ совпадает с ключом своего: `EX1` и `EX-1`.

    Строка добавляется владельцем на время теста и удаляется после: набор в базе общий.
    """
    insert = (
        "insert into dwh.equipment (equipment_id, type, model, site, motor_hours,"
        " last_ppr_motor_hours) values ('EX1', 'экскаватор', 'ЭКГ-5', 'K2', 100, 50)"
    )
    with database.owner.begin() as connection:
        connection.execute(text(insert))
    try:
        yield
    finally:
        with database.owner.begin() as connection:
            connection.execute(text("delete from dwh.equipment where equipment_id = 'EX1'"))


def test_ambiguous_equipment_key_is_never_taken_for_own(connect: Barrier, lookalike: None) -> None:
    # Два идентификатора с одним ключом на разных участках. Точное написание находит своё
    # и чужое; написание, совпавшее только по ключу, не находит ни одно — иначе первая
    # строка по порядку могла бы выдать чужое оборудование за своё.
    with _open(connect, "u-k1") as (connection, grants):
        exact_own = check_repair(connection, grants, "EX-1")
        exact_foreign = check_repair(connection, grants, "EX1")
        by_key = check_repair(connection, grants, "ех 1")
        rows = query_rows(
            connection, grants, KpiQuery("equipment", (Filter("equipment_id", "eq", "EX1"),))
        )
    assert exact_own.status is Status.OK
    assert exact_foreign.status is Status.DENIED
    assert by_key.status is Status.NOT_FOUND
    assert rows.status is Status.DENIED
    with _open(connect, "u-eng") as (connection, grants):
        # Точное написание не подмешивает строки оборудования с тем же ключом.
        own_rows = query_rows(
            connection, grants, KpiQuery("equipment", (Filter("equipment_id", "eq", "EX-1"),))
        )
    assert [row["equipment_id"] for row in own_rows.rows] == ["EX-1"]


# --- Второй барьер -------------------------------------------------------------------------


def test_database_holds_rights_when_code_is_wrong(database: Database, small: Path) -> None:
    # Код ошибся и считает, что пользователю открыто всё. Под ролью сервиса база всё равно
    # отдаёт только разрешённое по полномочиям из своих таблиц.
    with database.service.begin() as connection:
        bind_user(connection, "u-k1")
        grants = load_grants(connection, "u-k1")
        assert grants is not None
        wrong = Grants(
            user_id=grants.user_id,
            name=grants.name,
            position=grants.position,
            groups=frozenset({"hr", "finance", "engineering"}),
            kpi={table: frozenset({"K1", "K2"}) for table in ("production", "downtime")},
            repair_sites=grants.repair_sites,
        )
        assert _docs(search(connection, wrong, "оклад горного мастера").chunks) == set()
        assert get_document(connection, wrong, "H1").status is Status.DENIED
        rows = query_rows(connection, wrong, KpiQuery("production"))
    assert {row["site"] for row in rows.rows} == {"K1"}


def test_unknown_user_has_no_grants(database: Database, small: Path) -> None:
    with database.service.begin() as connection:
        bind_user(connection, "u-nobody")
        assert load_grants(connection, "u-nobody") is None
