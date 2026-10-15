"""`report.md` из `results.jsonl` — без обращения к сервису: формат отчёта меняется
и пересобирается, не тратя вызовов модели.

Вопрос — все его попытки и формулировки (`q01.2` идёт в `q01`). Статус: проходит стабильно,
нестабилен, не проходит стабильно. «Доля прошедших» — строго, по вопросам, прошедшим во
всех попытках; рядом — средняя по попыткам. Утечка — по худшей попытке.
"""

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evalrun.checks import rejudge
from evalrun.diagnostics import diagnose, fragment_in_input

STABLE = "проходит стабильно"
UNSTABLE = "нестабилен"
FAILING = "не проходит стабильно"
OUTCOMES = ("answer", "refusal", "no_data", "action", "budget_exceeded", "error")


def read(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("type") == "config":
            config = item
        elif item.get("type") == "attempt":
            attempts.append(_refreshed(item))
    return config, attempts


def _refreshed(line: dict[str, Any]) -> dict[str, Any]:
    """Диагностика пересчитывается из сохранённой записи хода: новая метрика появляется
    и в отчёте старого прогона, без вызовов модели. Записи нет — остаётся сохранённая."""
    line = rejudge(line)
    record = line.get("turn_record")
    if not isinstance(record, dict):
        return line
    response = line.get("response")
    answer = str(response.get("answer") or "") if isinstance(response, dict) else ""
    question = str((line.get("question") or {}).get("question") or "")
    return {**line, "diagnostics": diagnose(record, question, answer)}


def base_id(line: dict[str, Any]) -> str:
    return str(line["question"]["id"]).split(".", 1)[0]


def statuses(attempts: list[dict[str, Any]]) -> dict[str, str]:
    by_question: dict[str, list[bool]] = defaultdict(list)
    for line in attempts:
        by_question[base_id(line)].append(bool(line["passed"]))
    return {
        qid: STABLE if all(results) else FAILING if not any(results) else UNSTABLE
        for qid, results in by_question.items()
    }


def percentile(values: list[float], share: float) -> float | None:
    """Ближайший ранг: значение, не меньше которого `share` наблюдений."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(math.ceil(share * len(ordered)), 1)
    return ordered[rank - 1]


def _share(part: int, whole: int) -> str:
    return f"{part} из {whole}" + (f" ({100 * part / whole:.0f} %)" if whole else "")


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _fmt(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _usage(line: dict[str, Any]) -> dict[str, Any] | None:
    response = line.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        usage: dict[str, Any] = response["usage"]
        return usage
    return None


def cost(attempts: list[dict[str, Any]], prices: dict[str, Any]) -> dict[str, Any]:
    usages = [usage for line in attempts if (usage := _usage(line)) is not None]
    inputs = [int(u.get("input_tokens") or 0) for u in usages]
    outputs = [int(u.get("output_tokens") or 0) for u in usages]
    cached_known = [u for u in usages if u.get("cached_input_tokens") is not None]
    cached = sum(int(u["cached_input_tokens"]) for u in cached_known)
    known_input = sum(int(u.get("input_tokens") or 0) for u in cached_known)
    latencies = [
        float(u.get("latency_ms") or line["latency_ms"])
        for line in attempts
        if (u := _usage(line)) is not None
    ]
    price = None
    if usages and all(prices.get(name) is not None for name in ("input", "cached", "output")):
        totals = [
            (
                (int(u.get("input_tokens") or 0) - int(u.get("cached_input_tokens") or 0))
                * float(prices["input"])
                + int(u.get("cached_input_tokens") or 0) * float(prices["cached"])
                + int(u.get("output_tokens") or 0) * float(prices["output"])
            )
            / 1_000_000
            for u in usages
        ]
        price = sum(totals) / len(totals)
    return {
        "turns": len(usages),
        "calls_mean": _mean(int(u.get("model_calls") or 0) for u in usages),
        "input_mean": _mean(inputs),
        "input_max": max(inputs) if inputs else None,
        "max_call_input": max(
            (int(u.get("max_call_input_tokens") or 0) for u in usages), default=None
        ),
        "cached_share": cached / known_input if cached_known and known_input else None,
        "output_mean": _mean(outputs),
        "latency_p50": percentile(latencies, 0.5),
        "latency_p95": percentile(latencies, 0.95),
        "price_mean": price,
    }


def build(config: dict[str, Any], attempts: list[dict[str, Any]]) -> str:
    lines: list[str] = ["# Прогон эталонных вопросов", ""]
    lines += _config(config)
    status = statuses(attempts)
    lines += _summary(attempts, status)
    lines += _cost(cost(attempts, config.get("prices") or {}))
    lines += _questions(attempts, status)
    lines += _diagnostics(attempts)
    lines += _retrieval(attempts)
    lines += _rules(attempts)
    return "\n".join(lines).rstrip() + "\n"


def _config(config: dict[str, Any]) -> list[str]:
    rows = [
        ("Запуск", config.get("run_id")),
        ("Модель", config.get("model")),
        ("Адрес провайдера", config.get("model_base_url")),
        ("prefix_hash", config.get("prefix_hash")),
        ("Коммит", config.get("commit")),
        ("Хэш набора", config.get("dataset_hash")),
        ("Файл вопросов", config.get("questions_file")),
        ("Хэш вопросов", config.get("questions_hash")),
        ("Выборка", json.dumps(config.get("sampling"), ensure_ascii=False)),
        ("Попыток", config.get("attempts")),
        (
            "Цены, $ за 1 млн: вход, кэш, выход",
            json.dumps(config.get("prices"), ensure_ascii=False),
        ),
    ]
    out = ["## Конфигурация", "", "| Поле | Значение |", "|---|---|"]
    out += [f"| {name} | {'—' if value in (None, '') else value} |" for name, value in rows]
    return [*out, ""]


def _table(rows: list[tuple[str, str]]) -> list[str]:
    return ["| Метрика | Значение |", "|---|---|", *(f"| {k} | {v} |" for k, v in rows), ""]


def _summary(attempts: list[dict[str, Any]], status: dict[str, str]) -> list[str]:
    total = len(status)
    stable = sum(1 for value in status.values() if value == STABLE)
    by_attempt: dict[int, list[bool]] = defaultdict(list)
    for line in attempts:
        by_attempt[int(line["attempt"])].append(bool(line["passed"]))
    mean = _mean(sum(items) / len(items) for items in by_attempt.values() if items)
    leak_questions = {base_id(line) for line in attempts if line.get("leak")}
    leak_attempts = sum(1 for line in attempts if line.get("leak"))
    correct = sum(1 for line in attempts if _check(line, "outcome"))
    toir_broken = sum(1 for line in attempts if _check(line, "toir") is False)
    mean_text = _fmt(None if mean is None else 100 * mean, 0)
    return [
        "## Сводка",
        "",
        *_table(
            [
                ("Доля прошедших — во всех попытках", _share(stable, total)),
                ("Средняя доля прошедших по попыткам", f"{mean_text} %"),
                ("Утечки: вопросов / попыток", f"{len(leak_questions)} / {leak_attempts}"),
                ("Точность outcome по попыткам", _share(correct, len(attempts))),
                ("Заявки в ТОиР без подтверждения, попыток", str(toir_broken)),
            ]
        ),
    ]


def _check(line: dict[str, Any], name: str) -> bool | None:
    for item in line.get("checks", []):
        if item.get("name") == name:
            return bool(item.get("passed"))
    return None


def _cost(values: dict[str, Any]) -> list[str]:
    cached = values["cached_share"]
    price = values["price_mean"]
    input_text = f"{_fmt(values['input_mean'], 0)} / {values['input_max'] or '—'}"
    latency = f"{_fmt(values['latency_p50'], 0)} / {_fmt(values['latency_p95'], 0)}"
    return [
        "## Стоимость хода",
        "",
        *_table(
            [
                ("Ходов с расходом", str(values["turns"])),
                ("Вызовов модели на ход, среднее", _fmt(values["calls_mean"], 2)),
                ("Вход на ход, среднее / максимум", input_text),
                ("Наибольший вход одного вызова", str(values["max_call_input"] or "—")),
                (
                    "Доля кэшированного входа",
                    "провайдер не отдаёт" if cached is None else f"{100 * cached:.1f} %",
                ),
                ("Выход на ход, среднее", _fmt(values["output_mean"], 0)),
                ("Задержка p50 / p95, мс", latency),
                ("Цена хода, $", "цены не заданы" if price is None else f"{price:.6f}"),
            ]
        ),
    ]


def _questions(attempts: list[dict[str, Any]], status: dict[str, str]) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in attempts:
        grouped[base_id(line)].append(line)
    out = [
        "## По вопросам",
        "",
        "| Вопрос | Ожидается | Статус | Прошло попыток | Утечка | Почему не прошёл |",
        "|---|---|---|---|---|---|",
    ]
    for qid, lines in grouped.items():
        reasons = Counter(
            f"{item['name']}: {item['reason']}" if item.get("reason") else item["name"]
            for line in lines
            for item in line.get("checks", [])
            if not item.get("passed")
        )
        why = "; ".join(f"{reason} ×{count}" for reason, count in reasons.items()) or "—"
        ok = sum(1 for line in lines if line["passed"])
        leak = "да" if any(line.get("leak") for line in lines) else "нет"
        expect = lines[0]["question"].get("expect")
        cells = [qid, str(expect), status[qid], f"{ok} из {len(lines)}", leak, _cell(why)]
        out.append("| " + " | ".join(cells) + " |")
    return [*out, ""]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _numeric(line: dict[str, Any]) -> bool:
    question = line["question"]
    expected = " ".join(question.get("must_include", []))
    return question.get("expect") == "answer" and any(ch.isdigit() for ch in expected)


def _diagnostics(attempts: list[dict[str, Any]]) -> list[str]:
    matrix: Counter[tuple[str, str]] = Counter()
    for line in attempts:
        raw = line.get("response")
        response: dict[str, Any] = raw if isinstance(raw, dict) else {}
        expected = str(line["question"].get("expect"))
        matrix[(expected, str(response.get("outcome", "нет ответа")))] += 1
    actual = [name for name in (*OUTCOMES, "нет ответа") if any(key[1] == name for key in matrix)]
    out = ["## Диагностика", "", "Матрица исходов: строки — ожидаемый, столбцы — полученный.", ""]
    out.append("| | " + " | ".join(actual) + " |")
    out.append("|---|" + "---|" * len(actual))
    for expected in OUTCOMES:
        if any(key[0] == expected for key in matrix):
            counts = " | ".join(str(matrix[(expected, name)]) for name in actual)
            out.append(f"| {expected} | {counts} |")
    out.append("")

    diags = [line["diagnostics"] for line in attempts if line.get("diagnostics")]
    out += [f"Запись хода прочитана в {_share(len(diags), len(attempts))} попыток.", ""]
    if not diags:
        return out
    tool_calls = sum(d["tool_calls"] for d in diags)
    numeric = [
        line["diagnostics"] for line in attempts if line.get("diagnostics") and _numeric(line)
    ]
    paths = Counter(str(d.get("parse_path")) for d in diags)
    numbers = sum(d["numbers"] for d in diags)
    grounded = sum(d["numbers_grounded"] for d in diags)
    turns = len(diags)
    return out + _table(
        [
            ("Код заменил outcome модели, ходов", _share(_sum(diags, "outcome_replaced"), turns)),
            (
                "Код заменил текст отказа с числом без опоры, ходов",
                _share(_sum(diags, "refusal_replaced"), turns),
            ),
            ("Отброшено цитат как не бывших во входе", str(_sum(diags, "dropped_citations"))),
            ("Ходов, остановленных бюджетом", _share(_sum(diags, "budget_stop"), turns)),
            ("Вызовов инструментов не по схеме", _share(_sum(diags, "invalid_calls"), tool_calls)),
            ("Вызовов инструментов вопреки запрету", str(_sum(diags, "tool_calls_after_ban"))),
            ("Ответов модели с вызовом инструмента в тексте", str(_sum(diags, "calls_in_text"))),
            ("Путь разбора итога", ", ".join(f"{k}: {v}" for k, v in paths.most_common())),
            (
                "Ходов с calculate среди вопросов с числом в ожидании",
                _share(_sum(numeric, "calculate_used"), len(numeric)),
            ),
            ("Опора чисел ответа на результаты инструментов или вопрос", _share(grounded, numbers)),
            (
                "Опора входов расчёта на результаты инструментов хода или вопрос",
                _share(_sum(diags, "calculate_inputs_grounded"), _sum(diags, "calculate_inputs")),
            ),
        ]
    )


def _sum(diags: list[dict[str, Any]], name: str) -> int:
    return sum(int(d[name]) for d in diags)


def _category(question: dict[str, Any]) -> str:
    if len(question.get("must_cite", [])) >= 2:
        return "два документа"
    return str(question.get("expect"))


def _retrieval(attempts: list[dict[str, Any]]) -> list[str]:
    rows = [line for line in attempts if line.get("diagnostics")]
    out = ["## Поиск", ""]
    if not rows:
        return [*out, "Записей ходов нет — метрики поиска не считаются.", ""]
    cited = [line for line in rows if line["question"].get("must_cite")]
    doc_hits = [
        set(line["question"]["must_cite"]) <= set(line["diagnostics"]["docs_in_input"])
        for line in cited
    ]
    labeled = [line for line in rows if line.get("labels")]
    hits = {
        id(line): fragment_in_input(tuple(line["labels"]), line["diagnostics"]["chunks_in_input"])
        for line in labeled
    }
    by_category: dict[str, list[bool]] = defaultdict(list)
    for line in labeled:
        by_category[_category(line["question"])].append(bool(hits[id(line)]))
    false_refusals = [
        line
        for line in rows
        if line["question"].get("expect") == "answer"
        and isinstance(line.get("response"), dict)
        and line["response"].get("outcome") in ("refusal", "no_data")
    ]
    seen = sum(1 for line in false_refusals if hits.get(id(line)))
    searches = _sum([line["diagnostics"] for line in rows], "searches")
    empty = _sum([line["diagnostics"] for line in rows], "empty_searches")
    repeated = sum(1 for line in rows if line["diagnostics"]["searches"] >= 2)
    fragments = sum(1 for value in hits.values() if value)
    table = [
        ("Документы из must_cite во входе хода", _share(sum(doc_hits), len(doc_hits))),
        ("Размеченные фрагменты во входе хода", _share(fragments, len(hits))),
        *(
            (f"— категория «{name}»", _share(sum(values), len(values)))
            for name, values in sorted(by_category.items())
        ),
        (
            "Ложные отказы на вопрос с ответом",
            f"{len(false_refusals)}: фрагмент был во входе — {seen},"
            f" не был или нет разметки — {len(false_refusals) - seen}",
        ),
        ("Поисков с пустой выдачей", _share(empty, searches)),
        ("Ходов с повторным поиском", _share(repeated, len(rows))),
    ]
    return out + _table(table)


def _rules(attempts: list[dict[str, Any]]) -> list[str]:
    """Где вердикт строгого сравнения чисел расходится с правилом задания (решение 18)."""
    ruled = [line for line in attempts if "task_rule" in line]
    out = [
        "## Сравнение чисел строже задания",
        "",
        "Вердикт прогона — числа целиком (решение 18); правило задания — подстрока.",
        "",
    ]
    if not ruled:
        return [*out, "Ответов для пересчёта нет.", ""]
    task = [{**line, "passed": line["task_rule"]["passed"]} for line in ruled]
    strict_status, task_status = statuses(ruled), statuses(task)
    out += _table(
        [
            (
                "Доля прошедших во всех попытках: строго / по заданию",
                f"{_share(_stable(strict_status), len(strict_status))}"
                f" / {_share(_stable(task_status), len(task_status))}",
            ),
            (
                "Утечки, попыток: строго / по заданию",
                f"{sum(1 for line in ruled if line['leak'])}"
                f" / {sum(1 for line in ruled if line['task_rule']['leak'])}",
            ),
        ]
    )
    rows = [(line, item) for line in ruled for item in line["task_rule"]["differs"]]
    if not rows:
        return [*out, "Вердикты правил не расходятся ни в одной попытке.", ""]
    out += [
        "| Вопрос | Попытка | Проверка | По заданию | Строго | Причина |",
        "|---|---|---|---|---|---|",
    ]
    for line, item in rows:
        cells = [
            str(line["question"]["id"]),
            str(line["attempt"]),
            item["check"],
            "проходит" if item["task"] else "не проходит",
            "проходит" if item["strict"] else "не проходит",
            _cell(item["reason"] or "—"),
        ]
        out.append("| " + " | ".join(cells) + " |")
    return [*out, ""]


def _stable(status: dict[str, str]) -> int:
    return sum(1 for value in status.values() if value == STABLE)
