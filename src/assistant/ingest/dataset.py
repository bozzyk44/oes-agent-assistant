"""Чтение набора из `DATA_DIR` и проверка формата.

Проверяется только то, без чего набор нельзя загрузить: наличие файлов, заголовки и типы
колонок CSV, обязательные поля frontmatter, совпадение `id` с именем файла, формат дат
и времени. Состав набора здесь не проверяется: набор проверяющих не обязан быть похож
на наш ничем, кроме формата. Любые значения групп, участков, причин простоя и типов
оборудования принимаются, неизвестные поля пропускаются, `questions.jsonl` не читается.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

PRODUCTION_COLUMNS = ("date", "shift", "site", "tonnes")
DOWNTIME_COLUMNS = ("equipment_id", "site", "start", "end", "reason")
EQUIPMENT_COLUMNS = (
    "equipment_id",
    "type",
    "model",
    "site",
    "motor_hours",
    "last_ppr_motor_hours",
)
TABLES = ("production", "downtime", "equipment")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_INT = re.compile(r"-?\d+")
_FRONTMATTER_REQUIRED = ("id", "title", "date", "status", "acl")


class DatasetError(Exception):
    """Набор не по формату. В тексте — файл и строка, содержимого набора нет."""


@dataclass(frozen=True)
class DocumentFile:
    doc_id: str
    title: str
    date: date
    status: str
    acl: tuple[str, ...]
    supersedes: str | None
    superseded_by: str | None
    body: str


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    name: str
    position: str
    groups: tuple[str, ...]
    kpi: dict[str, tuple[str, ...]]
    repair_requests: tuple[str, ...]


@dataclass(frozen=True)
class Dataset:
    content_hash: str
    documents: tuple[DocumentFile, ...]
    users: tuple[UserRecord, ...]
    production: tuple[tuple[date, int, str, int], ...]
    downtime: tuple[tuple[str, str, datetime, datetime, str], ...]
    equipment: tuple[tuple[str, str, str, str, int, int], ...]


def read_dataset(root: Path, loader_version: int) -> Dataset:
    """Читает и проверяет набор целиком; при первом нарушении — `DatasetError`."""
    if not root.is_dir():
        raise DatasetError(f"нет каталога набора: {root}")
    doc_paths = sorted((root / "docs").glob("*.md"))
    if not doc_paths:
        raise DatasetError("docs: нет ни одного документа *.md")
    files = [*doc_paths, root / "users.json", *(root / f"{table}.csv" for table in TABLES)]

    # В хэш входит версия алгоритма загрузки: при смене правил разбиения или индексации
    # набор перезагружается, даже если файлы те же. Файл вопросов в хэш не входит.
    digest = hashlib.sha256(f"loader:{loader_version}\n".encode())
    texts: dict[str, str] = {}
    for path in files:
        name = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise DatasetError(f"{name}: файл не читается") from exc
        try:
            texts[name] = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DatasetError(f"{name}: кодировка не UTF-8") from exc
        digest.update(f"{name}\n{len(raw)}\n".encode())
        digest.update(raw)

    return Dataset(
        content_hash=digest.hexdigest(),
        documents=tuple(
            _document(f"docs/{path.name}", path.stem, texts[f"docs/{path.name}"])
            for path in doc_paths
        ),
        users=_users(texts["users.json"]),
        production=tuple(
            (_date_cell(where, row[0]), _int_cell(where, row[1]), row[2], _int_cell(where, row[3]))
            for where, row in _rows("production.csv", texts["production.csv"], PRODUCTION_COLUMNS)
        ),
        downtime=tuple(
            (row[0], row[1], _time_cell(where, row[2]), _time_cell(where, row[3]), row[4])
            for where, row in _rows("downtime.csv", texts["downtime.csv"], DOWNTIME_COLUMNS)
        ),
        equipment=tuple(
            (row[0], row[1], row[2], row[3], _int_cell(where, row[4]), _int_cell(where, row[5]))
            for where, row in _rows("equipment.csv", texts["equipment.csv"], EQUIPMENT_COLUMNS)
        ),
    )


def _rows(name: str, text: str, columns: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    lines = text.splitlines()
    header = ",".join(columns)
    if not lines or lines[0].strip() != header:
        raise DatasetError(f"{name}:1: заголовок должен быть «{header}»")
    rows: list[tuple[str, list[str]]] = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != len(columns):
            raise DatasetError(f"{name}:{number}: полей {len(cells)}, нужно {len(columns)}")
        rows.append((f"{name}:{number}", cells))
    return rows


def _int_cell(where: str, value: str) -> int:
    if not _INT.fullmatch(value):
        raise DatasetError(f"{where}: ожидалось целое число")
    return int(value)


def _date_cell(where: str, value: str) -> date:
    if _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise DatasetError(f"{where}: дата должна быть вида YYYY-MM-DD")


def _time_cell(where: str, value: str) -> datetime:
    if _TIME.fullmatch(value):
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    raise DatasetError(f"{where}: время должно быть вида YYYY-MM-DDTHH:MM")


def _document(name: str, stem: str, text: str) -> DocumentFile:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DatasetError(f"{name}:1: документ должен начинаться с frontmatter «---»")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        raise DatasetError(f"{name}: frontmatter не закрыт строкой «---»") from None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f":{mark.line + 2}" if mark is not None else ""
        raise DatasetError(f"{name}{line}: frontmatter не разобран как YAML") from exc
    if not isinstance(meta, dict):
        raise DatasetError(f"{name}: frontmatter должен быть набором «поле: значение»")
    for field in _FRONTMATTER_REQUIRED:
        if meta.get(field) in (None, "", []):
            raise DatasetError(f"{name}: нет обязательного поля «{field}»")
    if str(meta["id"]) != stem:
        raise DatasetError(f"{name}: поле id не совпадает с именем файла")

    # YAML сам читает 2026-03-02 как дату; строка в кавычках проверяется по формату.
    doc_date = meta["date"]
    if isinstance(doc_date, datetime) or not isinstance(doc_date, date):
        doc_date = _date_cell(f"{name}: поле date", str(doc_date))

    acl = meta["acl"]
    if isinstance(acl, (str, int)):
        acl = [acl]
    if not isinstance(acl, list) or any(isinstance(item, (dict, list)) for item in acl):
        raise DatasetError(f"{name}: поле acl должно быть списком групп")

    return DocumentFile(
        doc_id=stem,
        title=str(meta["title"]),
        date=doc_date,
        status=str(meta["status"]),
        acl=tuple(dict.fromkeys(str(item) for item in acl)),
        supersedes=_optional(meta.get("supersedes")),
        superseded_by=_optional(meta.get("superseded_by")),
        body="\n".join(lines[end + 1 :]),
    )


def _optional(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _users(text: str) -> tuple[UserRecord, ...]:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise DatasetError("users.json: не разобран как JSON") from exc
    if not isinstance(raw, list):
        raise DatasetError("users.json: ожидался массив пользователей")
    users: list[UserRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"users.json[{index}]"
        if not isinstance(item, dict):
            raise DatasetError(f"{where}: ожидался объект")
        user_id = item.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise DatasetError(f"{where}: нет поля user_id")
        if user_id in seen:
            raise DatasetError(f"{where}: user_id повторяется")
        seen.add(user_id)
        kpi = item.get("kpi", {})
        if not isinstance(kpi, dict):
            raise DatasetError(f"{where}: поле kpi должно быть объектом «таблица: участки»")
        users.append(
            UserRecord(
                user_id=user_id,
                name=_text(where, item, "name"),
                position=_text(where, item, "position"),
                groups=_strings(where, "groups", item.get("groups", [])),
                kpi={
                    str(table): _strings(where, f"kpi.{table}", sites)
                    for table, sites in kpi.items()
                },
                repair_requests=_strings(where, "repair_requests", item.get("repair_requests", [])),
            )
        )
    return tuple(users)


def _text(where: str, item: dict[str, Any], field: str) -> str:
    value = item.get(field, "")
    if not isinstance(value, str):
        raise DatasetError(f"{where}: поле {field} должно быть строкой")
    return value


def _strings(where: str, field: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DatasetError(f"{where}: поле {field} должно быть списком строк")
    return tuple(dict.fromkeys(value))
