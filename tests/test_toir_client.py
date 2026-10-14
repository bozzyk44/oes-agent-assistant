"""Клиент ТОиР: разбор ответов и сбоев транспорта на три исхода."""

from collections.abc import Mapping
from typing import Any

import pytest

from assistant.actions.toir import Created, Rejected, Reply, ToirClient, Unknown, http_transport

TOKEN = "test-toir-client-token-marker-31aa"
PARAMS = {"equipment_id": "XY-7", "description": "течь", "priority": 2}


def _client(reply: Reply | Exception) -> ToirClient:
    def send(method: str, path: str, headers: Mapping[str, str], body: Any) -> Reply:
        assert (method, path) == ("POST", "/requests")
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["Idempotency-Key"] == "k-1"
        assert body == PARAMS
        if isinstance(reply, Exception):
            raise reply
        return reply

    return ToirClient(send, TOKEN)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (Reply(201, {"request_id": "REQ-1"}), Created("REQ-1")),
        (Reply(200, {"request_id": "REQ-1"}), Created("REQ-1")),
        (Reply(400), Rejected),
        (Reply(409), Rejected),
        (Reply(422), Rejected),
        (Reply(401), Unknown),
        (Reply(403), Unknown),
        (Reply(500), Unknown),
        (Reply(503), Unknown),
        (Reply(201, None), Unknown),
        (Reply(201, {"request_id": ""}), Unknown),
        (ConnectionRefusedError(), Unknown),
        (TimeoutError(), Unknown),
        (ValueError("unknown url type"), Unknown),
    ],
)
def test_toir_outcomes(reply: Reply | Exception, expected: Any) -> None:
    outcome = _client(reply).create("k-1", PARAMS)
    if isinstance(expected, type):
        assert isinstance(outcome, expected)
    else:
        assert outcome == expected


def test_token_is_not_in_repr_or_reasons() -> None:
    client = _client(Reply(503))
    assert TOKEN not in repr(client)
    outcome = client.create("k-1", PARAMS)
    assert isinstance(outcome, Unknown) and TOKEN not in outcome.reason


@pytest.mark.parametrize("url", ["http://127.0.0.1:1", "toir:8001", "http://"])
def test_http_transport_failures_are_unknown(url: str) -> None:
    # Отказ соединения на своей машине и неверный адрес в настройках — 502, а не 500.
    outcome = ToirClient(http_transport(url, timeout=2), TOKEN).create("k-1", PARAMS)
    assert isinstance(outcome, Unknown)
