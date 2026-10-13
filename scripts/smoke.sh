#!/bin/sh
# Сценарий сборки: поднимает всё с нуля и проверяет связку, а не логику —
# порядок старта, переменные окружения, монтирование набора, публикацию портов,
# перезапуск сервиса и повторный старт поверх существующего тома базы.
# Заодно сверяет data/ в репозитории с выходом генератора.
#
# Запускается целью `make smoke` в собственном проекте compose: том базы этого проекта
# удаляется в начале и в конце, запущенный рядом `make run` не задевается.
set -eu

: "${SMOKE_PROJECT:=oes-assistant-smoke}"
: "${COMPOSE:=docker compose -p $SMOKE_PROJECT}"
: "${MAKE:=make}"
# Порт на хосте — случайный: постоянный может быть занят работающим `make run`.
export SERVICE_PORT=0

step() { printf '\n== %s\n' "$1"; }
fail() { printf 'smoke: ПРОВАЛ — %s\n' "$1" >&2; exit 1; }

# Свой каталог набора с файлом-меткой: проверяется, что в контейнер попадает именно
# каталог с хоста и что подмена DATA_DIR действует. Каталог лежит внутри проекта:
# не всякий Docker видит системный каталог временных файлов.
marker="smoke-marker-$$"
smoke_data="$PWD/.smoke/data"
rm -rf "$PWD/.smoke"
mkdir -p "$smoke_data"
: > "$smoke_data/$marker"
: > "$smoke_data/questions.jsonl"
export DATA_DIR="$smoke_data"
# Имя модели задано только в окружении оболочки: в .env копии оно пустое.
export MODEL_NAME="smoke-model-$$"
mkdir -p "$PWD/.smoke/q dir"
: > "$PWD/.smoke/q dir/my questions.jsonl"

cleanup() {
    status=$?
    rm -rf "$PWD/.smoke"
    if [ "$status" -ne 0 ]; then
        printf '\n== журналы контейнеров\n' >&2
        $COMPOSE logs --no-color --tail 50 >&2 || true
    fi
    $COMPOSE down --volumes --remove-orphans >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

fetch() {
    # Запрос изнутри сети compose: на хосте нет ни curl, ни Python.
    $COMPOSE exec -T assistant python -c \
        "import sys, urllib.request as u; print(u.urlopen(sys.argv[1], timeout=5).read().decode())" "$1"
}

step "без переменных compose останавливается и называет недостающую"
if out=$(env -u POSTGRES_PASSWORD $COMPOSE --env-file /dev/null config 2>&1); then
    fail "compose принял пустое окружение"
fi
# Не заданы все обязательные переменные сразу; какую из них compose назовёт первой,
# зависит от его версии. Имя переменной стоит в самой подсказке, поэтому её достаточно.
case "$out" in
    *"required variable"*"не задан "*" — создайте .env командой make run"*) ;;
    *) fail "нет имени переменной или подсказки: $out" ;;
esac

step "набор по умолчанию — каталог data/ проекта"
case "$(DATA_DIR=data/ $COMPOSE config 2>&1)" in
    *"source: $PWD/data"*) ;;
    *) fail "DATA_DIR=data/ не превратился в каталог data проекта" ;;
esac

step "data/ в репозитории — ровно то, что пишет генератор"
# Генератор пишет в свой каталог, а не в data/: сценарий сборки набор не трогает.
# Сверка стоит здесь, а не в make test: тесты обязаны проходить и с чужим набором в data/.
mkdir -p "$PWD/.smoke/generated"
$COMPOSE --profile tools run --rm --build -T --user "$(id -u):$(id -g)" \
    --volume "$PWD/.smoke/generated:/out" datagen python -m datagen --out /out >/dev/null \
    || fail "генератор не прошёл собственную проверку"
diff -r -x .DS_Store "$PWD/.smoke/generated" "$PWD/data" \
    || fail "data/ отличается от выхода генератора. Если в data/ намеренно лежит другой набор, это ожидаемо; если набор репозитория — make data перезапишет его выходом генератора"

step "чистый старт"
$COMPOSE down --volumes --remove-orphans >/dev/null 2>&1 || true
$COMPOSE up --build --detach --wait

step "загрузчик завершился без ошибки до старта сервиса и заглушки"
loader=$($COMPOSE ps --all --quiet loader)
[ -n "$loader" ] || fail "контейнера загрузчика нет"
[ "$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$loader")" = "exited 0" ] \
    || fail "загрузчик не завершился с кодом 0"
