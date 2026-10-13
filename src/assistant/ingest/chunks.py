"""Разбиение документа на фрагменты для индекса.

Документ режется по заголовкам `##`; текст до первого заголовка — отдельный фрагмент.
Раздел длиннее потолка делится на части по абзацам и строкам таблиц; шапка таблицы
повторяется в каждой её части. Идентификатор — `<id документа>#<номер раздела>`,
у частей длинного раздела добавляется номер части через точку.
"""

import re
from dataclasses import dataclass

# Лимит результата инструмента из задания и потолок фрагмента — его четверть:
# в одну выдачу поиска помещается до четырёх фрагментов целиком.
TOOL_RESULT_TOKENS = 1500
CHUNK_TOKENS = TOOL_RESULT_TOKENS // 4
# Оценка сверху: на русском тексте измерено 3,9–4,7 символа на токен (DECISIONS.md,
# решение 8); три символа на токен оставляют запас для таблиц, цифр и разметки.
CHARS_PER_TOKEN = 3
CHUNK_CHARS = CHUNK_TOKENS * CHARS_PER_TOKEN

_TERM = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[-/][0-9A-Za-zА-Яа-яЁё]+)*")
_TABLE_RULE = re.compile(r"\|[\s:|-]+\|")


@dataclass(frozen=True)
class Piece:
    chunk_id: str
    section_no: int
    part_no: int
    heading: str
    body: str


def split_document(doc_id: str, body: str, limit: int = CHUNK_CHARS) -> list[Piece]:
    """Фрагменты документа по порядку. Строка названия `# …` в них не входит:
    название хранится отдельно и индексируется с каждым фрагментом.
    """
    lines = body.strip("\n").split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]

    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)

    pieces: list[Piece] = []
    for number, (heading, content) in enumerate(sections):
        text = "\n".join(content).strip("\n")
        if not heading and not text.strip():
            continue
        parts = _split_section(text, max(limit - len(heading), limit // 2))
        for index, part in enumerate(parts, start=1):
            suffix = f".{index}" if len(parts) > 1 else ""
            pieces.append(
                Piece(
                    chunk_id=f"{doc_id}#{number}{suffix}",
                    section_no=number,
                    part_no=index,
                    heading=heading,
                    body=part,
                )
            )
    return pieces


def exact_terms(text: str) -> list[str]:
    """Идентификаторы и числа для точного поиска: слова с цифрой, без стемминга.

    Регистр и «ё» приводятся так же, как в полнотекстовом индексе.
    """
    found = {
        match.group().lower().replace("ё", "е")
        for match in _TERM.finditer(text)
        if any(char.isdigit() for char in match.group())
    }
    return sorted(found)


def _split_section(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    units: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        if len(block) <= limit:
            units.append(block)
        elif _is_table(block):
            units.extend(_split_table(block, limit))
        else:
            units.extend(_split_lines(block, limit))
    return _pack(units, limit, "\n\n")


def _is_table(block: str) -> bool:
    rows = block.split("\n")
    return len(rows) >= 2 and rows[0].startswith("|") and bool(_TABLE_RULE.fullmatch(rows[1]))


def _split_table(block: str, limit: int) -> list[str]:
    rows = block.split("\n")
    head = "\n".join(rows[:2])
    room = max(limit - len(head) - 1, 1)
    return [f"{head}\n{part}" for part in _pack(rows[2:], room, "\n")]


def _split_lines(block: str, limit: int) -> list[str]:
    units: list[str] = []
    for line in block.split("\n"):
        # Строка длиннее потолка режется по пробелам: резать её больше не по чему.
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit + 1)
            cut = cut if cut > 0 else limit
            units.append(line[:cut])
            line = line[cut:].lstrip()
        units.append(line)
    return _pack(units, limit, "\n")


def _pack(units: list[str], limit: int, joiner: str) -> list[str]:
    """Собирает единицы подряд, пока часть не превысит потолок."""
    parts: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}{joiner}{unit}" if current else unit
        if current and len(candidate) > limit:
            parts.append(current)
            current = unit
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
