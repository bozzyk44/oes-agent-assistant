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
# Метка времени Docker с дробной частью ровно в девять знаков: Docker отбрасывает
# конечные нули, и без выравнивания «…00.5Z» оказалось бы позже «…00.123Z» как строка.
stamp() {
    whole=${1%Z}
    seconds=${whole%%.*}
    case "$whole" in *.*) fraction=${whole#*.} ;; *) fraction=0 ;; esac
    printf '%s.%-9s' "$seconds" "$fraction" | tr ' ' '0'
}
# Первое событие не позже второго. Метки в одном поясе: выровненные строки
# сравниваются как время.
not_later() {
    first=$(stamp "$1")
    [ "$(printf '%s\n%s\n' "$first" "$(stamp "$2")" | sort | head -n 1)" = "$first" ]
}

# Свой каталог набора с файлом-меткой: проверяется, что в контейнер попадает именно
# каталог с хоста и что подмена DATA_DIR действует. Сам набор в него пишет генератор
# с другим зерном, чем у data/: сервис поднимается не на наборе из репозитория.
# Каталог лежит внутри проекта: не всякий Docker видит системный каталог временных файлов.
marker="smoke-marker-$$"
smoke_data="$PWD/.smoke/data"
rm -rf "$PWD/.smoke"
mkdir -p "$smoke_data"
: > "$smoke_data/$marker"
: > "$smoke_data/questions.jsonl"
export DATA_DIR="$smoke_data"
# Имя модели задано только в окружении оболочки: в .env копии оно пустое.
export MODEL_NAME="smoke-model-$$"
# Адрес и ключ модели сценарий обнуляет: в рабочей копии .env — ссылка на настоящий,
# и ход, проверяемый ниже, не должен уйти к настоящей модели.
export MODEL_BASE_URL="" MODEL_API_KEY=""
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

step "набор сценария — от генератора с другим зерном"
$COMPOSE --profile tools run --rm -T --user "$(id -u):$(id -g)" \
    --volume "$smoke_data:/out" datagen python -m datagen --seed 7 --out /out >/dev/null \
    || fail "генератор не записал набор сценария"
[ -f "$smoke_data/$marker" ] || fail "генератор стёр посторонний файл в каталоге набора"

step "чистый старт"
$COMPOSE down --volumes --remove-orphans >/dev/null 2>&1 || true
$COMPOSE up --build --detach --wait

step "роли, миграции и загрузка — три шага по порядку, каждый завершился без ошибки"
previous_done=""
for one_off in bootstrap migrate loader; do
    container=$($COMPOSE ps --all --quiet "$one_off")
    [ -n "$container" ] || fail "нет контейнера шага $one_off"
    [ "$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$container")" = "exited 0" ] \
        || fail "шаг $one_off не завершился с кодом 0"
    started=$(docker inspect --format '{{.State.StartedAt}}' "$container")
    if [ -n "$previous_done" ]; then
        not_later "$previous_done" "$started" \
            || fail "шаг $one_off стартовал раньше, чем завершился предыдущий"
    fi
    previous_done=$(docker inspect --format '{{.State.FinishedAt}}' "$container")
    # Пароль администратора сервера — только у шага ролей и баз. Сравниваются имена
    # переменных; значения не печатаются.
    names=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" | cut -d= -f1)
    case "$one_off:$(printf '%s\n' "$names" | grep -c -x POSTGRES_PASSWORD)" in
        bootstrap:1|migrate:0|loader:0) ;;
        *) fail "пароль администратора сервера не там: шаг $one_off" ;;
    esac
done
case "$($COMPOSE logs --no-log-prefix migrate 2>&1)" in
    *"миграции применены"*) ;;
    *) fail "шаг миграций не сообщил о применении миграций на пустую базу" ;;
esac

step "загрузчик завершился до старта сервиса и заглушки"
# После цикла выше в переменных — последний шаг, загрузчик.
loader=$container
for service in assistant toir; do
    started=$(docker inspect --format '{{.State.StartedAt}}' "$($COMPOSE ps --quiet "$service")")
    not_later "$previous_done" "$started" \
        || fail "$service стартовал раньше, чем завершился загрузчик"
done

