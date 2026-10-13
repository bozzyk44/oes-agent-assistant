# Все цели выполняются в контейнерах: на машине нужны только Docker и make.

PROJECT ?= oes-assistant
COMPOSE := docker compose -p $(PROJECT)
COMPOSE_TEST := docker compose -p $(PROJECT)-test -f compose.test.yaml
# Файлы, которые контейнер пишет в каталоги хоста, принадлежат тому, кто запустил make.
HOST_USER := $(shell id -u):$(shell id -g)

# Свой файл вопросов: make eval QUESTIONS=<путь>. По умолчанию — $DATA_DIR/questions.jsonl,
# это значение подставляет compose.
ifdef QUESTIONS
EVAL_ENV := QUESTIONS_DIR="$(abspath $(dir $(QUESTIONS)))" QUESTIONS_FILE="$(notdir $(QUESTIONS))"
endif

.PHONY: help env data run down test eval smoke ci lock

help:
	@echo "make data   — сгенерировать набор в data/"
	@echo "make run    — создать .env при необходимости и поднять сервис, заглушку ТОиР и PostgreSQL"
	@echo "make down   — остановить; том базы остаётся"
	@echo "make test   — линтер, типы и тесты, без сети и модели"
	@echo "make eval   — прогон эталонных вопросов по запущенному сервису; QUESTIONS=<путь>"
	@echo "make smoke  — сценарий сборки: поднять всё с нуля и проверить связку"
	@echo "make ci     — test и smoke на чистой копии текущего коммита; REPORT=1 — отметить коммит в GitHub"
	@echo "make lock   — пересобрать uv.lock после правки зависимостей"

env:
	@sh scripts/make-env.sh

data: env
	$(COMPOSE) --profile tools run --rm --build --user $(HOST_USER) datagen

run: env
	$(COMPOSE) up --build

down:
	$(COMPOSE) down --remove-orphans

test:
	$(COMPOSE_TEST) build tests
	@$(COMPOSE_TEST) run --rm tests; status=$$?; \
	$(COMPOSE_TEST) down --volumes --remove-orphans >/dev/null 2>&1; \
	exit $$status

eval: env
	$(EVAL_ENV) $(COMPOSE) --profile tools run --rm --build --user $(HOST_USER) evalrun

smoke: env
	@COMPOSE='$(COMPOSE)' sh scripts/smoke.sh

ci:
	@REPORT='$(REPORT)' sh scripts/ci-local.sh

lock:
	docker build --quiet --target uv --tag oes-agent-assistant:uv .
	docker run --rm --user $(HOST_USER) --volume "$(CURDIR)":/app oes-agent-assistant:uv uv lock
