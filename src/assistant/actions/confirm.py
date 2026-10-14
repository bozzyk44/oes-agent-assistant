"""Подтверждение и отказ: конечный автомат действия поверх `app.actions`.

```
              подтверждение автором              ответ ТОиР записан
 proposed ───────────────────────> confirming ───────────────────> done
    │                                │  ▲   │
    │ отказ автора     ТОиР отверг   │  └───┘ повтор подтверждения:
    ▼                  запрос        ▼        тот же ключ, тот же запрос
 rejected                          failed
```

Подтверждение — три шага: намерение (`proposed → confirming`) фиксируется до вызова ТОиР;
вызов ТОиР с ключом идемпотентности из строки действия; результат записывается отдельной
транзакцией. Падение между шагами оставляет `confirming`, и повторное подтверждение
повторяет вызов с тем же ключом — ТОиР вернёт ту же заявку. «Ровно один раз» складывается
из двух половин: сервис может вызвать ТОиР больше одного раза, но всегда с одним ключом,
а ТОиР по одному ключу создаёт одну заявку.

Строку действия каждый шаг читает с блокировкой: одновременные подтверждение и отказ
выстраиваются в очередь, и второй видит итог первого. Триггер таблицы — вторая проверка:
недопустимый переход отклоняет база, что бы ни запросил код.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from assistant.access import Status, check_repair, load_grants
from assistant.actions.toir import Created, Rejected, ToirClient, Unknown
from assistant.db.session import transaction

_LOCK = text(
    "select user_id, params, status, idempotency_key, request_id, failure_reason"
    " from app.actions where action_id = :action_id for update"
)
_TO_CONFIRMING = text(
    "update app.actions set status = 'confirming'"
    " where action_id = :action_id and status = 'proposed'"
)
_TO_REJECTED = text(
    "update app.actions set status = 'rejected'"
    " where action_id = :action_id and status = 'proposed'"
)
_TO_DONE = text(
    "update app.actions set status = 'done', request_id = :request_id"
    " where action_id = :action_id and status = 'confirming'"
)
_TO_FAILED = text(
    "update app.actions set status = 'failed', failure_reason = :reason"
    " where action_id = :action_id and status = 'confirming'"
)


class NotFound(Exception):
    """Нет такого действия."""


class Forbidden(Exception):
    """Не автор действия или полномочие на участок отозвано."""


class Conflict(Exception):
    """Подтверждение после отказа или отказ после подтверждения."""


class ToirFailed(Exception):
    """ТОиР недоступен или отверг запрос; клиент получает 502."""


@dataclass(frozen=True)
class _Row:
    user_id: str
    params: dict[str, Any]
    status: str
    idempotency_key: str
    request_id: str | None
    failure_reason: str | None


# Запись результата: подставляется тестом, который проверяет падение после ответа ТОиР.
StoreResult = Callable[[Connection, str, str], None]


def store_result(connection: Connection, action_id: str, request_id: str) -> None:
    connection.execute(_TO_DONE, {"action_id": action_id, "request_id": request_id})


@dataclass(frozen=True)
class Actions:
    engine: Engine
    toir: ToirClient
    store: StoreResult = store_result

    def confirm(self, action_id: str, user_id: str) -> str:
        """Подтверждает действие и возвращает `request_id` заявки в ТОиР."""
        with transaction(self.engine, user_id) as connection:
            row = self._own(connection, action_id, user_id)
            if row.status == "done" and row.request_id:
                return row.request_id
            if row.status == "rejected":
                raise Conflict("действие отклонено")
            if row.status == "failed":
                # Повтор без вызова ТОиР: тот же запрос он отвергнет снова.
                raise ToirFailed(row.failure_reason or "ТОиР отверг запрос")
            if row.status == "proposed":
                # Полномочие проверяется при намерении, и только при нём. Повтор из
                # `confirming` доводит уже разрешённое намерение: заявка, возможно, создана
                # прошлой попыткой, и отказ здесь оставил бы действие в `confirming` навсегда,
                # а созданную заявку — незаписанной.
                self._check_right(connection, user_id, row)
                connection.execute(_TO_CONFIRMING, {"action_id": action_id})
            key, params = row.idempotency_key, row.params

        # Намерение зафиксировано. Вызов ТОиР — вне транзакции: он может длиться секунды.
        outcome = self.toir.create(key, params)

        with transaction(self.engine, user_id) as connection:
            if isinstance(outcome, Created):
                self.store(connection, action_id, outcome.request_id)
            elif isinstance(outcome, Rejected):
                connection.execute(_TO_FAILED, {"action_id": action_id, "reason": outcome.reason})
            row = self._own(connection, action_id, user_id)
        if row.status == "done" and row.request_id:
            # В том числе если одновременное подтверждение записало результат раньше.
            return row.request_id
        if isinstance(outcome, Unknown):
            raise ToirFailed(outcome.reason)
        raise ToirFailed(row.failure_reason or "ТОиР отверг запрос")

    def reject(self, action_id: str, user_id: str) -> None:
        with transaction(self.engine, user_id) as connection:
            row = self._own(connection, action_id, user_id)
            if row.status == "rejected":
                return
            if row.status != "proposed":
                raise Conflict("действие уже подтверждено")
            connection.execute(_TO_REJECTED, {"action_id": action_id})

    @staticmethod
    def _own(connection: Connection, action_id: str, user_id: str) -> _Row:
        found = connection.execute(_LOCK, {"action_id": action_id}).first()
        if found is None:
            raise NotFound(action_id)
        row = _Row(
            user_id=found.user_id,
            params=found.params,
            status=found.status,
            idempotency_key=str(found.idempotency_key),
            request_id=found.request_id,
            failure_reason=found.failure_reason,
        )
        if row.user_id != user_id:
            raise Forbidden("действие подтверждает его автор")
        return row

    @staticmethod
    def _check_right(connection: Connection, user_id: str, row: _Row) -> None:
        """Полномочие на участок проверяется заново: его могли отозвать после черновика."""
        grants = load_grants(connection, user_id)
        equipment_id = row.params.get("equipment_id")
        if grants is None or not isinstance(equipment_id, str):
            raise Forbidden("нет полномочия")
        if check_repair(connection, grants, equipment_id).status is not Status.OK:
            raise Forbidden("полномочие на участок оборудования отозвано")
