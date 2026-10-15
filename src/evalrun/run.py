"""Прогон: каждая попытка проигрывает весь набор в собственных сессиях.

Идентификатор сессии — `session` вопроса (или его `id`), запуск и номер попытки: попытки
не загрязняют историю друг друга, повторный прогон не попадает в сессии прошлого.
Вопросы одной сессии идут строго по порядку файла. На 409 запрос повторяется после паузы.
Заявки в ТОиР считаются до и после каждого вопроса, поэтому вопросы идут по одному.
"""

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalrun.api import Service, Toir
from evalrun.checks import check, leaked, passed, rejudge
from evalrun.diagnostics import diagnose
from evalrun.questions import Question, sessions

BUSY_RETRIES = 5
BUSY_PAUSE = 2.0


@dataclass
class Prices:
    """$ за 1 млн токенов; не задана — `None`, цена хода тогда не считается."""

    input: float | None = None
    cached: float | None = None
    output: float | None = None


@dataclass
class Plan:
    run_id: str
    attempts: int
    questions_file: str
    questions_hash: str
    prices: Prices = field(default_factory=Prices)


def session_id(question: Question, run_id: str, attempt: int) -> str:
    return f"{question.session or question.qid}-{run_id}-a{attempt}"


def config_line(plan: Plan, info: dict[str, Any], started: datetime) -> dict[str, Any]:
    return {
        "type": "config",
        "run_id": plan.run_id,
        "started_at": started.isoformat(),
        "model": info.get("model"),
        "model_base_url": info.get("model_base_url"),
        "prefix_hash": info.get("prefix_hash"),
        "commit": info.get("commit"),
        "dataset_hash": info.get("dataset_hash"),
        "questions_file": plan.questions_file,
        "questions_hash": plan.questions_hash,
        "sampling": info.get("sampling"),
        "attempts": plan.attempts,
        "prices": {
            "input": plan.prices.input,
            "cached": plan.prices.cached,
            "output": plan.prices.output,
        },
    }


@dataclass
class Runner:
    service: Service
    toir: Toir
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    progress: Callable[[int, str, bool], None] = lambda *_: None

    def preflight(self) -> str | None:
        """До первого вызова модели: сервис отвечает, а заглушка ТОиР отдаёт заявки.
        Без счёта заявок проверка «новой заявки нет» невозможна, а без сервиса прогон
        из одних провалов затёр бы прошлый итог."""
        if not self.service.info().get("prefix_hash"):
            return "сервис не отвечает на GET /info — он запущен? (make run)"
        if self.toir.count() is None:
            return "заглушка ТОиР не отвечает на GET /requests — проверьте TOIR_URL и TOIR_TOKEN"
        return None

    def ask(self, question: Question, session: str) -> tuple[int, Any, int]:
        retries = 0
        while True:
            status, body = self.service.turn(question.user, session, question.text)
            if status != 409 or retries >= BUSY_RETRIES:
                return status, body, retries
            retries += 1
            self.sleep(BUSY_PAUSE * retries)

    def attempt_line(self, question: Question, run_id: str, attempt: int) -> dict[str, Any]:
        session = session_id(question, run_id, attempt)
        before = self.toir.count()
        started = self.monotonic()
        status, body, retries = self.ask(question, session)
        latency_ms = round((self.monotonic() - started) * 1000)
        after = self.toir.count()
        checks = check(question, status, body, before, after)
        record = None
        if isinstance(body, dict) and body.get("turn_id"):
            full = self.service.record(question.user, str(body["turn_id"]))
            record = full.get("record") if full else None
        answer = str(body.get("answer") or "") if isinstance(body, dict) else ""
        return rejudge(
            {
                "type": "attempt",
                "run_id": run_id,
                "attempt": attempt,
                "question": question.raw,
                "labels": list(question.labels),
                "session_id": session,
                "status": status,
                "response": body,
                "busy_retries": retries,
                "latency_ms": latency_ms,
                "toir_before": before,
                "toir_after": after,
                "checks": [item.to_json() for item in checks],
                "passed": passed(checks),
                "leak": leaked(checks),
                "diagnostics": diagnose(record, question.text, answer),
                "turn_record": record,
            }
        )

    def run(
        self, questions: list[Question], plan: Plan, out: Path, now: Callable[[], datetime]
    ) -> list[dict[str, Any]]:
        lines = [config_line(plan, self.service.info(), now())]
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(lines[0], ensure_ascii=False) + "\n")
            for attempt in range(1, plan.attempts + 1):
                for group in sessions(questions):
                    for question in group:
                        line = self.attempt_line(question, plan.run_id, attempt)
                        lines.append(line)
                        stream.write(json.dumps(line, ensure_ascii=False) + "\n")
                        stream.flush()
                        self.progress(attempt, question.qid, bool(line["passed"]))
        return lines


def new_run_id(now: datetime | None = None) -> str:
    """Время запуска и случайный хвост: два запуска в одну секунду не делят сессии."""
    moment = now or datetime.now(UTC)
    return f"{moment.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
