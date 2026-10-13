"""Заглушка ТОиР — отдельное приложение и отдельный процесс.

Каркас: только проверка готовности. Приём и выдача заявок — отдельной задачей.
"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Заглушка ТОиР", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
