"""Черновик заявки на ремонт: проверки кода до того, как действие будет предложено.

Порядок проверок — из ARCHITECTURE.md, «Предложение действия»:

1. Идентификатор оборудования есть в текущем сообщении пользователя отдельным словом.
   Так инструкция «оформи заявку» из текста документа не становится действием:
   идентификатор пришёл не от пользователя.
2. Оборудование существует.
3. Участок оборудования разрешён пользователю для заявок.
4. Приоритет и описание такие, что ТОиР их примет.

В параметры идёт идентификатор в написании из набора, а не модели или пользователя.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection

from assistant.access import Grants, Status, check_repair
from assistant.access.equipment import equipment_key

TOOL = "create_repair_request"
MAX_DESCRIPTION = 10_000
MAX_EQUIPMENT_ID = 200
# Слово — буквы и цифры, склеенные дефисами: «XY-7» — одно слово, «XY-7-A» — тоже одно,
# и «XY-7» в нём не найдётся. Пробел внутри идентификатора допускается: «XY 7» — это
# два слова подряд, и они проверяются вместе.
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:-[0-9A-Za-zА-Яа-яЁё]+)*")
# Широкое слово — всё, кроме пробелов, скобок, кавычек и знаков препинания между словами:
# для идентификаторов с подчёркиванием, косой чертой, точкой или «№» внутри. Узкое
# разбиение остаётся: «XY-7/XY-8» в сообщении — это два идентификатора.
_WIDE_WORD = re.compile(r"[^\s,;:!?()\[\]{}«»\"']+")
_MAX_WORDS = 3


@dataclass(frozen=True)
class DraftResult:
    status: Status
    # Параметры заявки — только при `ok`.
    params: dict[str, Any] = field(default_factory=dict)
    # Что не так — для `invalid` и `needs_identifier`; без содержимого набора.
    problem: str = ""


def mentioned(message: str, equipment_id: str) -> bool:
    """Идентификатор есть в сообщении отдельным словом или несколькими словами подряд.

    Сравнение — по тому же ключу, что в базе: без регистра, пробелов и дефисов,
    с приведением похожих кириллических и латинских букв.
    """
    wanted = equipment_key(equipment_id)
    if not wanted:
        return False
    narrow = [equipment_key(match.group()) for match in _WORD.finditer(message)]
    wide = [equipment_key(match.group().rstrip(".")) for match in _WIDE_WORD.finditer(message)]
    return any(
        "".join(words[start : start + length]) == wanted
        for words in (narrow, wide)
        for start in range(len(words))
        for length in range(1, _MAX_WORDS + 1)
        if start + length <= len(words)
    )


def _priority(value: object) -> int | None:
    """Приоритет 1–3. Открытые модели пишут число по-разному: `2`, `2.0`, `"2"`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip() in {"1", "2", "3"}:
        value = int(value.strip())
    return value if isinstance(value, int) and 1 <= value <= 3 else None


def _storable(value: str) -> bool:
    return "\x00" not in value and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def prepare_repair(
    connection: Connection, grants: Grants, message: str, arguments: dict[str, Any]
) -> DraftResult:
    """Черновик по аргументам модели и текущему сообщению пользователя."""
    equipment_id = arguments.get("equipment_id")
    description = arguments.get("description")
    priority = arguments.get("priority")
    if not isinstance(equipment_id, str) or not 0 < len(equipment_id) <= MAX_EQUIPMENT_ID:
        return DraftResult(Status.INVALID, problem="equipment_id: ожидалась непустая строка")
    if not mentioned(message, equipment_id):
        return DraftResult(
            Status.NEEDS_IDENTIFIER,
            problem="идентификатора оборудования нет в сообщении пользователя: попросите его",
        )
    check = check_repair(connection, grants, equipment_id)
    if check.status is not Status.OK:
        return DraftResult(check.status)
    priority = _priority(priority)
    if priority is None:
        return DraftResult(Status.INVALID, problem="priority: ожидалось целое от 1 до 3")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > MAX_DESCRIPTION
        or not _storable(description)
    ):
        return DraftResult(
            Status.INVALID,
            problem=f"description: ожидался непустой текст до {MAX_DESCRIPTION} символов",
        )
    return DraftResult(
        Status.OK,
        params={
            "equipment_id": check.equipment_id,
            "description": description.strip(),
            "priority": priority,
        },
    )
