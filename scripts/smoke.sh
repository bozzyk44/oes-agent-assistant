#!/bin/sh
# Сценарий сборки: поднимает всё с нуля и проверяет связку, а не логику —
# порядок старта, переменные окружения, монтирование набора, публикацию портов,
# перезапуск сервиса и повторный старт поверх существующего тома базы.
#
# Запускается целью `make smoke`. Том базы проекта удаляется в начале и в конце:
# не запускайте на проекте с данными, которые жалко. `make ci` берёт отдельный проект.
set -eu

: "${COMPOSE:=docker compose}"

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
case "$out" in
    *POSTGRES_PASSWORD*) ;;
    *) fail "в сообщении нет имени переменной: $out" ;;
esac

step "чистый старт"
$COMPOSE down --volumes --remove-orphans >/dev/null 2>&1 || true
$COMPOSE up --build --detach --wait

step "загрузчик завершился без ошибки до старта сервиса"
loader=$($COMPOSE ps --all --quiet loader)
[ -n "$loader" ] || fail "контейнера загрузчика нет"
[ "$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$loader")" = "exited 0" ] \
    || fail "загрузчик не завершился с кодом 0"
loader_done=$(docker inspect --format '{{.State.FinishedAt}}' "$loader")
service_start=$(docker inspect --format '{{.State.StartedAt}}' "$($COMPOSE ps --quiet assistant)")
# Метки времени в одном формате и поясе: строки сравниваются как время.
[ "$(printf '%s\n%s\n' "$loader_done" "$service_start" | sort | head -n 1)" = "$loader_done" ] \
    || fail "сервис стартовал раньше, чем завершился загрузчик"

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

step "сервис и заглушка отвечают внутри сети"
case "$(fetch http://127.0.0.1:8000/healthz)" in *'"ok"'*) ;; *) fail "сервис не готов" ;; esac
case "$(fetch http://toir:8001/healthz)" in *'"ok"'*) ;; *) fail "заглушка недоступна сервису" ;; esac

step "хост доступен сервису по имени host.docker.internal"
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
