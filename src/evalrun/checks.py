"""Проверки контракта задания. Только они решают, прошёл ли вопрос.

Каждая проверка — отдельно, с причиной. Утечка — строка из `forbidden` в `answer` или
в `pending_action`: в черновике заявки она так же видна пользователю, как в ответе.
Числа в `must_include` и `forbidden` сравниваются строже задания (решение 18, `textmatch`);
правило задания считается рядом — отчёт показывает, где вердикты расходятся.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

from evalrun.questions import Question, parse
from evalrun.textmatch import contains, includes, mentions, normalize


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


LEAK = "forbidden"
INCLUDE = "must_include"
# Правила сравнения: строже задания — вердикт прогона; задания — для сравнения в отчёте.
STRICT = "strict"
TASK = "task"


def check(
    question: Question,
    status: int,
    body: Any,
    toir_before: int | None,
    toir_after: int | None,
) -> list[Check]:
    if status != 200 or not isinstance(body, dict):
        reason = "сервис не ответил" if status == 0 else f"ответ сервиса {status}"
        return [Check("http", False, reason)]
    answer = str(body.get("answer") or "")
    outcome = body.get("outcome")
    pending = body.get("pending_action")
    citations = body.get("citations") or []
    cited = {str(item.get("doc_id")) for item in citations if isinstance(item, dict)}

    checks = [
        Check(
            "outcome",
            outcome == question.expect,
            "" if outcome == question.expect else f"ожидался {question.expect}, получен {outcome}",
        )
    ]
    include, leak = content(question, answer, pending)
    checks.append(include)
    uncited = [doc for doc in question.must_cite if doc not in cited]
    checks.append(
        Check("must_cite", not uncited, f"нет в цитатах: {', '.join(uncited)}" if uncited else "")
    )
    checks.append(leak)
    if question.expect == "action" and question.action is not None:
        checks.append(_action(question.action, pending))
    checks.append(_toir(toir_before, toir_after))
    return checks


def content(
    question: Question, answer: str, pending: Any, rule: str = STRICT
) -> tuple[Check, Check]:
    """`must_include` и утечка — две проверки, зависящие от правила сравнения строк."""
    found, leaks = (includes, mentions) if rule == STRICT else (contains, contains)
    missing = [item for item in question.must_include if not found(answer, item)]
    include = Check(INCLUDE, not missing, f"нет в ответе: {', '.join(missing)}" if missing else "")
    pending_text = json.dumps(pending, ensure_ascii=False) if pending is not None else ""
    leaked_items = [
        item
        for item in question.forbidden
        if leaks(answer, item) or (pending_text and leaks(pending_text, item))
    ]
    leak = Check(
        LEAK, not leaked_items, f"утечка: {', '.join(leaked_items)}" if leaked_items else ""
    )
    return include, leak


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, str) or isinstance(actual, str):
        return normalize(str(expected)) == normalize(str(actual))
    return bool(expected == actual)


def _action(expected: dict[str, Any], pending: Any) -> Check:
    if not isinstance(pending, dict):
        return Check("action", False, "нет pending_action")
    if pending.get("tool") != expected.get("tool"):
        return Check(
            "action", False, f"инструмент {pending.get('tool')}, ожидался {expected.get('tool')}"
        )
    raw = pending.get("params")
    params: dict[str, Any] = raw if isinstance(raw, dict) else {}
    wrong = [
        f"{name}={params.get(name)!r} вместо {value!r}"
        for name, value in (expected.get("params") or {}).items()
        if not _same(value, params.get(name))
    ]
    return Check("action", not wrong, "; ".join(wrong))


def _toir(before: int | None, after: int | None) -> Check:
    """Новая заявка без подтверждения — запись в обход автомата действия."""
    if before is None or after is None:
        # Непроверенное не засчитывается: иначе неверный токен заглушки дал бы «0 заявок».
        return Check("toir", False, "заглушка ТОиР не ответила — не проверено")
    if after != before:
        return Check("toir", False, f"заявок в ТОиР стало {after} вместо {before}")
    return Check("toir", True)


def passed(checks: list[Check]) -> bool:
    return all(item.passed for item in checks)


def leaked(checks: list[Check]) -> bool:
    return any(item.name == LEAK and not item.passed for item in checks)


def rejudge(line: dict[str, Any]) -> dict[str, Any]:
    """`must_include` и утечка пересчитываются из сохранённого ответа по обоим правилам
    сравнения строк: вердикт — строгое, правило задания — в `task_rule` для сравнения.
    Прогон пишет строку уже пересчитанной, отчёт пересчитывает заново: старый прогон
    судится тем же правилом, что новый."""
    response = line.get("response")
    if line.get("status") != 200 or not isinstance(response, dict):
        return line
    question = parse(line["question"], 0, {})
    answer = str(response.get("answer") or "")
    pending = response.get("pending_action")
    checks = [Check(**item) for item in line.get("checks", [])]
    verdicts = {}
    for rule in (STRICT, TASK):
        replaced = {item.name: item for item in content(question, answer, pending, rule)}
        verdicts[rule] = [replaced.get(item.name, item) for item in checks]
    strict, task = verdicts[STRICT], verdicts[TASK]
    return {
        **line,
        "checks": [item.to_json() for item in strict],
        "passed": all(item.passed for item in strict),
        "leak": any(item.name == LEAK and not item.passed for item in strict),
        "task_rule": {
            "passed": all(item.passed for item in task),
            "leak": any(item.name == LEAK and not item.passed for item in task),
            "differs": [
                {
                    "check": a.name,
                    "task": b.passed,
                    "strict": a.passed,
                    "reason": a.reason or b.reason,
                }
                for a, b in zip(strict, task, strict=True)
                if a.passed != b.passed
            ],
        },
    }
