"""Чтение набора и проверка формата: по одному нарушению на каждое правило."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from assistant.ingest.dataset import DatasetError, read_dataset
from datagen.world import World
from tests.smallset import write_small_dataset

Edit = Callable[[Path], None]


def _replace(name: str, old: str, new: str) -> Edit:
    def edit(root: Path) -> None:
        path = root / name
        text = path.read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    return edit


def _remove(name: str) -> Edit:
    return lambda root: (root / name).unlink()


def _write(name: str, content: bytes) -> Edit:
    def edit(root: Path) -> None:
        (root / name).write_bytes(content)

    return edit


def _no_docs(root: Path) -> None:
    for path in (root / "docs").glob("*.md"):
        path.unlink()


VIOLATIONS: list[tuple[str, Edit, str]] = [
    ("нет документов", _no_docs, "docs: нет ни одного документа"),
    ("нет пользователей", _remove("users.json"), "users.json: файл не читается"),
    ("нет таблицы", _remove("downtime.csv"), "downtime.csv: файл не читается"),
    (
        "кодировка",
        _write("equipment.csv", "заголовок".encode("cp1251")),
        "equipment.csv: кодировка",
    ),
    (
        "заголовок CSV",
        _replace("production.csv", "date,shift,site,tonnes", "date,site,shift,tonnes"),
        "production.csv:1: заголовок",
    ),
    (
        "число полей",
        _replace("production.csv", "K1,100", "K1,100,лишнее"),
        "production.csv:2: полей 5",
    ),
    ("целое", _replace("production.csv", "K1,100", "K1,сто"), "production.csv:2: ожидалось целое"),
    (
        "дробное",
        _replace("equipment.csv", "5000,4500", "5000.5,4500"),
        "equipment.csv:2: ожидалось",
    ),
    (
        "дата",
        _replace("production.csv", "2026-01-01,1,K1", "01.01.2026,1,K1"),
        "production.csv:2: дата",
    ),
    (
        "нет такой даты",
        _replace("production.csv", "2026-01-02", "2026-02-30"),
        "production.csv:6: дата",
    ),
    (
        "время",
        _replace("downtime.csv", "2026-01-01T22:00", "2026-01-01 22:00"),
        "downtime.csv:2: время",
    ),
    (
        "время с секундами",
        _replace("downtime.csv", "2026-01-02T03:30", "2026-01-02T03:30:00"),
        "downtime.csv:2: время",
    ),
    ("нет frontmatter", _replace("docs/A1.md", "---\nid: A1", "id: A1"), "docs/A1.md:1:"),
    (
        "frontmatter не закрыт",
        _replace("docs/A1.md", "acl: [all]\n---", "acl: [all]\n"),
        "docs/A1.md: frontmatter не закрыт",
    ),
    ("не YAML", _replace("docs/A1.md", "acl: [all]", "acl: [all"), "docs/A1.md:"),
    ("нет id", _replace("docs/A1.md", "id: A1\n", ""), "docs/A1.md: нет обязательного поля «id»"),
    ("нет title", _replace("docs/A1.md", "title: Памятка для всех\n", ""), "поля «title»"),
    ("нет date", _replace("docs/A1.md", "date: 2026-01-10\n", ""), "поля «date»"),
    ("нет status", _replace("docs/A1.md", "status: active\n", ""), "поля «status»"),
    ("нет acl", _replace("docs/A1.md", "acl: [all]\n", ""), "поля «acl»"),
    ("пустой acl", _replace("docs/A1.md", "acl: [all]", "acl: []"), "поля «acl»"),
    ("id не имя файла", _replace("docs/A1.md", "id: A1", "id: A2"), "id не совпадает"),
    ("дата документа", _replace("docs/A1.md", "date: 2026-01-10", "date: 10 января"), "поле date"),
    ("не JSON", _write("users.json", b"[{"), "users.json: не разобран"),
    ("не массив", _write("users.json", b"{}"), "users.json: ожидался массив"),
    (
        "нет user_id",
        _replace("users.json", '"user_id": "u-eng", ', ""),
        "users.json[0]: нет поля user_id",
    ),
    (
        "повтор user_id",
        _replace("users.json", '"u-k1"', '"u-eng"'),
        "users.json[1]: user_id повторяется",
    ),
    ("kpi не объект", _replace("users.json", '"kpi": {}', '"kpi": []'), "users.json[3]: поле kpi"),
    (
        "группы не список",
        _replace("users.json", '"groups": ["hr"]', '"groups": "hr"'),
        "users.json[3]: поле groups",
    ),
]


@pytest.mark.parametrize(
    ("edit", "message"),
    [(edit, message) for _, edit, message in VIOLATIONS],
    ids=[name for name, _, _ in VIOLATIONS],
)
def test_violation_is_named_with_file_and_place(tmp_path: Path, edit: Edit, message: str) -> None:
    root = write_small_dataset(tmp_path)
    edit(root)
    with pytest.raises(DatasetError) as caught:
        read_dataset(root, 1)
    assert message in str(caught.value)


def test_missing_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="нет каталога набора"):
        read_dataset(tmp_path / "нет", 1)


def test_small_dataset_is_read(tmp_path: Path) -> None:
    dataset = read_dataset(write_small_dataset(tmp_path), 1)
    assert [doc.doc_id for doc in dataset.documents] == ["A1", "H1", "R1", "R2"]
    assert dataset.documents[2].superseded_by == "R2"
    assert dataset.documents[3].supersedes == "R1"
    assert dataset.documents[0].body.lstrip().startswith("# Памятка для всех")
    assert len(dataset.production) == 5
    assert dataset.downtime[0][2].hour == 22
    assert dataset.equipment[0][4:] == (5000, 4500)
    assert dataset.users[2].kpi == {"production": ("K1", "K2")}


def test_generated_dataset_is_read(dataset: tuple[Path, World]) -> None:
    root, world = dataset
    loaded = read_dataset(root, 1)
    assert {doc.doc_id for doc in loaded.documents} == {doc.doc_id for doc in world.docs.values()}
    assert len(loaded.users) == len(world.users)
    assert len(loaded.production) == len(world.production)
    assert len(loaded.downtime) == len(world.downtime)
    assert len(loaded.equipment) == len(world.equipment)


def test_unknown_fields_and_values_are_accepted(tmp_path: Path) -> None:
    # Набор проверяющих не обязан быть похож на наш ничем, кроме формата.
    root = write_small_dataset(tmp_path)
    _replace("docs/A1.md", "acl: [all]", "acl:\n  - all\n  - contractors\nowner: служба\nrev: 3")(
        root
    )
    _replace("docs/H1.md", "status: active", "status: draft")(root)
    _replace("docs/H1.md", "date: 2026-01-10", 'date: "2026-01-10"')(root)
    _replace("downtime.csv", "ремонт", "авария")(root)
    _replace("equipment.csv", "экскаватор,ЭКГ-10,K1", "бульдозер,Т-170,K3")(root)
    users = json.loads((root / "users.json").read_text(encoding="utf-8"))
    users[0]["groups"].append("contractors")
    users[0]["kpi"]["fuel"] = ["K3"]
    users[0]["phone"] = "нет"
    users.append({"user_id": "u-min"})
    (root / "users.json").write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
    (root / "заметки.txt").write_bytes(b"\xff\xfe")
    (root / "production.csv").write_bytes(
        b"\xef\xbb\xbf" + (root / "production.csv").read_bytes().replace(b"\n", b"\r\n")
    )

    dataset = read_dataset(root, 1)
    assert dataset.documents[0].acl == ("all", "contractors")
    assert dataset.documents[1].status == "draft"
    assert dataset.documents[1].date.isoformat() == "2026-01-10"
    assert dataset.downtime[0][4] == "авария"
    assert dataset.equipment[0][:4] == ("EX-1", "бульдозер", "Т-170", "K3")
    assert dataset.users[0].kpi["fuel"] == ("K3",)
    assert dataset.users[-1].groups == () and dataset.users[-1].repair_requests == ()
    assert len(dataset.production) == 5


def test_hash_follows_data_and_loader_version_but_not_questions(tmp_path: Path) -> None:
    root = write_small_dataset(tmp_path)
    base = read_dataset(root, 1).content_hash
    assert read_dataset(root, 1).content_hash == base
    # Файл вопросов загрузчик не читает и в хэш не включает.
    (root / "questions.jsonl").write_text("не JSON вовсе\n", encoding="utf-8")
    assert read_dataset(root, 1).content_hash == base
    # Версия алгоритма загрузки входит в хэш: те же файлы, другой хэш.
    assert read_dataset(root, 2).content_hash != base
    _replace("production.csv", "K1,100", "K1,101")(root)
    assert read_dataset(root, 1).content_hash != base


def test_hash_tells_renamed_file_from_changed_content(tmp_path: Path) -> None:
    first = write_small_dataset(tmp_path / "a")
    second = write_small_dataset(tmp_path / "b")
    (second / "docs" / "A1.md").rename(second / "docs" / "A0.md")
    _replace("docs/A0.md", "id: A1", "id: A0")(second)
    assert read_dataset(first, 1).content_hash != read_dataset(second, 1).content_hash
