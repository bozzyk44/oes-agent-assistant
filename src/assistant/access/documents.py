"""Документы: поиск и выдача по идентификатору.

Условие по группам доступа стоит в самом запросе: ранжируются только разрешённые фрагменты,
и закрытый документ не может вытеснить доступный из выдачи или попасть в неё после
отбора лучших. Построчная защита `kb.chunks` проверяет то же условие второй раз.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, text

from assistant.access.grants import Grants
from assistant.access.status import Status
from assistant.ingest.chunks import exact_terms

# Выдача поиска: фрагмент — не длиннее четверти лимита результата инструмента.
SEARCH_LIMIT = 4

_COLUMNS = (
    "c.chunk_id, c.doc_id, c.title, c.heading, c.doc_date, c.status, c.superseded_by,"
    " c.body, c.supersedes"
)

# Слова запроса — через «или»: требование всех слов сразу давало бы пустую выдачу на обычный
# вопрос. Лексемы в выводе `plainto_tsquery` закавычены и пробелов не содержат, поэтому
# ` & ` в нём — только знак между ними. Идентификаторы и числа ищутся точным совпадением.
_SEARCH = text(
    f"""
    with q as (
        select nullif(
            replace(plainto_tsquery('russian', :query)::text, ' & ', ' | '), ''
        )::tsquery as q
    )
    select {_COLUMNS}
    from kb.chunks c, q
    where c.acl && cast(:groups as text[])
      and (:all_editions or c.status = 'active')
      and ((q.q is not null and c.tsv @@ q.q) or c.terms && cast(:terms as text[]))
    order by
        coalesce(ts_rank(c.tsv, q.q), 0)
            + cardinality(array(
                select unnest(c.terms) intersect select unnest(cast(:terms as text[]))
            )) desc,
        c.chunk_id
    limit :limit
    """  # noqa: S608 — в тексте только постоянный список колонок
)
_CLOSED = text("select kb.best_match_is_closed(:query)")
_DOCUMENT = text(
    f"""
    select {_COLUMNS} from kb.chunks c
    where c.doc_id = :doc_id and c.acl && cast(:groups as text[])
    order by c.ordinal
    """  # noqa: S608
)
_EXISTS = text("select kb.document_exists(:doc_id)")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    heading: str
    doc_date: date
    status: str
    superseded_by: str | None
    body: str
    supersedes: str | None = None


@dataclass(frozen=True)
class SearchResult:
    status: Status
    chunks: tuple[Chunk, ...] = ()
    # Лучшее совпадение среди всех документов — закрытый для пользователя фрагмент.
    # Один бит от функции базы, без содержимого; от пустоты доступной выдачи не зависит.
    best_match_closed: bool = False


@dataclass(frozen=True)
class DocumentResult:
    status: Status
    chunks: tuple[Chunk, ...] = ()


def _normalize(query: str) -> str:
    # Как в индексе: регистр и «ё» приведены.
    return query.lower().replace("ё", "е")


def search(
    connection: Connection,
    grants: Grants,
    query: str,
    *,
    include_superseded: bool = False,
    limit: int = SEARCH_LIMIT,
) -> SearchResult:
    """Лучшие разрешённые фрагменты по запросу. Утратившие силу — только по явному признаку."""
    normalized = _normalize(query)
    rows = connection.execute(
        _SEARCH,
        {
            "query": normalized,
            "terms": exact_terms(normalized),
            "groups": grants.doc_groups,
            "all_editions": include_superseded,
            "limit": limit,
        },
    )
    chunks = tuple(Chunk(*row) for row in rows)
    closed = bool(connection.scalar(_CLOSED, {"query": query}))
    return SearchResult(Status.OK if chunks else Status.EMPTY, chunks, closed)


def get_document(connection: Connection, grants: Grants, ref: str) -> DocumentResult:
    """Документ целиком, раздел или его часть по ссылке `<документ>`, `<документ>#<раздел>`
    или `<документ>#<раздел>.<часть>`.

    Нет доступа — `denied` без названия, даты и текста. Есть ли такой документ вообще,
    сообщает функция базы, отвечающая одним битом.
    """
    ref = ref.strip()
    doc_id = ref.split("#", 1)[0]
    if not doc_id:
        return DocumentResult(Status.NOT_FOUND)
    chunks = tuple(
        Chunk(*row)
        for row in connection.execute(_DOCUMENT, {"doc_id": doc_id, "groups": grants.doc_groups})
    )
    if not chunks:
        exists = bool(connection.scalar(_EXISTS, {"doc_id": doc_id}))
        return DocumentResult(Status.DENIED if exists else Status.NOT_FOUND)
    if "#" in ref:
        # Ссылка на раздел даёт и все его части: длинный раздел хранится как `#3.1`, `#3.2`.
        chunks = tuple(
            chunk
            for chunk in chunks
            if chunk.chunk_id == ref or chunk.chunk_id.startswith(f"{ref}.")
        )
        if not chunks:
            return DocumentResult(Status.NOT_FOUND)
    return DocumentResult(Status.OK, chunks)
