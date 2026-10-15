#!/bin/sh
# Выполняется на сервере стенда: его передаёт scripts/stand.sh, локально не запускается.
# Аргументы: каталог стенда в домашнем каталоге, коммит и адрес публикации порта сервиса.
# Код уже лежит в <каталог>/app.new, настройки модели — в <каталог>/model.env.
#
# .env живёт в <каталог>/.env и переживает выкатки: пароли базы, созданные при первой
# выкатке, должны совпадать с томом базы. Настройки модели в нём заменяются при каждой.
set -eu

home="$HOME/$1"
sha="$2"
bind="$3"
cd "$home"

[ -d app.new ] || { echo "stand: нет нового кода в $home/app.new" >&2; exit 1; }
rm -rf app.old
[ -d app ] && mv app app.old
mv app.new app
rm -rf app.old

cd app
# .env в каталоге кода — ссылка на постоянный файл. make-env дописывает недостающее и
# при первой выкатке создаёт файл со случайными секретами; значения не печатает.
ln -sfn ../.env .env
sh scripts/make-env.sh </dev/null

# Настройки модели и порт публикации: старые строки этих имён убираются, новые дописываются.
# Файл собирается рядом и заменяет прежний целиком, с правами только для владельца.
umask 077
names=$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' ../model.env)
# Профили compose: прочие, заданные на сервере руками, сохраняются; proxy — по имени стенда,
# observability — по STAND_OBSERVABILITY=1 вместе с адресом приёмника для сервиса.
profiles=$(sed -n 's/^COMPOSE_PROFILES=//p' ../.env | tail -n 1 | tr ',' '\n' | grep -vx -e 'proxy' -e 'observability' | grep -v '^$' || true)
if grep -q '^STAND_DOMAIN=.' ../model.env; then
    profiles=$(printf '%s\nproxy' "$profiles")
fi
otel=
if grep -qx 'STAND_OBSERVABILITY=1' ../model.env; then
    profiles=$(printf '%s\nobservability' "$profiles")
    otel=http://lgtm:4318
fi
profiles=$(printf '%s\n' "$profiles" | grep -v '^$' | paste -sd, - || true)
{
    grep -v -E "^($(printf '%s|' $names SERVICE_BIND COMPOSE_PROFILES OTEL_EXPORTER_OTLP_ENDPOINT | sed 's/|$//'))=" ../.env || true
    cat ../model.env
    [ -z "$profiles" ] || echo "COMPOSE_PROFILES=$profiles"
    echo "OTEL_EXPORTER_OTLP_ENDPOINT=$otel"
    # 0.0.0.0 — только на сервере, где группу безопасности создаёт Terraform этого
    # репозитория; на сервере из панели — 127.0.0.1, проверка через SSH-туннель.
    echo "SERVICE_BIND=$bind"
} > ../.env.new
mv ../.env.new ../.env
rm -f ../model.env

# Имя стенда убрано — прокси останавливается: up без профиля его контейнер не трогает, и
# он продолжал бы пускать снаружи по старому имени.
case ",$profiles," in
    *,proxy,*) ;;
    *) docker compose -p oes-assistant --profile proxy rm --stop --force proxy </dev/null ;;
esac
# То же с наблюдаемостью: выключенная, она занимала бы до 1,2 ГБ памяти.
case ",$profiles," in
    *,observability,*) ;;
    *) docker compose -p oes-assistant --profile observability rm --stop --force lgtm </dev/null ;;
esac

# Скрипт читается из stdin: дочерние команды его не получают.
GIT_COMMIT="$sha" docker compose -p oes-assistant up -d --build --wait --remove-orphans </dev/null
docker image prune -f >/dev/null </dev/null

port=$(sed -n 's/^SERVICE_PORT=//p' ../.env | tail -n 1)
info=$(curl -fsS "http://127.0.0.1:${port:-8000}/info")
echo "stand: сервис отвечает: $info"
