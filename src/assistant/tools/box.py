"""Исполнитель вызовов инструментов: проверка аргументов, права через слой доступа,
единый конверт результата.

Конверт — строка заголовка JSON со статусом, данными и пояснением кода; текст документов
идёт после него размеченными блоками. Последовательности разметки в самом тексте
экранируются, чтобы документ не мог «закрыть» свой блок и продолжить от имени системы.
`denied` не несёт содержимого — ни названия, ни числа строк.

Результат во входе — не больше `TOOL_RESULT_TOKENS` по оценке хода. Не поместившиеся
фрагменты перечисляются идентификаторами, строки КХД — числом: продолжение запрашивается
тем же инструментом и проходит те же права. Что не делится на части, режется по символам
с явной пометкой.
"""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection

from assistant.access import (
    Chunk,
    Grants,
    KpiQuery,
    Status,
    get_document,
    query_rows,
    search,
)
from assistant.actions.draft import prepare_repair
from assistant.ingest.chunks import TOOL_RESULT_TOKENS
from assistant.tools.arith import NUMBER, BadExpression, evaluate, literals, normalize_digits
from assistant.tools.kpi import _number, round_half_up, run_kpi
from assistant.tools.schemas import (
    ARGUMENTS,
    CalculateArgs,
    DocumentArgs,
    KpiArgs,
    RepairArgs,
    SearchArgs,
)

BLOCK_OPEN = "<<<фрагмент"
BLOCK_CLOSE = "<<<конец фрагмента>>>"
# Документ целиком: оглавление и первые фрагменты в пределах результата.
DOCUMENT_CHUNKS = 4

Transaction = Callable[[], AbstractContextManager[Connection]]
Count = Callable[[str], int]
CUT_MARK = "\n[результат обрезан по лимиту результата инструмента]"


@dataclass
class ToolResult:
    """Что получил вызов: текст для модели и то, что код знает о нём."""

    status: Status
    content: str
    chunk_ids: tuple[str, ...] = ()
    best_match_closed: bool = False
    partially_denied: bool = False
    # Параметры черновика заявки — только при успешной подготовке.
    draft: dict[str, Any] | None = None
    record: dict[str, Any] = field(default_factory=dict)


def envelope(status: Status, data: Any = None, note: str = "", blocks: str = "") -> str:
    header: dict[str, Any] = {"status": status.value}
    if data is not None and status is Status.OK:
        header["data"] = data
    if note:
        header["note"] = note
    text = json.dumps(header, ensure_ascii=False)
    return f"{text}\n{blocks}" if blocks else text


def _escape(text: str) -> str:
    return text.replace("<<<", "‹‹‹").replace(">>>", "›››")


def _blocks(chunks: tuple[Chunk, ...]) -> str:
    parts = []
    for chunk in chunks:
        edition = "утратил силу" if chunk.status != "active" else "действует"
        if chunk.superseded_by:
            edition += f", заменён на {chunk.superseded_by}"
        head = (
            f"{BLOCK_OPEN} id={chunk.chunk_id} документ={chunk.doc_id}"
            f" «{_escape(chunk.title)}» дата={chunk.doc_date.isoformat()}"
            f" редакция: {edition}>>>"
        )
        heading = f"## {_escape(chunk.heading)}\n" if chunk.heading else ""
        parts.append(f"{head}\n{heading}{_escape(chunk.body)}\n{BLOCK_CLOSE}")
    return "\n".join(parts)


