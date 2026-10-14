"""Эталонные вопросы и разметка фрагментов.

Формат `questions.jsonl` — из задания. Разметка лежит рядом с файлом вопросов
(`questions.labels.json`); у формулировок `<вопрос>.<номер>` она общая с вопросом.
"""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LABELS_FILE = "questions.labels.json"


@dataclass(frozen=True)
class Question:
    qid: str
    user: str
    text: str
    expect: str
    session: str | None = None
    must_include: tuple[str, ...] = ()
    must_cite: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    action: dict[str, Any] | None = None
    note: str = ""
    # Фрагменты с ответом по разметке генератора: `<документ>#<раздел>`.
    labels: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def base_id(self) -> str:
        """Вопрос без номера формулировки: `q01.2` → `q01`."""
        return self.qid.split(".", 1)[0]


class QuestionError(ValueError):
    pass


def _strings(record: dict[str, Any], name: str, line: int) -> tuple[str, ...]:
    value = record.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise QuestionError(f"строка {line}: {name} — ожидался список строк")
    return tuple(value)


def parse(record: dict[str, Any], line: int, labels: dict[str, list[str]]) -> Question:
    for name in ("id", "user", "question", "expect"):
        if not isinstance(record.get(name), str) or not record[name]:
            raise QuestionError(f"строка {line}: нет поля {name}")
    action = record.get("action")
    if action is not None and not isinstance(action, dict):
        raise QuestionError(f"строка {line}: action — ожидался объект")
    session = record.get("session")
    qid = record["id"]
    return Question(
        qid=qid,
        user=record["user"],
        text=record["question"],
        expect=record["expect"],
        session=session if isinstance(session, str) and session else None,
        must_include=_strings(record, "must_include", line),
        must_cite=_strings(record, "must_cite", line),
        forbidden=_strings(record, "forbidden", line),
        action=action,
        note=str(record.get("note", "")),
        labels=tuple(labels.get(qid.split(".", 1)[0], [])),
        raw=record,
    )


def load_labels(path: Path) -> dict[str, list[str]]:
    """Разметка генератора; нет файла — пусто: на чужом наборе метрики фрагментов не считаются."""
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        qid: [str(item["chunk_prefix"]) for item in items if "chunk_prefix" in item]
        for qid, items in raw.items()
        if isinstance(items, list)
    }


def load(path: Path, labels: dict[str, list[str]] | None = None) -> list[Question]:
    labels = labels if labels is not None else load_labels(path.parent / LABELS_FILE)
    questions = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            raise QuestionError(f"строка {number}: не JSON") from None
        if not isinstance(record, dict):
            raise QuestionError(f"строка {number}: ожидался объект")
        questions.append(parse(record, number, labels))
    ids = [question.qid for question in questions]
    if len(set(ids)) != len(ids):
        raise QuestionError("идентификаторы вопросов повторяются")
    return questions


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sessions(questions: list[Question]) -> Iterator[list[Question]]:
    """Группы вопросов одной сессии в порядке файла; вопрос без `session` — своя группа."""
    groups: dict[str, list[Question]] = {}
    order: list[str] = []
    for question in questions:
        key = f"s:{question.session}" if question.session else f"q:{question.qid}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(question)
    for key in order:
        yield groups[key]
