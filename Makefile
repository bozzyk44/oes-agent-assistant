# Цели задания — data, run, test, eval — выполняются в контейнерах и требуют только Docker
# и make. Цели smoke и ci — сценарии оболочки на хосте вокруг тех же контейнеров: ci нужны
# ещё git и tar, а REPORT=1 — gh.

COMPOSE_PROJECT ?= oes-assistant
COMPOSE := docker compose -p $(COMPOSE_PROJECT)
COMPOSE_TEST := docker compose -p $(COMPOSE_PROJECT)-test -f compose.test.yaml
# Сценарий сборки удаляет том базы, поэтому работает в своём проекте, а не в том, что поднят make run.
COMPOSE_SMOKE := docker compose -p $(COMPOSE_PROJECT)-smoke
# Файлы, которые контейнер пишет в каталоги хоста, принадлежат тому, кто запустил make.
HOST_USER := $(shell id -u):$(shell id -g)
# Коммит для GET /info; вне рабочей копии git — пусто.
export GIT_COMMIT ?= $(shell git describe --always --dirty 2>/dev/null)

.PHONY: help env data data-holdout run migrate migrate-check down reset test eval eval-report eval-compare judge-set judge-check judge-report judge mutants smoke ci lock stand stand-deploy stand-destroy stand-cloud-init observe

help:
	@echo "make data   — сгенерировать набор в data/"
	@echo "make data-holdout — набор для итоговой проверки с другим зерном в holdout/"
	@echo "make run    — создать .env при необходимости и поднять сервис, заглушку ТОиР и PostgreSQL"
	@echo "make observe — то же, что run, плюс наблюдаемость: трассы, метрики и журнал в Grafana на 127.0.0.1:3000"
	@echo "make down   — остановить; том базы остаётся"
	@echo "make reset  — остановить и удалить том базы"
	@echo "make test   — линтер, типы и тесты, без сети и модели"
	@echo "make eval   — прогон эталонных вопросов по запущенному сервису; QUESTIONS=<путь>, EVAL_ATTEMPTS=<число>"
	@echo "make eval-report — пересобрать eval/report.md из eval/results.jsonl без сервиса"
	@echo "make eval-compare A=<прогон> B=<прогон> — сравнить два прогона попарно по вопросам"
	@echo "make judge-set — набор для проверки LLM-судьи из сохранённых прогонов, без модели"
	@echo "make judge-check — проверка судьи на наборе (платно); make judge RESULTS=… NAME=… — судья по прогону (платно)"
	@echo "make mutants — прогон против сборок с отключённой защитой; MUTANT_MODE=model — с настоящей моделью (платно)"
	@echo "make migrate — применить миграции к базе текущего запуска; повтор ничего не меняет"
	@echo "make migrate-check — проверка миграций: пустая база, повтор, откат и снова, сверка моделей"
	@echo "make smoke  — сценарий сборки: поднять всё с нуля и проверить связку"
	@echo "make ci     — migrate-check, test и smoke на чистой копии текущего коммита; REPORT=1 — отметить коммит в GitHub"
	@echo "make lock   — пересобрать uv.lock после правки зависимостей"
	@echo "make stand  — создать сервер в облаке (платно, спросит подтверждение) и развернуть на нём сервис"
	@echo "make stand-deploy — выкатить текущий коммит на созданный стенд"
	@echo "make stand-destroy — удалить стенд (спросит подтверждение)"
	@echo "make stand-cloud-init — cloud-init стенда; выкатка на сервер из панели: STAND_HOST=<логин>@<адрес> make stand-deploy"

env:
	@sh scripts/make-env.sh

data: env
	$(COMPOSE) --profile tools run --rm --build --user $(HOST_USER) datagen

# Набор для итоговой проверки: промпт настраивается на data/, а проверяется ещё и на наборе
# с другим зерном, на котором его не подгоняли. В репозиторий он не кладётся: его
# воспроизводит та же команда.
# Зерно — HOLDOUT_SEED из src/datagen/world.py; переменная make его переопределяет.
data-holdout: env
	mkdir -p holdout
	$(COMPOSE) --profile tools run --rm --build --user $(HOST_USER) \
		-v "$(CURDIR)/holdout:/app/holdout" datagen \
		python -m datagen --holdout --out holdout $(if $(HOLDOUT_SEED),--seed $(HOLDOUT_SEED))

run: env
	$(COMPOSE) up --build

# Сервис с наблюдаемостью: контейнер grafana/otel-lgtm и адрес приёмника для сервиса.
# Grafana — http://127.0.0.1:3000. Заданные в оболочке переменные сильнее .env.
observe: env
	COMPOSE_PROFILES=observability OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318 $(COMPOSE) up --build

# Профили указаны, чтобы контейнеры наблюдаемости и прокси тоже останавливались.
down:
	$(COMPOSE) --profile observability --profile proxy down --remove-orphans

reset:
	$(COMPOSE) --profile observability --profile proxy down --volumes --remove-orphans

