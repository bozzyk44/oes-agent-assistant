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
_FIELD = re.compile(r"([A-Za-z_][\w-]*):(?:\s+(.*)|())")
_LIST_ITEM = re.compile(r"\s*-\s+(.*)")
_COMMENT = re.compile(r"\s+#.*")
_ID_LINE = re.compile(r"id:\s")
# Как YAML записывает отсутствие значения; типы не угадываются, поэтому сравнение по тексту.
_YAML_NULLS = ("null", "Null", "NULL", "~")
# Границы целых колонок базы: значение вне их названо с файлом и строкой, а не ошибкой базы.
_INT_MAX = 2**31 - 1
_SMALLINT_MAX = 2**15 - 1


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
    # Файлы в docs/ без frontmatter: не документы набора, в базу не попадают.
    skipped: tuple[str, ...]
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

    parsed = {
        f"docs/{path.name}": _document(f"docs/{path.name}", path.stem, texts[f"docs/{path.name}"])
        for path in doc_paths
    }
    documents = tuple(doc for doc in parsed.values() if doc is not None)
    if not documents:
        raise DatasetError("docs: ни в одном файле *.md нет frontmatter «---»")

    return Dataset(
        content_hash=digest.hexdigest(),
        skipped=tuple(name for name, doc in parsed.items() if doc is None),
        documents=documents,
        users=_users(texts["users.json"]),
        production=tuple(
            (
                _date_cell(where, row[0]),
                _int_cell(where, row[1], _SMALLINT_MAX),
                row[2],
                _int_cell(where, row[3]),
            )
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


def _int_cell(where: str, value: str, limit: int = _INT_MAX) -> int:
    if not _INT.fullmatch(value):
        raise DatasetError(f"{where}: ожидалось целое число")
    if abs(int(value)) > limit:
        raise DatasetError(f"{where}: число больше допустимого {limit}")
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


def _document(name: str, stem: str, text: str) -> DocumentFile | None:
    """Документ набора, либо `None` для файла без frontmatter: это не документ набора,
    а посторонний файл в каталоге — он пропускается и называется в выводе загрузчика.
    """
    lines = text.splitlines()
    # Пустые строки перед frontmatter — не повод потерять документ.
    while lines and not lines[0].strip():
        lines = lines[1:]
    if not lines or lines[0].strip() != "---":
        # Файл с полем id, но без открывающей строки — испорченный документ, а не посторонний
        # файл: пропустить его молча значило бы потерять документ из индекса.
        if any(_ID_LINE.match(line) for line in lines[:10]):
            raise DatasetError(f"{name}:1: документ должен начинаться с frontmatter «---»")
        return None
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        raise DatasetError(f"{name}: frontmatter не закрыт строкой «---»") from None
    meta = _frontmatter(name, lines[1:end])
    for field in ("id", "title", "date", "status"):
        if meta.get(field) in (None, "", [], *_YAML_NULLS):
            raise DatasetError(f"{name}: нет обязательного поля «{field}»")
        if not isinstance(meta[field], str):
            raise DatasetError(f"{name}: поле {field} должно быть строкой")
    if meta["id"] != stem:
        raise DatasetError(f"{name}: поле id не совпадает с именем файла")

    if "acl" not in meta or meta["acl"] in (None, "", *_YAML_NULLS):
        raise DatasetError(f"{name}: нет обязательного поля «acl»")
    acl = meta["acl"]
    if isinstance(acl, str):
        # «acl: engineering, site-k1» без скобок — тоже список, а не одна группа с запятой.
        acl = [item.strip() for item in acl.split(",") if item.strip()]
    if not isinstance(acl, list) or not all(isinstance(item, str) for item in acl):
        raise DatasetError(f"{name}: поле acl должно быть списком групп")

    return DocumentFile(
        doc_id=stem,
        title=meta["title"],
        date=_date_cell(f"{name}: поле date", meta["date"]),
        status=meta["status"],
        # Пустой список — документ не доступен никому; это допустимое значение.
        acl=tuple(dict.fromkeys(acl)),
        supersedes=_optional(name, "supersedes", meta.get("supersedes")),
        superseded_by=_optional(name, "superseded_by", meta.get("superseded_by")),
        body="\n".join(lines[end + 1 :]),
    )


def _frontmatter(name: str, lines: list[str]) -> dict[str, Any]:
    """Frontmatter как YAML, а где YAML не справился — построчно.

    Значения читаются строками, без догадок YAML о типах: иначе идентификатор 0012 стал бы
    числом 10, группа no — ложью, а дата 2026-02-30 уронила бы разбор. BaseLoader строит
    только строки, списки и словари — произвольных объектов нет.

    Набор может быть собран подстановкой в шаблон, без оглядки на YAML. Два таких случая
    не должны останавливать запуск: «title: Приказ № 12: об ответственных» — для YAML
    ошибка, и «title: Приказ #12» — для YAML комментарий, молча обрезающий название.
    """
    plain = _plain_fields(lines)
    try:
        meta = yaml.load("\n".join(lines), Loader=yaml.BaseLoader)  # noqa: S506
    except (yaml.YAMLError, ValueError) as exc:
        # Построчное чтение годится, только если нашло все обязательные поля.
        if all(field in plain for field in ("id", "title", "date", "status", "acl")):
            return plain
        mark = getattr(exc, "problem_mark", None)
        line = f":{mark.line + 2}" if mark is not None else ""
        raise DatasetError(f"{name}{line}: frontmatter не разобран") from exc
    if not isinstance(meta, dict):
        raise DatasetError(f"{name}: frontmatter должен быть набором «поле: значение»")
    # Только название: в остальных полях « # …» — обычный комментарий YAML, и его место
    # за пределами значения. Название же с решёткой — «Приказ #12» — обрезалось бы молча.
    raw, value = plain.get("title"), meta.get("title")
    if isinstance(raw, str) and isinstance(value, str) and raw != value and raw.startswith(value):
        meta["title"] = raw
    return {str(key): value for key, value in meta.items()}


def _plain_fields(lines: list[str]) -> dict[str, Any]:
    """Поля верхнего уровня, прочитанные построчно: скаляр как есть, список в скобках
    или по элементу на строке. Вложенные и незакрытые структуры пропускаются.
    """
    fields: dict[str, Any] = {}
    current: str | None = None
    for line in lines:
        item = _LIST_ITEM.fullmatch(line)
        if item and current is not None and fields.get(current) == "":
            fields[current] = []
        if item and current is not None and isinstance(fields.get(current), list):
            fields[current].append(_unquote(item.group(1)))
            continue
        match = _FIELD.fullmatch(line)
        if not match:
            continue
        current, rest = match.group(1), (match.group(2) or "").strip()
        if not rest:
            # Пустое значение; станет списком, если дальше идут строки «- элемент».
            fields[current] = ""
        elif rest[0] not in "[{":
            # Хвост « # …» — комментарий; у названия он остаётся частью текста.
            fields[current] = _unquote(rest if current == "title" else _COMMENT.sub("", rest))
        elif rest[0] == "[" and rest.endswith("]"):
            fields[current] = [_unquote(part) for part in rest[1:-1].split(",") if part.strip()]
        else:
            fields.pop(current, None)
    return fields


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _optional(name: str, field: str, value: Any) -> str | None:
    if value is None or value in ("", *_YAML_NULLS):
        return None
    if not isinstance(value, str):
        raise DatasetError(f"{name}: поле {field} должно быть строкой")
    return value


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
        # null в необязательном поле — то же, что его отсутствие.
        kpi = item.get("kpi")
        if kpi is None:
            kpi = {}
        if not isinstance(kpi, dict):
            raise DatasetError(f"{where}: поле kpi должно быть объектом «таблица: участки»")
        users.append(
            UserRecord(
                user_id=user_id,
                name=_text(where, item, "name"),
                position=_text(where, item, "position"),
                groups=_strings(where, "groups", item.get("groups")),
                kpi={
                    str(table): _strings(where, f"kpi.{table}", sites)
                    for table, sites in kpi.items()
                },
                repair_requests=_strings(where, "repair_requests", item.get("repair_requests")),
            )
        )
    return tuple(users)


def _text(where: str, item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DatasetError(f"{where}: поле {field} должно быть строкой")
    return value


def _strings(where: str, field: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DatasetError(f"{where}: поле {field} должно быть списком строк")
    return tuple(dict.fromkeys(value))