loader_done=$(docker inspect --format '{{.State.FinishedAt}}' "$loader")
for service in assistant toir; do
    started=$(docker inspect --format '{{.State.StartedAt}}' "$($COMPOSE ps --quiet "$service")")
    # Метки времени в одном формате и поясе: строки сравниваются как время.
    [ "$(printf '%s\n%s\n' "$loader_done" "$started" | sort | head -n 1)" = "$loader_done" ] \
        || fail "$service стартовал раньше, чем завершился загрузчик"
done

step "имя модели из окружения оболочки дошло до сервиса"
[ "$($COMPOSE exec -T assistant printenv MODEL_NAME)" = "$MODEL_NAME" ] \
    || fail "MODEL_NAME из оболочки не дошёл до сервиса"

step "набор смонтирован в загрузчик с хоста и только на чтение"
mounts=$(docker inspect --format '{{range .Mounts}}{{.Destination}}={{.RW}} {{end}}' "$loader")
case "$mounts" in
    *"/data=false"*) ;;
    *) fail "ожидалось /data=false, получено: $mounts" ;;
esac
$COMPOSE run --rm --no-deps -T loader test -f "/data/$marker" \
    || fail "в /data загрузчика нет файла с хоста: каталог набора не смонтирован"
if $COMPOSE run --rm --no-deps -T --user 0 loader touch /data/written 2>/dev/null; then
    fail "в /data удалось записать"
fi

step "прогон видит вопросы и набор на чтение, каталог результатов — на запись"
$COMPOSE --profile tools run --rm -T --user "$(id -u):$(id -g)" evalrun python -c "
import sys
from pathlib import Path
from evalrun.mounts import check_mounts
problems = check_mounts(Path('/questions/questions.jsonl'), Path('/data'), Path('/app/eval'))
if not Path('/data/$marker').is_file():
    problems.append('в /data нет файла с хоста')
print('\n'.join(problems))
sys.exit(1 if problems else 0)
" || fail "монтирования прогона не на месте"
if $COMPOSE --profile tools run --rm -T --user 0 evalrun touch /questions/written 2>/dev/null; then
    fail "в /questions удалось записать"
fi

step "make eval QUESTIONS=<путь с пробелами> монтирует каталог своего файла"
$MAKE eval COMPOSE_PROJECT="$SMOKE_PROJECT" QUESTIONS=".smoke/q dir/my questions.jsonl" \
    EVAL_ARGS='sh -c "test \"\$$QUESTIONS\" = \"/questions/my questions.jsonl\" && test -f \"\$$QUESTIONS\" && test -d /data && test -w /app/eval"' \
    || fail "make eval не увидел свой файл вопросов"

step "сервис и заглушка отвечают внутри сети"
case "$(fetch http://127.0.0.1:8000/healthz)" in *'"ok"'*) ;; *) fail "сервис не готов" ;; esac
case "$(fetch http://toir:8001/healthz)" in *'"ok"'*) ;; *) fail "заглушка недоступна сервису" ;; esac

step "имя host.docker.internal разрешается в контейнере сервиса"
$COMPOSE exec -T assistant python -c \
    "import socket; socket.gethostbyname('host.docker.internal')" \
    || fail "имя host.docker.internal не разрешается"

step "наружу опубликован только порт сервиса, и только на заданном адресе"
published=$($COMPOSE port assistant 8000) || fail "порт сервиса не опубликован"
case "$published" in
    ""|":0") fail "порт сервиса не опубликован" ;;
    0.0.0.0:*|"[::]:"*|:::*)
        [ "${SERVICE_BIND:-}" = "0.0.0.0" ] || fail "порт сервиса открыт всем: $published" ;;
esac
for pair in "postgres 5432" "toir 8001"; do
    # shellcheck disable=SC2086
    out=$($COMPOSE port $pair 2>/dev/null) || out=""
    # Для неопубликованного порта compose печатает «:0».
    case "$out" in
        ""|":0") ;;
        *) fail "опубликован лишний порт: $pair -> $out" ;;
    esac
done

step "перезапуск сервиса"
$COMPOSE restart assistant >/dev/null
$COMPOSE up --detach --wait
case "$(fetch http://127.0.0.1:8000/healthz)" in *'"ok"'*) ;; *) fail "сервис не поднялся после перезапуска" ;; esac

step "повторный старт поверх существующего тома базы"
$COMPOSE down --remove-orphans >/dev/null 2>&1
$COMPOSE up --detach --wait
case "$(fetch http://toir:8001/healthz)" in *'"ok"'*) ;; *) fail "после повторного старта заглушка недоступна" ;; esac

printf '\nsmoke: всё прошло\n'
