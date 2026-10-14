"""Инварианты прав на любом наборе: тест не знает, что в наборе лежит, и перебирает его.

Идёт на наборе с зерном `data/` и на втором, с другими идентификаторами и расстановкой
ловушек. Ожидание считается из файлов набора, а не из знания о нём.
"""

import csv
import json
import re
from pathlib import Path
from typing import Any

import pytest

from assistant.access import (
    Filter,
    KpiQuery,
    Status,
    check_repair,
    get_document,
    load_grants,
    query_rows,
    search,
)
from assistant.access.grants import EVERYONE
from assistant.db.session import transaction
from datagen.world import DEFAULT_SEED, SECOND_SEED, World, build_world
from tests.conftest import Database

KPI = ("production", "downtime", "equipment")
SOURCE = Path(__file__).resolve().parents[1] / "src" / "assistant"


def _users(root: Path) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = json.loads((root / "users.json").read_text(encoding="utf-8"))
    return users


def _rows(root: Path, table: str) -> list[dict[str, str]]:
    with (root / f"{table}.csv").open(encoding="utf-8") as file:
        return list(csv.DictReader(file))


@pytest.mark.db
def test_every_user_against_every_closed_document_site_and_equipment(
    database: Database, dataset: tuple[Path, World]
) -> None:
    root, world = dataset
    database.load(root)
    docs = {doc.doc_id: doc for doc in world.docs.values()}
    acl = {doc_id: set(doc.acl) for doc_id, doc in docs.items()}
    tables = {table: _rows(root, table) for table in KPI}
    equipment = {row["equipment_id"]: row["site"] for row in tables["equipment"]}
    sites = {row["site"] for rows in tables.values() for row in rows}
    checked = {"документы": 0, "участки": 0, "оборудование": 0}

    for user in _users(root):
        groups = {EVERYONE, *(user["groups"] or [])}
        closed = sorted(doc for doc, value in acl.items() if not value & groups)
        with transaction(database.service, user["user_id"]) as connection:
            grants = load_grants(connection, user["user_id"])
            assert grants is not None

            for doc_id in closed:
                checked["документы"] += 1
                assert get_document(connection, grants, doc_id).status is Status.DENIED
                title = docs[doc_id].title
                found = search(connection, grants, title, include_superseded=True)
                assert doc_id not in {chunk.doc_id for chunk in found.chunks}, (user, doc_id)

            for table in KPI:
                allowed = set(user["kpi"].get(table) or [])
                for site in sorted(sites - allowed):
                    checked["участки"] += 1
                    named = query_rows(
                        connection, grants, KpiQuery(table, (Filter("site", "eq", site),))
                    )
                    assert named.status is Status.DENIED, (user["user_id"], table, site)
                unfiltered = query_rows(connection, grants, KpiQuery(table, limit=1000))
                if not allowed:
                    assert unfiltered.status is Status.DENIED
                    continue
                # Без фильтра — ровно строки разрешённых участков, посчитанные из файла.
                expected = [row for row in tables[table] if row["site"] in allowed]
                assert len(unfiltered.rows) == len(expected), (user["user_id"], table)
                assert {row["site"] for row in unfiltered.rows} <= allowed
                if table == "production":
                    total = sum(int(row["tonnes"]) for row in unfiltered.rows)
                    assert total == sum(int(row["tonnes"]) for row in expected)

            repair = set(user["repair_requests"] or [])
            downtime_allowed = set(user["kpi"].get("downtime") or [])
            for equipment_id, site in equipment.items():
                if site not in repair:
                    checked["оборудование"] += 1
                    check = check_repair(connection, grants, equipment_id)
                    assert check.status is Status.DENIED, (user["user_id"], equipment_id)
                if downtime_allowed and site not in downtime_allowed:
                    by_id = query_rows(
                        connection,
                        grants,
                        KpiQuery("downtime", (Filter("equipment_id", "eq", equipment_id),)),
                    )
                    assert by_id.status is Status.DENIED, (user["user_id"], equipment_id)

    # Набор содержит ловушки на все три вида: инвариант не прошёл впустую.
    assert all(checked.values()), checked


def test_service_code_knows_nothing_of_the_dataset() -> None:
    # Ни одного идентификатора документа, пользователя или оборудования ни из одного
    # набора генератора: сервис запустят на чужом наборе того же формата.
    identifiers: set[str] = set()
    for seed in (DEFAULT_SEED, SECOND_SEED):
        world = build_world(seed)
        identifiers |= {doc.doc_id for doc in world.docs.values()}
        identifiers |= {user.user_id for user in world.users}
        identifiers |= {item.equipment_id for item in world.equipment}
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(map(re.escape, sorted(identifiers))) + r")(?![\w-])"
    )
    found = [
        f"{path.relative_to(SOURCE)}:{number}: {match.group()}"
        for path in sorted(SOURCE.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".sql", ".md", ".txt", ".json"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for match in pattern.finditer(line)
    ]
    assert found == []


def test_only_access_layer_reads_dataset_tables() -> None:
    # Инструменты и цикл хода получают данные набора только через `access/`.
    pattern = re.compile(r"\b(dwh|kb)\.\w|models\.(Production|Downtime|Equipment|Chunk|Document)\b")
    allowed = ("access/", "db/", "ingest/")
    found = [
        f"{path.relative_to(SOURCE)}:{number}"
        for path in sorted(SOURCE.rglob("*.py"))
        if not path.relative_to(SOURCE).as_posix().startswith(allowed)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert found == []
