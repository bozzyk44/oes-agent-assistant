# Один образ на все процессы: сервис, три шага перед ним, заглушка ТОиР, генератор, прогон.
# Стадии: uv — интерпретатор и uv; runtime — то, что запускается; dev — плюс линтер, типы, тесты.

FROM ghcr.io/astral-sh/uv:0.13.0 AS uv-bin

FROM python:3.14-slim AS uv
COPY --from=uv-bin /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache
WORKDIR /app

FROM uv AS deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && rm -rf /tmp/uv-cache

FROM deps AS deps-dev
RUN uv sync --frozen && rm -rf /tmp/uv-cache

FROM python:3.14-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=deps /opt/venv /opt/venv
COPY src ./src
USER app

FROM runtime AS dev
COPY --from=deps-dev /opt/venv /opt/venv
COPY pyproject.toml ./
COPY tests ./tests
