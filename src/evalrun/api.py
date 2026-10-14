"""HTTP к сервису и к заглушке ТОиР. Сервис для прогона — чёрный ящик: только HTTP.

Транспорт подставляется: в контейнере — стандартная библиотека, в тестах — приложения
в процессе тестов. Зависимостей сверх стандартной библиотеки прогону не нужно.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Метод, адрес, заголовки, тело → код ответа и разобранное тело (None — не JSON).
Transport = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]


def urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"адрес не http: {url}")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        for name, value in headers.items():
            request.add_header(name, value)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, _json(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _json(error.read())

    return send


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


@dataclass
class Service:
    url: str
    transport: Transport

    def turn(self, user: str, session: str, message: str) -> tuple[int, Any]:
        return self.transport(
            "POST",
            f"{self.url}/turn",
            {"X-User-ID": user},
            {"session_id": session, "message": message},
        )

    def record(self, user: str, turn_id: str) -> dict[str, Any] | None:
        status, body = self.transport(
            "GET", f"{self.url}/turns/{turn_id}", {"X-User-ID": user}, None
        )
        return body if status == 200 and isinstance(body, dict) else None

    def info(self) -> dict[str, Any]:
        status, body = self.transport("GET", f"{self.url}/info", {}, None)
        return body if status == 200 and isinstance(body, dict) else {}


@dataclass
class Toir:
    """Заглушка ТОиР: адрес и токен прогон берёт из своего окружения, сервису их не передаёт."""

    url: str
    token: str
    transport: Transport

    def count(self) -> int | None:
        status, body = self.transport(
            "GET", f"{self.url}/requests", {"Authorization": f"Bearer {self.token}"}, None
        )
        return len(body) if status == 200 and isinstance(body, list) else None
