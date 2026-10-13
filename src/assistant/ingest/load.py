"""Загрузка набора в базу под ролью владельца.

Идемпотентна по хэшу содержимого `DATA_DIR`: совпал с записанным — ничего не меняется,
сессии и действия целы. Отличается — одной транзакцией заменяются индекс документов,
таблицы КХД, пользователи и полномочия и очищаются сессии, ходы, сообщения и действия:
история хранит данные прежнего набора.
"""

from sqlalchemy import Connection, delete, insert, select

from assistant.db import models
from assistant.ingest.chunks import exact_terms, split_document
from assistant.ingest.dataset import Dataset

# Версия алгоритма загрузки: растёт при смене правил разбиения на фрагменты и индексации.
LOADER_VERSION = 1


def load_dataset(connection: Connection, dataset: Dataset) -> bool:
    """Заменяет набор, если он изменился. Возвращает, была ли замена.

    Работает в транзакции вызывающего: при любой ошибке прежний набор остаётся целиком.
    """
    current = connection.scalar(select(models.DatasetVersion.content_hash))
    if current == dataset.content_hash:
        return False

    # Сессии уходят вместе с пользователями; ходы, сообщения и действия — каскадом.
    for model in (
        models.DatasetVersion,
        models.Action,
        models.Session,
        models.User,
        models.Document,
        models.Production,
        models.Downtime,
        models.Equipment,
    ):
        connection.execute(delete(model))

    _insert(
        connection,
        models.User,
        [
            {"user_id": user.user_id, "name": user.name, "position_title": user.position}
            for user in dataset.users
        ],
    )
    _insert(
        connection,
        models.UserGroup,
        [
            {"user_id": user.user_id, "group_name": group}
            for user in dataset.users
            for group in user.groups
        ],
    )
    _insert(
        connection,
        models.KpiGrant,
        [
            {"user_id": user.user_id, "table_name": table, "site": site}
            for user in dataset.users
            for table, sites in user.kpi.items()
            for site in sites
        ],
    )
    _insert(
        connection,
        models.RepairGrant,
        [
            {"user_id": user.user_id, "site": site}
            for user in dataset.users
            for site in user.repair_requests
        ],
    )

    _insert(
        connection,
        models.Production,
        [
            {"date": day, "shift": shift, "site": site, "tonnes": tonnes}
            for day, shift, site, tonnes in dataset.production
        ],
    )
    _insert(
        connection,
        models.Downtime,
        [
            {"equipment_id": unit, "site": site, "start": start, "end": end, "reason": reason}
            for unit, site, start, end, reason in dataset.downtime
        ],
    )
    _insert(
        connection,
        models.Equipment,
        [
            {
                "equipment_id": unit,
                "type": kind,
                "model": model,
                "site": site,
                "motor_hours": hours,
                "last_ppr_motor_hours": last,
            }
            for unit, kind, model, site, hours, last in dataset.equipment
        ],
    )

    _insert(connection, models.Document, [{"doc_id": doc.doc_id} for doc in dataset.documents])
    chunks = []
    for doc in dataset.documents:
        for ordinal, piece in enumerate(split_document(doc.doc_id, doc.body)):
            chunks.append(
                {
                    "chunk_id": piece.chunk_id,
                    "doc_id": doc.doc_id,
                    "ordinal": ordinal,
                    "section_no": piece.section_no,
                    "part_no": piece.part_no,
                    "title": doc.title,
                    "doc_date": doc.date,
                    "status": doc.status,
                    "acl": list(doc.acl),
                    "supersedes": doc.supersedes,
                    "superseded_by": doc.superseded_by,
                    "heading": piece.heading,
                    "body": piece.body,
                    "terms": exact_terms(f"{doc.title}\n{piece.heading}\n{piece.body}"),
                }
            )
    _insert(connection, models.Chunk, chunks)

    connection.execute(
        insert(models.DatasetVersion).values(singleton=True, content_hash=dataset.content_hash)
    )
    return True


def _insert(
    connection: Connection, model: type[models.Base], rows: list[dict[str, object]]
) -> None:
    if rows:
        connection.execute(insert(model), rows)
