"""Эталонные вопросы генератора: ожидания пересчитываются из файлов набора своим кодом,
проверка `forbidden` ловит порчу, разметка указывает на настоящие фрагменты сервиса.

Тесты идут на двух наборах — с зерном `data/` и со вторым — и не знают их содержимого.
"""

import csv
import json
import shutil
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from assistant.ingest.chunks import split_document
from datagen.check import check_dataset, check_traps
from datagen.questions import CASES, Question, accessible_text, build_questions
from datagen.render import render
from datagen.text import normalize
from datagen.world import DEFAULT_SEED, HOLDOUT_SEED, SECOND_SEED, World, build_world

Dataset = tuple[Path, World]


def _lines(root: Path, name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (root / name).read_text(encoding="utf-8").splitlines()]


def _table(root: Path, name: str) -> list[dict[str, str]]:
    with (root / f"{name}.csv").open(encoding="utf-8") as file:
        return list(csv.DictReader(file))


def _questions(world: World) -> dict[str, Question]:
    return {question.qid: question for question in build_questions(world, render(world))}


def _expected(root: Path, qid: str) -> dict[str, Any]:
    return next(record for record in _lines(root, "questions.jsonl") if record["id"] == qid)


def _by_case(world: World, case: str) -> list[Question]:
    return [question for question in _questions(world).values() if question.case == case]


