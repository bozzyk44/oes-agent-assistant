"""Клиент ТОиР: один вызов `POST /requests` с ключом идемпотентности и разбор ответа.

Ответ сводится к трём исходам, по которым автомат действия решает, что делать дальше:
заявка есть; ТОиР отверг запрос как неверный — повторять бессмысленно; неизвестно —
ТОиР недоступен, не ответил, ответил ошибкой сервера или отказом в авторизации, и заявка
могла быть создана. Повтор с тем же ключом безопасен: ТОиР по одному ключу создаёт одну
заявку.

Секрет ТОиР живёт только здесь и в транспорте; в ответах и журнале его нет.
"""

import http.client
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
# Коды, которыми ТОиР отвергает сам запрос: повтор того же запроса даст тот же ответ.
_REJECTED = frozenset({400, 409, 422})


@dataclass(frozen=True)
class Reply:
    status: int
    body: Any = None


# Транспорт: метод, путь, заголовки, тело → ответ. Сбой сети — исключение `OSError`.
Transport = Callable[[str, str, Mapping[str, str], Any], Reply]


@dataclass(frozen=True)
class Created:
    request_id: str


@dataclass(frozen=True)
class Rejected:
    reason: str


@dataclass(frozen=True)
class Unknown:
    reason: str


Outcome = Created | Rejected | Unknown


@dataclass(frozen=True)
class ToirClient:
    transport: Transport
    token: str = field(repr=False)

    def create(self, idempotency_key: str, params: Mapping[str, Any]) -> Outcome:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }
        outcome = self._create(headers, params)
        if not isinstance(outcome, Created):
            # Причина без тела запроса и без заголовков: токена в журнале нет.
            log.warning("заявка с ключом %s: %s", idempotency_key, outcome.reason)
        return outcome

    def _create(self, headers: Mapping[str, str], params: Mapping[str, Any]) -> Outcome:
        try:
            reply = self.transport("POST", "/requests", headers, dict(params))
        except (OSError, http.client.HTTPException, ValueError) as error:
            # Запрос мог дойти: заявка, возможно, создана. Повтор с тем же ключом это решит.
            # Неверный адрес в настройках тоже сюда: ответ клиенту — 502, а не 500.
            return Unknown(f"ТОиР недоступен: {type(error).__name__}")
        if 200 <= reply.status < 300:
            request_id = reply.body.get("request_id") if isinstance(reply.body, dict) else None
            if isinstance(request_id, str) and request_id:
                return Created(request_id)
            return Unknown("ТОиР ответил без идентификатора заявки")
        if reply.status in _REJECTED:
            return Rejected(f"ТОиР отверг запрос: {reply.status}")
        return Unknown(f"ТОиР ответил {reply.status}")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Перенаправление не выполняется: иначе заголовок с токеном ушёл бы на другой адрес.
    Ответ 3xx становится обычной ошибкой, исход — «неизвестно».
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_transport(base_url: str, timeout: float = TIMEOUT_SECONDS) -> Transport:
    """Транспорт по HTTP на стандартной библиотеке: ради одного вызова зависимость не нужна."""
    root = base_url.rstrip("/")
    opener = urllib.request.build_opener(_NoRedirects)

    def send(method: str, path: str, headers: Mapping[str, str], body: Any) -> Reply:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(  # noqa: S310 — адрес из настроек, схема http(s)
            root + path, data=data, method=method, headers=dict(headers)
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                return Reply(response.status, _json(response.read()))
        except urllib.error.HTTPError as error:
            return Reply(error.code, _json(error.read()))

    return send


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None
