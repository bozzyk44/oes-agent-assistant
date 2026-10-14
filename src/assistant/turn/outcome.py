"""Итоговый `outcome` хода: одна таблица на оба пути разбора ответа модели.

Модель предлагает исход, код проверяет его по статусам инструментов. Ход сводится к двум
признакам — сигналу отказа и использованным данным, — и таблица применяется сверху вниз.
«Исход не назван» — текстовый путь разбора или объект без поля исхода.
`budget_exceeded` и `error` ставит не эта таблица, а рамка хода.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from assistant.access.status import Status

SEARCH = "search_documents"
DOCUMENT = "get_document"
KPI = "query_kpi"
REPAIR = "create_repair_request"
# Инструменты прямого запроса: их ответ говорит о самих данных, а не о похожих фрагментах.
DIRECT_TOOLS = frozenset({DOCUMENT, KPI})
# Исходы, которые модели позволено предложить.
PROPOSABLE = frozenset({"answer", "refusal", "no_data"})


@dataclass(frozen=True)
class ToolStatus:
    """Что код знает о вызове инструмента; содержимого здесь нет."""

    tool: str
    status: Status
    # Поиск: лучшее совпадение закрыто.
    best_match_closed: bool = False
    # Запрос к КХД: отказано в части явно названных участков или оборудования.
    partially_denied: bool = False


@dataclass(frozen=True)
class Verdict:
    outcome: str
    # Номер строки таблицы — в запись хода, чтобы замена исхода кодом была видна.
    row: int


def resolve_outcome(
    calls: Iterable[ToolStatus],
    *,
    proposed: str | None,
    cited: bool,
    action_drafted: bool,
) -> Verdict:
    """Исход хода.

    `proposed` — исход, предложенный моделью; всё, кроме `answer`, `refusal` и `no_data`,
    считается неназванным. `cited` — в ответе есть принятая кодом цитата.
    """
    calls = tuple(calls)
    if proposed not in PROPOSABLE:
        proposed = None
    refusal_signal = any(
        call.status is Status.DENIED or call.best_match_closed or call.partially_denied
        for call in calls
    )
    used_data = cited or any(call.tool == KPI and call.status is Status.OK for call in calls)

    if action_drafted:
        return Verdict("action", 1)
    if any(call.tool == REPAIR and call.status is Status.DENIED for call in calls):
        return Verdict("refusal", 2)
    if refusal_signal and not used_data:
        return Verdict("refusal", 3)
    if refusal_signal:
        return Verdict("refusal" if proposed in ("refusal", "no_data") else "answer", 4)
    if proposed == "refusal":
        # Данные получены или законно отсутствуют: принятая цитата либо прямой запрос —
        # КХД или документ по идентификатору — с ответом `ok`, `empty` или `not_found`.
        # Поиск без цитаты не в счёт: через «или» он почти всегда что-то возвращает,
        # и обоснованный отказ модели иначе становился бы `no_data`.
        # Пустой поиск не в счёт: признак закрытого совпадения не видит слов с цифрами
        # и одиночных слов, и вопрос о закрытом документе по идентификатору дал бы пустую
        # выдачу без сигнала отказа — обоснованный отказ модели стал бы `no_data`.
        reached = cited or any(
            call.tool in DIRECT_TOOLS and call.status in (Status.OK, Status.EMPTY, Status.NOT_FOUND)
            for call in calls
        )
        return Verdict("no_data" if reached else "refusal", 5)
    if proposed == "no_data":
        return Verdict("no_data", 6)
    if proposed == "answer":
        return Verdict("answer", 7)
    found = used_data or any(call.status is Status.OK for call in calls)
    return Verdict("answer" if found else "no_data", 8)
