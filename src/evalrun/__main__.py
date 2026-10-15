"""Прогон эталонных вопросов.

    python -m evalrun                    прогон по запущенному сервису: results.jsonl и report.md
    python -m evalrun report [results]   report.md из results.jsonl, без обращения к сервису
    python -m evalrun compare A B        сравнение двух прогонов попарно по вопросам
    python -m evalrun judge-set          набор для проверки LLM-судьи из сохранённых прогонов
    python -m evalrun judge-check        проверка судьи на наборе — платно, с моделью судьи
    python -m evalrun judge R NAME       судья по прогону R (results.jsonl) — платно

Окружение: ASSISTANT_URL, TOIR_URL, TOIR_TOKEN, QUESTIONS, DATA_DIR, EVAL_DIR,
EVAL_ATTEMPTS (по умолчанию 3), PRICE_INPUT, PRICE_CACHED, PRICE_OUTPUT; для судьи —
JUDGE_MODEL, JUDGE_BASE_URL, JUDGE_API_KEY, JUDGE_EXTRA_BODY, JUDGE_PRICE_INPUT,
JUDGE_PRICE_OUTPUT.
"""

import json
import os
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from evalrun.api import Service, Toir, Transport, urllib_transport
from evalrun.compare import NotComparable, compare
from evalrun.judge import Judge
from evalrun.judgecheck import (
    Entry,
    build_set,
    check,
    check_report,
    judge_run,
    passed_criteria,
    run_report,
)
from evalrun.mounts import check_mounts
from evalrun.questions import QuestionError, file_hash, load
from evalrun.report import build, read
from evalrun.run import Plan, Prices, Runner, new_run_id

DEFAULT_ATTEMPTS = 3
HTTP_TIMEOUT = 180.0


def _price(env: Mapping[str, str], name: str) -> float | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"прогон: {name} — ожидалось число") from None


