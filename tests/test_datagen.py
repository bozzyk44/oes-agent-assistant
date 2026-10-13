"""Генератор набора: повторяемость, формат, состав и ловушки.

Тесты не знают содержимого набора: всё, что они сверяют, вычислено из файлов или из
параметров генератора.
"""

import csv
import filecmp
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from datagen.__main__ import main
from datagen.check import LONG_DOC_TOKENS, check_dataset, check_traps, read_docs
from datagen.render import INJECTION_PHRASE, render, write_dataset
from datagen.text import min_tokens, normalize
from datagen.world import DEFAULT_SEED, SECOND_SEED, SITES, World, build_world

Dataset = tuple[Path, World]


def _generated_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def test_same_seed_gives_same_files(dataset: Dataset, tmp_path: Path) -> None:
    root, world = dataset
    write_dataset(build_world(world.seed), tmp_path)
    names = _generated_files(root)
    assert _generated_files(tmp_path) == names
    assert filecmp.cmpfiles(root, tmp_path, names, shallow=False)[1:] == ([], [])


def test_other_seed_gives_other_numbers_and_ids() -> None:
    first, second = render(build_world(DEFAULT_SEED)), render(build_world(SECOND_SEED))
    for name in ("users.json", "production.csv", "downtime.csv", "equipment.csv"):
        assert first[name] != second[name]
    first_world, second_world = build_world(DEFAULT_SEED), build_world(SECOND_SEED)
    assert {role: doc.doc_id for role, doc in first_world.docs.items()} != {
        role: doc.doc_id for role, doc in second_world.docs.items()
    }


@pytest.mark.skipif("DATASET_DIR" not in os.environ, reason="набор из репозитория не подключён")
def test_repository_dataset_is_generator_output(tmp_path: Path) -> None:
    # data/ в репозитории — ровно то, что пишет `make data`: правка руками была бы видна здесь.
    committed = Path(os.environ["DATASET_DIR"])
    write_dataset(build_world(DEFAULT_SEED), tmp_path)
    names = _generated_files(tmp_path)
    assert sorted(path.name for path in (committed / "docs").glob("*.md")) == sorted(
        path.name for path in (tmp_path / "docs").glob("*.md")
    )
    assert filecmp.cmpfiles(committed, tmp_path, names, shallow=False)[1:] == ([], [])
    assert check_dataset(committed) == []


def test_dataset_passes_format_and_trap_checks(dataset: Dataset) -> None:
    root, world = dataset
    assert check_dataset(root) == []
    assert check_traps(root, world) == []


@pytest.mark.parametrize("seed", range(40))
def test_any_seed_passes_checks(seed: int, tmp_path: Path) -> None:
    world = build_world(seed)
    write_dataset(world, tmp_path)
    assert check_dataset(tmp_path) == []
    assert check_traps(tmp_path, world) == []


def test_old_and_new_edition_send_different_excavators_to_ppr(dataset: Dataset) -> None:
    root, _ = dataset
    docs = {doc.doc_id: doc for doc in read_docs(root, [])}
    old = next(doc for doc in docs.values() if doc.meta["status"] == "superseded")
    active = docs[str(old.meta["superseded_by"])]

    def periodicity(body: str) -> int:
        words = body.split()
        return int(words[words.index("моточасов") - 1])

    with (root / "equipment.csv").open(encoding="utf-8", newline="") as file:
        elapsed = {
            row["equipment_id"]: int(row["motor_hours"]) - int(row["last_ppr_motor_hours"])
            for row in csv.DictReader(file)
            if row["equipment_id"].startswith("EX")
        }
    due_old = {key for key, hours in elapsed.items() if hours >= periodicity(old.body)}
    due_new = {key for key, hours in elapsed.items() if hours >= periodicity(active.body)}
    assert periodicity(old.body) != periodicity(active.body)
    assert due_old != due_new
    assert due_new


def test_instruction_for_assistant_and_long_document(dataset: Dataset) -> None:
    root, _ = dataset
    docs = read_docs(root, [])
    hr_only = [doc.doc_id for doc in docs if doc.acl == ["hr"]]
    injected = [doc for doc in docs if INJECTION_PHRASE in doc.body]
    assert len(injected) == 1
    assert "all" in injected[0].acl
    assert any(doc_id in injected[0].body for doc_id in hr_only)
    assert any("all" in doc.acl and min_tokens(doc.body) >= LONG_DOC_TOKENS for doc in docs)


def test_closed_numbers_are_absent_from_everything_else(dataset: Dataset) -> None:
    root, world = dataset
    closed = {world.docs[role].doc_id: numbers for role, numbers in world.confidential().items()}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        text = normalize(path.read_text(encoding="utf-8"))
        for doc_id, numbers in closed.items():
            present = [number for number in numbers if str(number) in text]
            assert present == (list(numbers) if path.stem == doc_id else [])


def _drop_line(root: Path, name: str, keep: Callable[[str], bool]) -> None:
    path = root / name
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(line for line in lines if keep(line)), encoding="utf-8")


def _edit(root: Path, name: str, old: str, new: str) -> None:
    path = root / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _break_header(root: Path, world: World) -> None:
    _edit(root, "production.csv", "date,shift,site,tonnes", "date,site,shift,tonnes")


def _quote_field(root: Path, world: World) -> None:
    row = world.equipment[0]
    _edit(root, "equipment.csv", f"{row.equipment_id},", f'"{row.equipment_id}",')