def _tenths(minutes: int) -> str:
    hours = Decimal(minutes) / 60
    return str(hours.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _within(row: dict[str, str], basis: dict[str, Any]) -> bool:
    start: date = basis["date_from"]
    end: date = basis["date_to"]
    return start.isoformat() <= row["date"] <= end.isoformat()


def _tonnes(production: list[dict[str, str]], basis: dict[str, Any]) -> list[int]:
    """Сменная добыча участка за период вопроса: весь период или неделя из него."""
    return [
        int(row["tonnes"])
        for row in production
        if row["site"] == basis["site"] and _within(row, basis)
    ]


def _downtime_minutes(root: Path, site: str, reason: str) -> int:
    """Минуты внутри календарных дней периода из production.csv: вопрос называет даты."""
    dates = sorted({row["date"] for row in _table(root, "production")})
    begin = datetime.fromisoformat(dates[0])
    end = datetime.fromisoformat(dates[-1]) + timedelta(days=1)
    total = 0
    for row in _table(root, "downtime"):
        if row["site"] == site and row["reason"] == reason:
            start = max(datetime.fromisoformat(row["start"]), begin)
            finish = min(datetime.fromisoformat(row["end"]), end)
            total += max(int((finish - start).total_seconds()) // 60, 0)
    return total


def test_kpi_expectations_are_recomputed_from_files(dataset: Dataset) -> None:
    root, world = dataset
    production = _table(root, "production")
    total, lowest, mean = _by_case(world, "kpi_calc")
    tonnes = [int(row["tonnes"]) for row in production if row["site"] == total.basis["site"]]
    assert _expected(root, total.qid)["must_include"] == [str(sum(tonnes))]
    tonnes = [int(row["tonnes"]) for row in production if row["site"] == lowest.basis["site"]]
    assert _expected(root, lowest.qid)["must_include"] == [str(min(tonnes))]
    # Минимум — смена с провалом добычи, автор — начальник этого участка.
    assert min(tonnes) * 2 < sorted(tonnes)[len(tonnes) // 2]
    tonnes = _tonnes(production, mean.basis)
    average = (Decimal(sum(tonnes)) / len(tonnes)).quantize(Decimal("0.1"), ROUND_HALF_UP)
    assert _expected(root, mean.qid)["must_include"] == [str(average)]

    (plan,) = _by_case(world, "kpi_and_doc")
    produced = sum(_tonnes(production, plan.basis))
    days = len({row["date"] for row in production if _within(row, plan.basis)})
    planned = world.shift_plan[plan.basis["site"]] * 2 * days
    percent = (Decimal(produced) * 100 / planned).quantize(Decimal("0.1"), ROUND_HALF_UP)
    assert _expected(root, plan.qid)["must_include"] == [str(percent)]

    (similar,) = _by_case(world, "kpi_not_similar_doc")
    minutes = _downtime_minutes(root, similar.basis["site"], similar.basis["reason"])
    assert _expected(root, similar.qid)["must_include"] == [_tenths(minutes)]


def test_numbers_of_similar_document_differ_from_kpi(dataset: Dataset) -> None:
    # Верный ответ — из КХД: в доступной записке за прошлый месяц его нет.
    root, world = dataset
    (similar,) = _by_case(world, "kpi_not_similar_doc")
    memo = (root / "docs" / f"{world.docs['memo'].doc_id}.md").read_text(encoding="utf-8")
    assert normalize(similar.must_include[0]) not in normalize(memo)


def test_refusal_numbers_are_real_rows_of_foreign_data(dataset: Dataset) -> None:
    root, world = dataset
    own, foreign = _by_case(world, "foreign_site_in_session")
    production = _table(root, "production")

    def tonnes(basis: dict[str, Any]) -> str:
        return next(
            row["tonnes"]
            for row in production
            if row["site"] == basis["site"]
            and row["date"] == basis["day"].isoformat()
            and row["shift"] == str(basis["shift"])
        )

    assert _expected(root, own.qid)["must_include"] == [tonnes(own.basis)]
    assert _expected(root, foreign.qid)["forbidden"] == [tonnes(foreign.basis)]
    assert own.session == foreign.session and own.basis["site"] != foreign.basis["site"]
    (table,) = _by_case(world, "table_not_in_kpi")
    minutes = _downtime_minutes(root, table.basis["site"], table.basis["reason"])
    assert _expected(root, table.qid)["forbidden"] == [_tenths(minutes)]


def test_no_data_date_is_outside_the_period(dataset: Dataset) -> None:
    root, world = dataset
    dates = {row["date"] for row in _table(root, "production")}
    for question in _by_case(world, "no_data"):
        if "day" in question.basis:
            assert question.basis["day"].isoformat() not in dates


def test_action_params_follow_the_repair_order(dataset: Dataset) -> None:
    root, world = dataset
    (action,) = _by_case(world, "action_own")
    (reaction,) = _by_case(world, "reaction_time")
    # Срок в прошлом ходе сессии — для другого приоритета: подсказки нет.
    assert reaction.session == action.session
    assert action.action is not None
    priority = action.action["params"]["priority"]
    assert f"приоритетом {priority}" not in reaction.formulations[0]
    record = _expected(root, action.qid)
    params = record["action"]["params"]
    equipment = {row["equipment_id"]: row["site"] for row in _table(root, "equipment")}
    users = {user["user_id"]: user for user in json.loads((root / "users.json").read_text())}
    assert equipment[params["equipment_id"]] in users[record["user"]]["repair_requests"]
    # Приоритет — из условий раздела 2 порядка оформления заявок: описание поломки
    # подходит под условие строки этого приоритета и только её.
    repair = (root / "docs" / f"{world.docs['repair'].doc_id}.md").read_text(encoding="utf-8")
    rows = (
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in repair.splitlines()
        if line.startswith("| ")
    )
    conditions = {int(cells[0]): cells[1] for cells in rows if cells[0].isdigit()}
    markers = {1: ("стоит", "встал"), 2: ("ограничени", "вполсилы"), 3: ("не влияет", "не мешает")}
    assert set(conditions) == set(markers)
    for formulation in action.formulations:
        found = {p for p, words in markers.items() if any(w in formulation for w in words)}
        assert found == {params["priority"]}, formulation
    assert params["equipment_id"] in record["question"]
    (foreign,) = _by_case(world, "action_foreign")
    target = next(word.rstrip(":") for word in foreign.formulations[0].split() if "-" in word)
    assert equipment[target] not in users[foreign.user]["repair_requests"]


def test_every_case_of_the_assignment_is_covered(dataset: Dataset) -> None:
    _, world = dataset
    cases = [question.case for question in _questions(world).values()]
    assert set(CASES) <= set(cases)
    assert cases.count("no_data") >= 2


def test_forbidden_is_absent_from_data_of_the_author(dataset: Dataset) -> None:
    root, world = dataset
    files = render(world)
    users = {user["user_id"]: user for user in json.loads(files["users.json"])}
    for record in _lines(root, "questions.jsonl"):
        text = accessible_text(files, users[record["user"]])
        for value in record["forbidden"]:
            assert normalize(value) not in text, (record["id"], value)


def test_forbidden_check_catches_a_leak(dataset: Dataset, tmp_path: Path) -> None:
    # Строка forbidden, подложенная в документ «всем», — ложная утечка: make data падает.
    root, world = dataset
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    record = _expected(root, next(q.qid for q in _by_case(world, "foreign_site_in_session")[1:]))
    handover = copy / "docs" / f"{world.docs['handover'].doc_id}.md"
    handover.write_text(
        handover.read_text(encoding="utf-8") + f"\nСправочно: {record['forbidden'][0]}.\n",
        encoding="utf-8",
    )
    problems = check_dataset(copy)
    assert any("forbidden" in problem and record["id"] in problem for problem in problems)


def test_variants_keep_expectations_and_sessions(dataset: Dataset) -> None:
    root, _ = dataset
    main = {record["id"]: record for record in _lines(root, "questions.jsonl")}
    variants = _lines(root, "questions.variants.jsonl")
    numbers: dict[str, list[int]] = {}
    for record in variants:
        base, number = record["id"].rsplit(".", 1)
        numbers.setdefault(base, []).append(int(number))
        original = main[base]
        for key in ("user", "expect", "must_include", "must_cite", "forbidden", "action"):
            assert record.get(key) == original.get(key), (record["id"], key)
        if "session" in original:
            assert record["session"] == f"{original['session']}-v{number}"
    assert set(numbers) == set(main)
    assert all(sorted(found) == list(range(1, len(found) + 1)) for found in numbers.values())
    assert all(len(found) >= 2 for found in numbers.values())
    # Первая формулировка — та, что в questions.jsonl.
    for record in variants:
        if record["id"].endswith(".1"):
            assert record["question"] == main[record["id"][:-2]]["question"]


def test_labels_point_to_real_chunks_of_the_service(dataset: Dataset) -> None:
    root, _ = dataset
    labels = json.loads((root / "questions.labels.json").read_text(encoding="utf-8"))
    main = {record["id"]: record for record in _lines(root, "questions.jsonl")}
    assert set(labels) == set(main)
    for qid, items in labels.items():
        for item in items:
            doc = (root / "docs" / f"{item['doc_id']}.md").read_text(encoding="utf-8")
            body = doc.split("---", 2)[2]
            chunks = [piece.chunk_id for piece in split_document(item["doc_id"], body)]
            prefix = item["chunk_prefix"]
            assert any(chunk == prefix or chunk.startswith(f"{prefix}.") for chunk in chunks), (
                qid,
                prefix,
            )
        # Цитируемые документы размечены.
        cited = set(main[qid]["must_cite"])
        assert cited <= {item["doc_id"] for item in items}, qid


def test_questions_do_not_shift_the_dataset(dataset: Dataset) -> None:
    # Вопросы берут свой генератор случайных чисел: файлы набора от них не зависят.
    root, world = dataset
    files = render(world)
    for name, text in files.items():
        if not name.startswith("questions"):
            assert (root / name).read_text(encoding="utf-8") == text


@pytest.mark.parametrize("seed", range(40))
def test_any_seed_gives_a_valid_question_set(seed: int, tmp_path: Path) -> None:
    from datagen.render import write_dataset

    world = build_world(seed)
    write_dataset(world, tmp_path)
    assert check_dataset(tmp_path) == []
    assert check_traps(tmp_path, world) == []


def test_edited_question_file_is_caught(dataset: Dataset, tmp_path: Path) -> None:
    # Файл вопросов, поправленный руками, не совпадает с выходом генератора — make data падает.
    root, world = dataset
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    path = copy / "questions.jsonl"
    records = _lines(copy, "questions.jsonl")
    records[1]["must_include"] = ["12345"]
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    assert any("questions.jsonl" in problem for problem in check_traps(copy, world))


def test_short_expectations_are_not_bare_digits(dataset: Dataset) -> None:
    # Голая одна-две цифры нашлись бы почти в любом ответе.
    root, _ = dataset
    for record in _lines(root, "questions.jsonl"):
        for value in [*record["must_include"], *record["forbidden"]]:
            assert not (value.isdigit() and len(value) <= 2), (record["id"], value)


@pytest.mark.parametrize("seed", [DEFAULT_SEED, SECOND_SEED, HOLDOUT_SEED])
def test_tenths_expectations_are_not_a_prefix_of_a_wrong_answer(seed: int) -> None:
    # «96» подстрокой есть в неверном «96.4 %»: вопросы до десятых ждут «96.4», а не «96».
    world = build_world(seed)
    tenths = [question for question in _questions(world).values() if question.tenths]
    assert {question.case for question in tenths} == {
        "kpi_calc",
        "kpi_and_doc",
        "kpi_not_similar_doc",
    }
    for question in tenths:
        (value,) = question.must_include
        whole, _, fraction = value.partition(".")
        assert whole.isdigit() and len(fraction) == 1 and fraction != "0", (question.qid, value)


def test_tenths_check_catches_a_whole_number(
    dataset: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = dataset
    built = build_questions(world, render(world))
    spoiled = tuple(
        replace(question, must_include=(question.must_include[0].split(".")[0],))
        if question.tenths
        else question
        for question in built
    )
    monkeypatch.setattr("datagen.check.build_questions", lambda *_: spoiled)
    problems = check_traps(root, world)
    for question in spoiled:
        if question.tenths:
            assert any(question.qid in p and "десятыми" in p for p in problems), question.qid


def test_holdout_seed_is_neither_data_nor_test_seed() -> None:
    assert HOLDOUT_SEED not in (DEFAULT_SEED, SECOND_SEED)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Виноградова Наталья Борисовна", "Виноградов"),
        ("Соколов Дмитрий Андреевич", "Соколов"),
        ("Воробьёва Светлана Дмитриевна", "Воробь"),
        ("Фёдоров Алексей Михайлович", "доров"),
        # Кусок без «ё» короче пяти букв нашёлся бы в любом ответе: фамилии в ожидании нет.
        ("Семёнова Ирина Олеговна", None),
    ],
)
def test_surname_part_matches_declension_and_e_spelling(name: str, expected: str | None) -> None:
    from datagen.questions import _surname

    assert _surname(name) == expected


def test_attacks_are_valid_tool_calls(dataset: Dataset) -> None:
    """Атака для мутантов — вызовы в формате схем инструментов; у каждого вопроса — случай."""
    from pydantic import BaseModel

    from assistant.tools.schemas import DocumentArgs, KpiArgs, RepairArgs, SearchArgs

    root = dataset[0]
    attacks = json.loads((root / "questions.attacks.json").read_text(encoding="utf-8"))
    assert set(attacks) == {record["id"] for record in _lines(root, "questions.jsonl")}
    schemas: dict[str, type[BaseModel]] = {
        "search_documents": SearchArgs,
        "get_document": DocumentArgs,
        "query_kpi": KpiArgs,
        "create_repair_request": RepairArgs,
    }
    for qid, item in attacks.items():
        assert item["case"], qid
        for call in item["calls"]:
            schemas[call["tool"]].model_validate(call["arguments"])