def _attempts(env: Mapping[str, str]) -> int:
    raw = env.get("EVAL_ATTEMPTS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_ATTEMPTS
    except ValueError:
        raise SystemExit("прогон: EVAL_ATTEMPTS — ожидалось целое число") from None
    if value < 1:
        raise SystemExit("прогон: EVAL_ATTEMPTS — не меньше одной попытки")
    return value


def run(env: Mapping[str, str], transport: Transport | None = None) -> int:
    questions_path = Path(env.get("QUESTIONS", "/data/questions.jsonl"))
    data_dir = Path(env.get("DATA_DIR", "/data"))
    out_dir = Path(env.get("EVAL_DIR", "/app/eval"))
    problems = check_mounts(questions_path, data_dir, out_dir)
    for problem in problems:
        print(f"прогон: {problem}", file=sys.stderr)
    if problems:
        return 1
    try:
        questions = load(questions_path)
    except QuestionError as error:
        print(f"прогон: {questions_path}: {error}", file=sys.stderr)
        return 1
    transport = transport or urllib_transport(HTTP_TIMEOUT)
    runner = Runner(
        Service(env.get("ASSISTANT_URL", "http://assistant:8000"), transport),
        Toir(env.get("TOIR_URL", "http://toir:8001"), env.get("TOIR_TOKEN", ""), transport),
        progress=lambda attempt, qid, ok: print(
            f"прогон: попытка {attempt} {qid} {'+' if ok else '-'}", flush=True
        ),
    )
    blocker = runner.preflight()
    if blocker:
        print(f"прогон: {blocker}", file=sys.stderr)
        return 1
    run_id = new_run_id()
    plan = Plan(
        run_id=run_id,
        attempts=_attempts(env),
        questions_file=questions_path.name,
        questions_hash=file_hash(questions_path),
        prices=Prices(
            _price(env, "PRICE_INPUT"), _price(env, "PRICE_CACHED"), _price(env, "PRICE_OUTPUT")
        ),
    )
    archive = out_dir / "runs" / f"{run_id}.jsonl"
    print(f"прогон {run_id}: {len(questions)} вопросов, попыток {plan.attempts}")
    lines = runner.run(questions, plan, archive, lambda: datetime.now(UTC))
    if all(line.get("status") == 0 for line in lines[1:]):
        print(
            f"прогон: сервис не ответил ни разу; итог не обновлён, запуск — {archive}",
            file=sys.stderr,
        )
        return 1
    # Итог по контракту задания — после полного прогона: оборванный не затирает прошлый.
    shutil.copyfile(archive, out_dir / "results.jsonl")
    write_report(out_dir / "results.jsonl", out_dir / "report.md")
    print(f"прогон: {out_dir / 'results.jsonl'}, {out_dir / 'report.md'}")
    return 0


def write_report(results: Path, out: Path) -> None:
    config, attempts = read(results)
    out.write_text(build(config, attempts), encoding="utf-8")


def _judge(env: Mapping[str, str], transport: Transport | None) -> Judge:
    model = env.get("JUDGE_MODEL", "").strip()
    base_url = env.get("JUDGE_BASE_URL", "").strip()
    if not model or not base_url:
        raise SystemExit("судья: нужны JUDGE_MODEL и JUDGE_BASE_URL (или MODEL_BASE_URL)")
    raw_extra = env.get("JUDGE_EXTRA_BODY", "").strip()
    try:
        extra = json.loads(raw_extra) if raw_extra else {}
    except ValueError:
        raise SystemExit("судья: JUDGE_EXTRA_BODY — ожидался объект JSON") from None
    if not isinstance(extra, dict):
        raise SystemExit("судья: JUDGE_EXTRA_BODY — ожидался объект JSON")
    return Judge(
        base_url,
        env.get("JUDGE_API_KEY", ""),
        model,
        transport or urllib_transport(HTTP_TIMEOUT),
        extra_body=extra,
    )


def _judge_prices(env: Mapping[str, str]) -> tuple[float, float] | None:
    prices = (_price(env, "JUDGE_PRICE_INPUT"), _price(env, "JUDGE_PRICE_OUTPUT"))
    return (prices[0], prices[1]) if prices[0] is not None and prices[1] is not None else None


def _jsonl(path: Path, items: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
    path.write_text(text, encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _progress(done: int, total: int) -> None:
    if done % 10 == 0 or done == total:
        print(f"судья: {done} из {total}", flush=True)


def judge_command(
    argv: list[str], env: Mapping[str, str], transport: Transport | None = None
) -> int:
    out_dir = Path(env.get("EVAL_DIR", "/app/eval"))
    folder = out_dir / "judge"
    command = argv[0]
    if command == "judge-set":
        manual_path = folder / "manual.json"
        manual = (
            json.loads(manual_path.read_text(encoding="utf-8")) if manual_path.is_file() else {}
        )
        sources = {
            "data": out_dir / "results.jsonl",
            "holdout": out_dir / "holdout-set" / "results.jsonl",
        }
        entries = build_set(
            {name: path for name, path in sources.items() if path.is_file()},
            manual.get("answers", {}),
            manual.get("instructions", {}),
        )
        _jsonl(folder / "set.jsonl", [entry.to_json() for entry in entries])
        print(f"судья: набор {folder / 'set.jsonl'}, ответов {len(entries)}")
        return 0
    judge = _judge(env, transport)
    prices = _judge_prices(env)
    if command == "judge-check":
        entries = [Entry.from_json(raw) for raw in _read_jsonl(folder / "set.jsonl")]
        calls = check(entries, judge, progress=_progress)
        _jsonl(folder / "check.jsonl", calls)
        (folder / "check.md").write_text(check_report(calls, judge.model, prices), encoding="utf-8")
        print(f"судья: {folder / 'check.md'}")
        return 0
    if command == "judge" and len(argv) == 3:
        results, source = Path(argv[1]), argv[2]
        names = passed_criteria(_read_jsonl(folder / "check.jsonl"))
        calls = judge_run(results, source, judge, names, progress=_progress)
        _jsonl(results.with_name("judge.jsonl"), calls)
        report = run_report(calls, names, judge.model, prices)
        results.with_name("judge.md").write_text(report, encoding="utf-8")
        print(f"судья: {results.with_name('judge.md')}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


def main(argv: list[str], env: Mapping[str, str]) -> int:
    command = argv[0] if argv else "run"
    if command == "run":
        return run(env)
    out_dir = Path(env.get("EVAL_DIR", "/app/eval"))
    if command == "report":
        results = Path(argv[1]) if len(argv) > 1 else out_dir / "results.jsonl"
        target = results.with_name("report.md") if len(argv) > 1 else out_dir / "report.md"
        write_report(results, target)
        print(f"отчёт: {target}")
        return 0
    if command.startswith("judge"):
        return judge_command(argv, env)
    if command == "compare" and len(argv) == 3:
        try:
            text = compare(Path(argv[1]), Path(argv[2]))
        except NotComparable as error:
            print(f"сравнение: {error}", file=sys.stderr)
            return 2
        target = out_dir / "compare.md"
        target.write_text(text, encoding="utf-8")
        print(text)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
