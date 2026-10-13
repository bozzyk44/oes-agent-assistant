from fastapi.testclient import TestClient

from toir_stub.app import create_app as create_toir


def test_toir_stub_ready() -> None:
    response = TestClient(create_toir()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