def _drop_reason(root: Path, world: World) -> None:
    _drop_line(root, "downtime.csv", lambda line: not line.rstrip().endswith(",нет водителя"))


def _drop_day(root: Path, world: World) -> None:
    day = world.production[len(world.production) // 2].day.isoformat()
    _drop_line(root, "production.csv", lambda line: not line.startswith(day))


def _level_anomaly(root: Path, world: World) -> None:
    row = world.anomaly
    prefix = f"{row.day.isoformat()},{row.shift},{row.site},"
    _edit(root, "production.csv", f"{prefix}{row.tonnes}\n", f"{prefix}{row.tonnes * 4}\n")


def _drop_profile(root: Path, world: World) -> None:
    path = root / "users.json"
    users = [user for user in json.loads(path.read_text(encoding="utf-8")) if user["kpi"]]
    path.write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")


def _widen_site_head(root: Path, world: World) -> None:
    path = root / "users.json"
    users = json.loads(path.read_text(encoding="utf-8"))
    for user in users:
        if len(user["repair_requests"]) == 1:
            user["repair_requests"] = list(SITES)
    path.write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")


def _open_closed_doc(root: Path, world: World) -> None:
    _edit(root, f"docs/{world.docs['hr'].doc_id}.md", "acl: [hr]", "acl: [all]")


def _break_edition_link(root: Path, world: World) -> None:
    name = f"docs/{world.docs['ppr_old'].doc_id}.md"
    _drop_line(root, name, lambda line: not line.startswith("superseded_by:"))


def _rename_doc(root: Path, world: World) -> None:
    (root / "docs" / f"{world.docs['plan'].doc_id}.md").rename(root / "docs" / "D99.md")


def _shorten_long_doc(root: Path, world: World) -> None:
    path = root / "docs" / f"{world.docs['safety'].doc_id}.md"
    path.write_text(path.read_text(encoding="utf-8")[:4000], encoding="utf-8")


def _too_few_docs(root: Path, world: World) -> None:
    for role in ("downtime_rules", "contacts", "order"):
        (root / "docs" / f"{world.docs[role].doc_id}.md").unlink()


@pytest.mark.parametrize(
    "damage",
    [
        _break_header,
        _quote_field,
        _drop_reason,
        _drop_day,
        _level_anomaly,
        _drop_profile,
        _widen_site_head,
        _open_closed_doc,
        _break_edition_link,
        _rename_doc,
        _shorten_long_doc,
        _too_few_docs,
    ],
)
def test_format_check_catches_damage(
    damage: Callable[[Path, World], None], dataset: Dataset, tmp_path: Path
) -> None:
    root, world = dataset
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    damage(copy, world)
    assert check_dataset(copy) != []
    assert main(["--check", str(copy)]) == 1


def _same_periodicity(root: Path, world: World) -> None:
    # Обе редакции с одной периодичностью: на ППР пора одним и тем же экскаваторам.
    name = f"docs/{world.docs['ppr_old'].doc_id}.md"
    _edit(root, name, f"каждые {world.ppr_old} моточасов", f"каждые {world.ppr_new} моточасов")


def _reset_motor_hours(root: Path, world: World) -> None:
    path = root / "equipment.csv"
    rows = [line.split(",") for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows[1:]:
        row[5] = row[4]
    path.write_text("\n".join(",".join(row) for row in rows) + "\n", encoding="utf-8")


def _leak_salary(root: Path, world: World) -> None:
    name = f"docs/{world.docs['contacts'].doc_id}.md"
    path = root / name
    path.write_text(
        path.read_text(encoding="utf-8") + f"\nОклад: {world.salaries[0].salary} руб.\n",
        encoding="utf-8",
    )


def _remove_instruction(root: Path, world: World) -> None:
    _edit(root, f"docs/{world.docs['handover'].doc_id}.md", INJECTION_PHRASE, "")


def _open_memo(root: Path, world: World) -> None:
    doc = world.docs["memo"]
    _edit(root, f"docs/{doc.doc_id}.md", f"acl: [{', '.join(doc.acl)}]", "acl: [site-k1, site-k2]")


def _phone_in_order(root: Path, world: World) -> None:
    person = world.responsibles[SITES[0]]
    name = f"docs/{world.docs['order'].doc_id}.md"
    _edit(root, name, person.person.name, f"{person.person.name} (тел. {person.phone})")


@pytest.mark.parametrize(
    "damage",
    [
        _same_periodicity,
        _reset_motor_hours,
        _leak_salary,
        _remove_instruction,
        _open_memo,
        _phone_in_order,
    ],
)
def test_trap_check_catches_damage(
    damage: Callable[[Path, World], None], dataset: Dataset, tmp_path: Path
) -> None:
    root, world = dataset
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    damage(copy, world)
    assert check_traps(copy, world) != []


def test_command_writes_checked_dataset_and_removes_stale_documents(tmp_path: Path) -> None:
    stale = tmp_path / "docs" / "D99.md"
    stale.parent.mkdir()
    stale.write_text("прежний запуск", encoding="utf-8")
    foreign = tmp_path / "questions.jsonl"
    foreign.write_text("", encoding="utf-8")

    assert main(["--seed", str(SECOND_SEED), "--out", str(tmp_path)]) == 0
    assert not stale.exists()
    assert foreign.exists()
    assert main(["--check", str(tmp_path)]) == 0
