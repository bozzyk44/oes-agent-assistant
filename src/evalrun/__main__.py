"""Прогон эталонных вопросов.

    python -m evalrun                    прогон по запущенному сервису: results.jsonl и report.md
    python -m evalrun report [results]   report.md из results.jsonl, без обращения к сервису
    python -m evalrun compare A B        сравнение двух прогонов попарно по вопросам

Окружение: ASSISTANT_URL, TOIR_URL, TOIR_TOKEN, QUESTIONS, DATA_DIR, EVAL_DIR,
EVAL_ATTEMPTS (по умолчанию 3), PRICE_INPUT, PRICE_CACHED, PRICE_OUTPUT.
"""

import os
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from evalrun.api import Service, Toir, urllib_transport
from evalrun.compare import NotComparable, compare
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


def run(env: Mapping[str, str]) -> int:
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
    transport = urllib_transport(HTTP_TIMEOUT)
    runner = Runner(
        Service(env.get("ASSISTANT_URL", "http://assistant:8000"), transport),
        Toir(env.get("TOIR_URL", "http://toir:8001"), env.get("TOIR_TOKEN", ""), transport),
        progress=lambda attempt, qid, ok: print(
            f"прогон: попытка {attempt} {qid} {'+' if ok else '-'}", flush=True
        ),
    )
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
    runner.run(questions, plan, archive, lambda: datetime.now(UTC))
    # Итог по контракту задания — после полного прогона: оборванный не затирает прошлый.
    shutil.copyfile(archive, out_dir / "results.jsonl")
    write_report(out_dir / "results.jsonl", out_dir / "report.md")
    print(f"прогон: {out_dir / 'results.jsonl'}, {out_dir / 'report.md'}")
    return 0


def write_report(results: Path, out: Path) -> None:
    config, attempts = read(results)
    out.write_text(build(config, attempts), encoding="utf-8")


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
