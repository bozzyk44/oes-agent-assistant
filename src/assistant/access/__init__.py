"""Права: единственный выход к данным набора.

Инструменты и цикл хода получают документы, строки КХД и проверку права на заявку только
отсюда. Каждая функция принимает транзакцию, уже привязанную к пользователю, и его
полномочия на ход: условие по правам ставится в сам запрос, а построчная защита базы
проверяет то же самое второй раз.
"""

from assistant.access.documents import (
    Chunk,
    DocumentResult,
    SearchResult,
    get_document,
    search,
)
from assistant.access.grants import Grants, load_grants
from assistant.access.kpi import Filter, KpiQuery, KpiResult, query_rows
from assistant.access.repair import RepairCheck, check_repair
from assistant.access.status import Status

__all__ = [
    "Chunk",
    "DocumentResult",
    "Filter",
    "Grants",
    "KpiQuery",
    "KpiResult",
    "RepairCheck",
    "SearchResult",
    "Status",
    "check_repair",
    "get_document",
    "load_grants",
    "query_rows",
    "search",
]
