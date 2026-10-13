"""Что прогон получает с хоста: файл вопросов, набор и каталог результатов."""

import os
from pathlib import Path


def check_mounts(questions: Path, data_dir: Path, out_dir: Path) -> list[str]:
    """Возвращает список проблем; пустой список — всё на месте."""
    problems: list[str] = []
    if not questions.is_file():
        problems.append(f"нет файла вопросов: {questions}")
    elif not os.access(questions, os.R_OK):
        problems.append(f"файл вопросов не читается: {questions}")
    if not data_dir.is_dir():
        problems.append(f"нет каталога набора: {data_dir}")
    if not out_dir.is_dir():
        problems.append(f"нет каталога результатов: {out_dir}")
    elif not os.access(out_dir, os.W_OK):
        problems.append(f"каталог результатов недоступен для записи: {out_dir}")
    return problems
