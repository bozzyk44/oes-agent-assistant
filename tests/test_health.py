from fastapi.testclient import TestClient

from assistant.api.app import create_app as create_assistant
from toir_stub.app import create_app as create_toir


def test_assistant_ready() -> None:
    response = TestClient(create_assistant()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_toir_stub_ready() -> None:
    response = TestClient(create_toir()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_no_schema_endpoints() -> None:
    # Схема API наружу не публикуется: контракт задан заданием, а не автодокументацией.
    client = TestClient(create_assistant())
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