@dataclass
class ToolBox:
    """Инструменты одного хода. Каждый вызов — своя транзакция от имени пользователя."""

    transaction: Transaction
    grants: Grants
    # Текущее сообщение пользователя: идентификатор оборудования для заявки — только из него.
    message: str
    # Оценка токенов куска входа — та же, что у проверки бюджета хода.
    count: Count
    limit: int = TOOL_RESULT_TOKENS
    # Результаты `ok` этого хода в том виде, в каком их получила модель, — опора входов
    # расчёта. Результат, позже убранный из входа ради бюджета, остаётся опорой: число в нём
    # настоящее.
    sources: list[str] = field(default_factory=list)

    def run(self, name: str, raw_arguments: str) -> ToolResult:
        result = self._run(name, raw_arguments)
        if self.count(result.content) > self.limit:
            result.content = _cut(result.content, self.count, self.limit)
            result.record["cut"] = True
        if result.status is Status.OK:
            self.sources.append(normalize_digits(result.content))
        return result

    def _run(self, name: str, raw_arguments: str) -> ToolResult:
        model = ARGUMENTS.get(name)
        if model is None:
            return ToolResult(
                Status.INVALID, envelope(Status.INVALID, note=f"нет инструмента {name}")
            )
        try:
            arguments = json.loads(raw_arguments or "{}")
            args = model.model_validate(arguments)
        except ValueError as error:
            return ToolResult(Status.INVALID, envelope(Status.INVALID, note=_problem(error)))
        with self.transaction() as connection:
            match args:
                case SearchArgs():
                    return self._search(connection, args)
                case DocumentArgs():
                    return self._document(connection, args)
                case KpiArgs():
                    return self._kpi(connection, args)
                case CalculateArgs():
                    return _calculate(
                        args,
                        source_numbers(normalize_digits("\n".join([self.message, *self.sources]))),
                    )
                case RepairArgs():
                    return self._repair(connection, arguments)
        return ToolResult(Status.INVALID, envelope(Status.INVALID, note="неизвестные аргументы"))

    def _search(self, connection: Connection, args: SearchArgs) -> ToolResult:
        found = search(
            connection, self.grants, args.query, include_superseded=args.include_superseded
        )
        notes = []
        if found.best_match_closed:
            notes.append("Лучшее совпадение — в документе, закрытом для пользователя.")
        if any(chunk.supersedes for chunk in found.chunks) and not args.include_superseded:
            notes.append("У документа есть прежняя редакция; она утратила силу.")
        if found.status is Status.EMPTY:
            notes.append("Подходящих фрагментов в доступных документах нет.")
        content, shown = self._fit_chunks(found.status, {}, " ".join(notes), found.chunks)
        return ToolResult(
            found.status,
            content,
            chunk_ids=shown,
            best_match_closed=found.best_match_closed,
            record={"chunks": list(shown)},
        )

    def _document(self, connection: Connection, args: DocumentArgs) -> ToolResult:
        found = get_document(connection, self.grants, args.ref)
        if found.status is not Status.OK:
            note = "Доступа нет." if found.status is Status.DENIED else "Такого документа нет."
            return ToolResult(found.status, envelope(found.status, note=note))
        chunks = found.chunks
        note = ""
        data: dict[str, Any] = {}
        if "#" not in args.ref and len(chunks) > DOCUMENT_CHUNKS:
            data["contents"] = [
                {"chunk_id": chunk.chunk_id, "heading": chunk.heading} for chunk in chunks
            ]
            chunks = chunks[:DOCUMENT_CHUNKS]
            note = "Показаны первые фрагменты; остальные — по идентификатору из оглавления."
        content, shown = self._fit_chunks(Status.OK, data, note, chunks)
        return ToolResult(Status.OK, content, chunk_ids=shown, record={"chunks": list(shown)})

    def _fit_chunks(
        self, status: Status, data: dict[str, Any], note: str, chunks: tuple[Chunk, ...]
    ) -> tuple[str, tuple[str, ...]]:
        """Сколько фрагментов целиком помещается в лимит; остальные — идентификаторами."""
        for size in range(len(chunks), -1, -1):
            shown, rest = chunks[:size], chunks[size:]
            extra = (
                f" Не поместились в лимит результата: {', '.join(c.chunk_id for c in rest)};"
                " их можно запросить get_document по идентификатору."
                if rest
                else ""
            )
            body = {**data, "chunks": [chunk.chunk_id for chunk in shown]}
            content = envelope(status, body, (note + extra).strip(), _blocks(shown))
            if self.count(content) <= self.limit or not shown:
                return content, tuple(chunk.chunk_id for chunk in shown)
        raise AssertionError("недостижимо")

    def _kpi(self, connection: Connection, args: KpiArgs) -> ToolResult:
        answer = run_kpi(connection, self.grants, args)
        return ToolResult(
            answer.status,
            self._fit_rows(answer.status, answer.data, answer.note),
            partially_denied=answer.partially_denied,
            record={"note": answer.note},
        )

    def _fit_rows(self, status: Status, data: dict[str, Any], note: str) -> str:
        """Сколько строк или групп помещается в лимит — наибольшее число, поиском делением."""
        content = envelope(status, data, note)
        key = next((name for name in ("rows", "groups") if name in data), None)
        if key is None or self.count(content) <= self.limit:
            return content
        items = data[key]

        def build(size: int) -> str:
            left = len(items) - size
            extra = (
                f" Показаны {size} из {len(items)}; ещё {left} не поместились в лимит"
                " результата — сузьте фильтры или период."
            )
            body = {**data, key: items[:size], "more_rows": True}
            return envelope(status, body, (note + extra).strip())

        low, high = 0, len(items)
        while low < high:
            middle = (low + high + 1) // 2
            if self.count(build(middle)) <= self.limit:
                low = middle
            else:
                high = middle - 1
        return build(low)

    def _repair(self, connection: Connection, arguments: dict[str, Any]) -> ToolResult:
        draft = prepare_repair(connection, self.grants, self.message, arguments)
        if draft.status is Status.OK:
            return ToolResult(
                Status.OK, envelope(Status.OK, {"draft": draft.params}), draft=draft.params
            )
        notes = {
            Status.DENIED: "Оформлять заявку на это оборудование пользователю нельзя.",
            Status.NOT_FOUND: "Такого оборудования нет.",
        }
        note = draft.problem or notes.get(draft.status, "")
        if draft.status is Status.NOT_FOUND:
            note += _own_equipment(connection, self.grants)
        return ToolResult(draft.status, envelope(draft.status, note=note))


