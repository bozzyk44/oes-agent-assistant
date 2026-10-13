"""Прогон эталонных вопросов: `make eval`. Каркас — сам прогон появляется отдельной задачей."""

import os
import sys
from pathlib import Path

from evalrun.mounts import check_mounts


def main() -> int:
    questions = Path(os.environ.get("QUESTIONS", "/data/questions.jsonl"))
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    out_dir = Path(os.environ.get("EVAL_DIR", "/app/eval"))
    problems = check_mounts(questions, data_dir, out_dir)
    for problem in problems:
        print(f"прогон: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"прогон: вопросы {questions}, набор {data_dir}, результаты {out_dir}")
    print("прогон эталонных вопросов ещё не реализован: результаты не записаны", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