# Миграции — отдельный шаг: тот же сервис compose, что стартует в `make run` между ролями
# и загрузкой набора. PostgreSQL и шаг ролей поднимаются как зависимости.
migrate: env
	$(COMPOSE) run --rm --build migrate

# Проверка миграций отдельным шагом CI: применение на пустую базу, повтор, откат каждой
# миграции её собственным откатом и снова, сверка моделей со схемой. Эти же тесты входят
# и в `make test`.
migrate-check:
	$(COMPOSE_TEST) build tests
	@$(COMPOSE_TEST) run --rm tests pytest -p no:cacheprovider -m migrations; status=$$?; \
	$(COMPOSE_TEST) down --volumes --remove-orphans >/dev/null 2>&1; \
	exit $$status

test:
	$(COMPOSE_TEST) build tests
	@$(COMPOSE_TEST) run --rm tests; status=$$?; \
	$(COMPOSE_TEST) down --volumes --remove-orphans >/dev/null 2>&1; \
	exit $$status

# Свой файл вопросов: make eval QUESTIONS=<путь>. Каталог и имя вычисляет оболочка, а не
# функции make: те делят путь по пробелам. Без QUESTIONS compose берёт $DATA_DIR/questions.jsonl.
# EVAL_ARGS заменяет команду контейнера: так сценарий сборки проверяет монтирования.
eval: env
	@if [ -n "$(QUESTIONS)" ]; then \
		QUESTIONS_DIR=$$(cd "$$(dirname "$(QUESTIONS)")" && pwd) || exit 1; \
		QUESTIONS_FILE=$$(basename "$(QUESTIONS)"); \
		export QUESTIONS_DIR QUESTIONS_FILE; \
	fi; \
	$(COMPOSE) --profile tools run --rm --build --user $(HOST_USER) evalrun $(EVAL_ARGS)

# Отчёт из eval/results.jsonl без обращения к сервису и сравнение двух прогонов из eval/.
eval-report: env
	$(COMPOSE) --profile tools run --rm --build --no-deps --user $(HOST_USER) evalrun python -m evalrun report

eval-compare: env
	@test -n "$(A)" -a -n "$(B)" || { echo 'нужно: make eval-compare A=eval/runs/<первый>.jsonl B=eval/runs/<второй>.jsonl'; exit 2; }
	$(COMPOSE) --profile tools run --rm --build --no-deps --user $(HOST_USER) evalrun python -m evalrun compare /app/$(A) /app/$(B)

# LLM-судья: набор для его проверки — без модели; проверка и оценка прогона — платно,
# с моделью из JUDGE_MODEL, только с подтверждения.
judge-set: env
	$(COMPOSE) --profile tools run --rm --build --no-deps --user $(HOST_USER) evalrun python -m evalrun judge-set

judge-check: env
	$(COMPOSE) --profile tools run --rm --build --no-deps --user $(HOST_USER) evalrun python -m evalrun judge-check

judge-report: env
	$(COMPOSE) --profile tools run --rm --build --no-deps --user $(HOST_USER) evalrun python -m evalrun judge-report

judge: env
	@test -n "$(RESULTS)" -a -n "$(NAME)" || { echo 'нужно: make judge RESULTS=eval/results.jsonl NAME=data'; exit 2; }
	$(COMPOSE) --profile tools run --rm --build --no-deps --user $(HOST_USER) evalrun python -m evalrun judge /app/$(RESULTS) $(NAME)

# Мутанты: прогон против сборок сервиса с одной отключённой защитой, в своём проекте compose.
# По умолчанию — послушная заглушка модели; MUTANT_MODE=model — настоящая модель из .env,
# это платный прогон. MUTANTS — имена через пробел, по умолчанию все.
mutants: env
	@MUTANT_MODE='$(MUTANT_MODE)' MUTANTS='$(MUTANTS)' sh scripts/mutants.sh

smoke: env
	@COMPOSE='$(COMPOSE_SMOKE)' SMOKE_PROJECT='$(COMPOSE_PROJECT)-smoke' MAKE='$(MAKE)' sh scripts/smoke.sh

ci:
	@REPORT='$(REPORT)' sh scripts/ci-local.sh

# Стенд в облаке: сервер через Terraform и тот же compose на нём. Создание сервера — платное
# действие, apply и destroy спрашивают подтверждение. Порядок и доступ — docs/development/STAND.md.
stand:
	@bash scripts/stand.sh up

stand-deploy:
	@bash scripts/stand.sh deploy

stand-destroy:
	@bash scripts/stand.sh down

# cloud-init стенда для сервера из панели (панель его отклоняла — STAND.md); выкатка на сервер
# из панели — STAND_HOST=<логин>@<адрес> make stand-deploy.
stand-cloud-init:
	@bash scripts/stand.sh cloud-init

lock:
	docker build --quiet --target uv --tag oes-agent-assistant:uv .
	docker run --rm --user $(HOST_USER) --volume "$(CURDIR)":/app oes-agent-assistant:uv uv lock
