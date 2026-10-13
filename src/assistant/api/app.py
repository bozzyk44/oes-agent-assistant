"""Приложение сервиса ассистента.

Каркас: только проверка готовности. Маршруты хода и действий, конфигурация
и пользователь из заголовка появляются в следующих задачах.
"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Ассистент сотрудника", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