def _cut(content: str, count: Count, limit: int) -> str:
    """Последний рубеж: режет текст по символам, пока оценка не уляжется в лимит."""
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        if count(content[:middle] + CUT_MARK) <= limit:
            low = middle
        else:
            high = middle - 1
    return content[:low] + CUT_MARK


def _own_equipment(connection: Connection, grants: Grants) -> str:
    """Подсказка при промахе: оборудование участков, на которые пользователь оформляет заявки,
    — только из строк, которые ему и так видны. Права на таблицу нет — списка нет.
    """
    visible = query_rows(connection, grants, KpiQuery("equipment", limit=200))
    ids = sorted(row["equipment_id"] for row in visible.rows if row["site"] in grants.repair_sites)
    if visible.status is not Status.OK or not ids:
        return " Уточните идентификатор у пользователя."
    return f" Оборудование ваших участков: {', '.join(ids)}. Уточните у пользователя."


def _problem(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        first = error.errors(include_input=False, include_url=False)[0]
        place = ".".join(str(part) for part in first["loc"]) or "аргументы"
        return f"{place}: {first['msg']}"
    return "аргументы — не объект JSON"


# Постоянная перевода доли в проценты: законна без источника.
CONSTANTS = frozenset({"100"})


def _plain(value: Decimal) -> str:
    """Число как в тексте: без знака, без лишних нулей и без экспоненты."""
    return format(abs(value).normalize(), "f")


_DIGITS = re.compile(r"\d+")


def source_numbers(text: str) -> set[str]:
    """Числа источника целиком и их группы цифр: «14.09.2026» даёт 14.09, 14, 9 и 2026,
    «2 852,50» — 2852.5, 2852 и 50. Число входа опирается только на целое число или целую
    группу: «1143» на «114300» не опирается.
    """
    found = NUMBER.findall(text) + _DIGITS.findall(text)
    return {_plain(Decimal(item)) for item in found}


# Дата в тексте: «14.09.2026» или «2026-09-14». Иначе «14.09» читалось бы дробным числом,
# а в источнике та же дата могла быть записана по-другому.
_DATE = re.compile(r"(?<![\d.])\d{1,2}\.\d{1,2}\.\d{4}(?![\d.])|(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def unsourced_numbers(text: str, source: str) -> list[str]:
    """Числа текста, которых нет в источнике ни целиком, ни группой цифр, — по тому же
    правилу, что входы расчёта, без постоянных. Дата сверяется по частям: день, месяц, год."""
    known = source_numbers(normalize_digits(source))
    prepared = normalize_digits(text)
    parts = [part for date in _DATE.findall(prepared) for part in _DIGITS.findall(date)]
    numbers = NUMBER.findall(_DATE.sub(" ", prepared))
    found = dict.fromkeys(_plain(Decimal(item)) for item in [*parts, *numbers])
    return [item for item in found if item not in known]


def _unsourced(args: CalculateArgs, sources: set[str]) -> list[str]:
    """Входы расчёта, которых нет ни в результатах инструментов хода, ни в сообщении
    пользователя. Числа сравниваются после той же нормализации, что у прогона: «2852.5»
    опирается на «2 852,50» документа. Результат прежнего расчёта — тоже опора: его входы
    уже проверены.
    """
    if args.operation == "expression":
        values = literals(args.expression or "")
    else:
        values = list(args.numbers)
    inputs = dict.fromkeys(_plain(value) for value in values)
    return [item for item in inputs if item not in CONSTANTS and item not in sources]


def _calculate(args: CalculateArgs, sources: set[str]) -> ToolResult:
    numbers = args.numbers
    try:
        match args.operation:
            case "expression":
                value = evaluate(args.expression or "")
            case "sum" if numbers:
                value = sum(numbers, Decimal(0))
            case "difference" if len(numbers) == 2:
                value = numbers[0] - numbers[1]
            case "ratio" if len(numbers) == 2:
                value = numbers[0] / numbers[1]
            case "percent" if len(numbers) == 2:
                value = numbers[0] * 100 / numbers[1]
            case "sum":
                note = "sum: нужно хотя бы одно число"
                return ToolResult(Status.INVALID, envelope(Status.INVALID, note=note))
            case _:
                note = f"{args.operation}: нужны ровно два числа"
                return ToolResult(Status.INVALID, envelope(Status.INVALID, note=note))
        result = _number(round_half_up(value, args.decimals))
    except BadExpression as error:
        return ToolResult(Status.INVALID, envelope(Status.INVALID, note=f"expression: {error}"))
    except ZeroDivisionError:
        return ToolResult(Status.INVALID, envelope(Status.INVALID, note="деление на ноль"))
    except ArithmeticError:
        note = "результат вне точности расчёта"
        return ToolResult(Status.INVALID, envelope(Status.INVALID, note=note))
    if missing := _unsourced(args, sources):
        note = (
            f"нет источника у чисел {', '.join(missing)}: их нет в результатах инструментов"
            " этого хода и в сообщении пользователя. Получите их инструментом"
        )
        return ToolResult(
            Status.INVALID, envelope(Status.INVALID, note=note), record={"unsourced": missing}
        )
    return ToolResult(Status.OK, envelope(Status.OK, {"result": result}))


def chunk_markers(text: str) -> Iterator[str]:
    """Идентификаторы фрагментов в блоках результата — для цитат."""
    for line in text.splitlines():
        if line.startswith(BLOCK_OPEN):
            yield line.split("id=", 1)[1].split()[0]
