"""Какие вызовы инструментов из одного ответа модели выполняются.

Модель может запросить несколько инструментов сразу. Если среди них есть подготовка заявки,
выполняется она одна, первая по порядку: после черновика ход завершается без следующего
вызова модели, и в одном ходе — не больше одного действия. На остальные вызовы модель
получает «не выполнен», если ход продолжится: протокол требует ответа на каждый вызов.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from assistant.actions.draft import TOOL as REPAIR_TOOL

NOT_EXECUTED = "не выполнен: в этом ответе запрошена подготовка заявки"


class Named(Protocol):
    @property
    def name(self) -> str: ...


@dataclass(frozen=True)
class CallPlan[C: Named]:
    run: tuple[C, ...]
    skipped: tuple[C, ...]

    @property
    def drafts(self) -> bool:
        """План готовит заявку: при успешном черновике ход на этом заканчивается."""
        return any(call.name == REPAIR_TOOL for call in self.run)


def plan_calls[C: Named](calls: Sequence[C]) -> CallPlan[C]:
    repair = next((call for call in calls if call.name == REPAIR_TOOL), None)
    if repair is None:
        return CallPlan(tuple(calls), ())
    return CallPlan((repair,), tuple(call for call in calls if call is not repair))