step "make migrate поверх работающего запуска ничего не меняет"
# Разовый контейнер удаляется сразу, журнала от него не остаётся: вывод печатается здесь.
service_before=$(docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$($COMPOSE ps --quiet assistant)")
migrate_out=$($MAKE migrate COMPOSE_PROJECT="$SMOKE_PROJECT" 2>&1) || true
case "$migrate_out" in
    *"схема уже на последней версии"*) ;;
    *)
        printf '%s\n' "$migrate_out" | tail -n 30 >&2
        fail "повторное применение миграций что-то изменило или не прошло"
        ;;
esac
# Отдельный шаг миграций работающий сервис не трогает: контейнер и время старта те же.
service_after=$(docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$($COMPOSE ps --quiet assistant)")
[ "$service_before" = "$service_after" ] || fail "make migrate перезапустил или пересоздал сервис"

step "набор сценария загружен, база под ролью сервиса в порядке"
case "$($COMPOSE logs --no-log-prefix loader 2>&1)" in
    *"набор загружен"*) ;;
    *) fail "загрузчик не сообщил о загрузке набора" ;;
esac
# Под ролью сервиса: полномочия читаются, строки КХД без привязки к пользователю не видны,
# в базу заглушки роль не входит. Адрес берётся из окружения контейнера и не печатается.
check_database() {
    $COMPOSE exec -T assistant python -c "
import os, sys
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
url = os.environ['DATABASE_URL']
engine = create_engine(url)
with engine.connect() as c:
    users = c.scalar(text('select count(*) from app.users'))
    hidden = c.scalar(text('select count(*) from dwh.production'))
with engine.begin() as c:
    # С привязкой к пользователю, у которого есть полномочие, строки видны: пустая
    # таблица прошла бы проверку выше и без построчной защиты.
    user = c.scalar(text(\"select user_id from app.kpi_grants where table_name = 'production' limit 1\"))
    c.execute(text(\"select set_config('app.user_id', :u, true)\"), {'u': user or ''})
    visible = c.scalar(text('select count(*) from dwh.production'))
    owned = c.scalar(text(\"select count(*) from pg_tables where tableowner = current_user\"))
if not users or not visible:
    sys.exit('пользователи не загружены или строки КХД не видны и с привязкой')
if hidden or owned:
    sys.exit('роль сервиса видит строки КХД без привязки или владеет таблицами')
try:
    # Имя базы заглушки — из образца окружения: сценарий создаёт .env из него.
    create_engine(url.rsplit('/', 1)[0] + '/toir').connect()
except OperationalError as error:
    # Именно отказ в праве подключения: «базы нет» или «сервер недоступен» — не он.
    sys.exit(0 if 'permission denied for database' in str(error) else 'вход в базу заглушки не проверен: ' + str(error.orig).split(chr(10))[0])
sys.exit('роль сервиса вошла в базу заглушки')
"
}
check_database || fail "база после загрузчика не в том состоянии"

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

step "ход: без заголовка — 401, без модели — ответ по контракту с outcome: error"
# Адрес модели в сценарии пуст: сервис стартовал, а ход честно завершается ошибкой.
[ -z "$($COMPOSE exec -T assistant printenv MODEL_BASE_URL)" ] \
    || fail "адрес модели дошёл до сервиса: сценарий сборки не должен звать настоящую модель"
$COMPOSE exec -T assistant python -c "
import json, os, sys, urllib.error, urllib.request as u
from sqlalchemy import create_engine, text
def post(headers):
    body = json.dumps({'session_id': 'smoke', 'message': 'проверка'}).encode()
    request = u.Request('http://127.0.0.1:8000/turn', data=body,
                        headers={'Content-Type': 'application/json', **headers})
    try:
        with u.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, {}
if post({})[0] != 401:
    sys.exit('ход без X-User-ID не отклонён кодом 401')
if post({'X-User-ID': 'smoke-nobody'})[0] != 401:
    sys.exit('ход неизвестного пользователя не отклонён кодом 401')
with create_engine(os.environ['DATABASE_URL']).connect() as c:
    user = c.scalar(text('select user_id from app.users order by 1 limit 1'))
status, reply = post({'X-User-ID': user})
if status != 200 or reply.get('outcome') != 'error' or 'usage' not in reply:
    sys.exit('ход без модели не дал ответ по контракту с outcome: error')
" || fail "ход через API не в том состоянии"

step "сервис и заглушка отвечают внутри сети"
case "$(fetch http://127.0.0.1:8000/healthz)" in *'"ok"'*) ;; *) fail "сервис не готов" ;; esac
case "$(fetch http://toir:8001/healthz)" in *'"ok"'*) ;; *) fail "заглушка недоступна сервису" ;; esac

# Заявка в заглушку изнутри контейнера сервиса: адрес и токен — из его окружения,
# в вывод сценария токен не попадает.
toir_call() {
    $COMPOSE exec -T assistant python -c "
import json, os, sys, urllib.error, urllib.request as u
key, mode = sys.argv[1], sys.argv[2]
base = os.environ['TOIR_URL']
auth = {'Authorization': 'Bearer ' + os.environ['TOIR_TOKEN']}
def call(method, path, headers, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = u.Request(base + path, data=data, method=method,
                        headers={**headers, 'Content-Type': 'application/json'})
    try:
        with u.urlopen(request, timeout=5) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, None
body = {'equipment_id': 'SMOKE-1', 'description': 'проверка сборки', 'priority': 3}
if mode == 'create':
    first = call('POST', '/requests', {**auth, 'Idempotency-Key': key}, body)
    again = call('POST', '/requests', {**auth, 'Idempotency-Key': key}, body)
    if first[0] != 201 or again != (200, first[1]):
        sys.exit('повтор с тем же ключом не вернул ту же заявку')
    if call('GET', '/requests', {})[0] != 401:
        sys.exit('список заявок отдан без токена')
status, rows = call('GET', '/requests', auth)
if status != 200 or [row['idempotency_key'] for row in rows].count(key) != 1:
    sys.exit('заявки с ключом нет в списке или она не одна')
" "$1" "$2"
}
smoke_key="smoke-$(date +%s)-$$"

step "заглушка ТОиР: заявка, повтор с тем же ключом, отказ без токена"
toir_call "$smoke_key" create || fail "заглушка ТОиР принимает заявки не по контракту"

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

step "заглушка модели: ход предлагает заявку"
# Заглушка модели — отдельный контейнер из compose.smoke.yaml; сервис пересоздаётся
# с её адресом. Настоящую модель сценарий не зовёт: ключ в этом файле — метка.
STUBBED="$COMPOSE -f compose.yaml -f compose.smoke.yaml"
$STUBBED up --build --detach --wait modelstub assistant
[ "$($COMPOSE exec -T assistant printenv MODEL_BASE_URL)" = "http://modelstub:8003/v1" ] \
    || fail "сервис не направлен на заглушку модели"
# Пользователь с правом на заявку и оборудование его участка — из набора сценария,
# изнутри загрузчика: набор смонтирован только туда.
pick=$($COMPOSE run --rm --no-deps -T loader python -c "
import csv, json
users = json.load(open('/data/users.json', encoding='utf-8'))
rows = list(csv.DictReader(open('/data/equipment.csv', encoding='utf-8')))
for user in users:
    for row in rows:
        if row['site'] in user.get('repair_requests', []):
            print(user['user_id'], row['equipment_id'])
            raise SystemExit
raise SystemExit('нет пользователя с правом на заявку')
") || fail "в наборе сценария нет пары «пользователь — оборудование» для заявки"
# shellcheck disable=SC2086 — два слова без пробелов внутри: идентификаторы
set -- $pick
repair_user=$1 repair_equipment=$2
# Ход: заглушка вызывает подготовку заявки. Итог вторым шагом — на случай, если ход
# позовёт модель ещё раз; сейчас после черновика ход завершает код. Номер действия — в вывод.
action_id=$($COMPOSE exec -T assistant python -c "
import json, sys, urllib.request as u
user, equipment = sys.argv[1], sys.argv[2]
def send(method, url, body, headers=None):
    request = u.Request(url, data=json.dumps(body).encode(), method=method,
                        headers={'Content-Type': 'application/json', **(headers or {})})
    with u.urlopen(request, timeout=30) as reply:
        return json.load(reply)
steps = [
    {'tool_calls': [{'name': 'create_repair_request', 'arguments': {
        'equipment_id': equipment, 'description': 'проверка сборки: течь', 'priority': 3}}]},
    {'final': {'answer': 'Черновик заявки готов, подтвердите.', 'outcome': 'action'}},
]
send('PUT', 'http://modelstub:8003/script', {'steps': steps})
reply = send('POST', 'http://127.0.0.1:8000/turn',
             {'session_id': 'smoke-repair', 'message': 'Оформи заявку на ремонт ' + equipment},
             {'X-User-ID': user})
pending = reply.get('pending_action') or {}
if reply.get('outcome') != 'action' or not pending.get('action_id'):
    sys.exit('ход не предложил заявку: outcome ' + str(reply.get('outcome')))
if pending.get('params', {}).get('equipment_id') != equipment:
    sys.exit('в черновике не то оборудование')
with u.urlopen('http://modelstub:8003/requests', timeout=5) as stub:
    # После черновика заявки сервис завершает ход сам: второго вызова модели может не быть.
    if json.load(stub)['requests'] < 1:
        sys.exit('ход прошёл не через заглушку модели')
print(pending['action_id'])
" "$repair_user" "$repair_equipment") || fail "ход с заглушкой модели не предложил заявку"

step "перезапуск сервиса между предложением и подтверждением"
$COMPOSE restart assistant >/dev/null
$STUBBED up --detach --wait assistant
case "$(fetch http://127.0.0.1:8000/healthz)" in *'"ok"'*) ;; *) fail "сервис не поднялся после перезапуска" ;; esac

step "подтверждение после перезапуска: ровно одна заявка, повтор её не добавляет"
$COMPOSE exec -T assistant python -c "
import json, os, sys, urllib.request as u
action, user, equipment = sys.argv[1], sys.argv[2], sys.argv[3]
def confirm():
    request = u.Request('http://127.0.0.1:8000/actions/' + action + '/confirm', data=b'',
                        method='POST', headers={'X-User-ID': user})
    with u.urlopen(request, timeout=30) as reply:
        return reply.status, json.load(reply)
first, again = confirm(), confirm()
if first[0] != 200 or first[1].get('status') != 'done' or not first[1].get('request_id'):
    sys.exit('подтверждение после перезапуска не прошло')
if again != first:
    sys.exit('повторное подтверждение вернуло другое')
request = u.Request(os.environ['TOIR_URL'] + '/requests',
                    headers={'Authorization': 'Bearer ' + os.environ['TOIR_TOKEN']})
with u.urlopen(request, timeout=5) as reply:
    rows = json.load(reply)
mine = [row for row in rows if row.get('equipment_id') == equipment]
if len(mine) != 1 or mine[0].get('request_id') != first[1]['request_id']:
    sys.exit('в заглушке ТОиР не ровно одна заявка на это оборудование или не та, что в ответе')
" "$action_id" "$repair_user" "$repair_equipment" \
    || fail "подтверждение заявки не по контракту"
# Сервис — снова с адресом модели из окружения, заглушка модели остановлена: следующий шаг
# перезапускает контейнер, а не пересоздаёт его из-за смены настроек.
$COMPOSE up --detach --wait --remove-orphans

step "перезапуск сервиса"
$COMPOSE restart assistant >/dev/null
$COMPOSE up --detach --wait
case "$(fetch http://127.0.0.1:8000/healthz)" in *'"ok"'*) ;; *) fail "сервис не поднялся после перезапуска" ;; esac

step "повторный старт поверх существующего тома базы"
$COMPOSE down --remove-orphans >/dev/null 2>&1
$COMPOSE up --detach --wait
case "$(fetch http://toir:8001/healthz)" in *'"ok"'*) ;; *) fail "после повторного старта заглушка недоступна" ;; esac
toir_call "$smoke_key" check || fail "заявка в заглушке ТОиР не пережила перезапуск"
# Тот же набор поверх того же тома: загрузчик ничего не меняет, сессии и действия целы.
case "$($COMPOSE logs --no-log-prefix loader 2>&1)" in
    *"набор не изменился"*) ;;
    *) fail "повторный запуск загрузчика перезагрузил тот же набор" ;;
esac
check_database || fail "база после повторного старта не в том состоянии"

printf '\nsmoke: всё прошло\n'
