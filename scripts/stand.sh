#!/usr/bin/env bash
# Стенд в облаке: сервер — Terraform в infra/terraform, сервис — тот же compose, что локально.
#
#   scripts/stand.sh up [аргументы apply]   создать сервер (спросит подтверждение) и развернуть
#   scripts/stand.sh deploy   выкатить текущий коммит на уже созданный сервер
#   scripts/stand.sh down     удалить стенд (спросит подтверждение)
#   scripts/stand.sh cloud-init   напечатать cloud-init для сервера, созданного в панели
#
# STAND_HOST=<логин>@<адрес> — выкатка на сервер, созданный не Terraform, а вручную,
# например бесплатный сервер из панели Cloud.ru: тогда адрес не берётся из Terraform.
#
# На сервер уходит закоммиченный код (`git archive`, вместе с data/) и из локального .env —
# только настройки модели и цены. Пароли базы и TOIR_TOKEN сервер создаёт себе сам из
# .env.example при первом развёртывании и хранит между выкатками. Ключа облака на сервере нет.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
tf="$root/infra/terraform/tf.sh"
env_file="${ENV_FILE:-$root/.env}"
login="${STAND_LOGIN:-oes}"
# Ключ хоста — свой файл: при пересоздании стенда адрес может повториться с новым ключом.
state="$root/.stand"
known="$state/known_hosts"
# Каталоги на сервере: код заменяется при каждой выкатке, .env остаётся.
remote_home="oes"

# Что из локального .env нужно серверу: модель и цены. Секреты базы и ТОиР — свои на сервере.
MODEL_VARS="MODEL_BASE_URL MODEL_API_KEY MODEL_NAME MODEL_MAX_OUTPUT_TOKENS MODEL_TIMEOUT_SECONDS
MODEL_RESPONSE_FORMAT MODEL_EXTRA_BODY MODEL_TOKEN_CHARS PRICE_INPUT PRICE_CACHED PRICE_OUTPUT"

die() { echo "stand: $*" >&2; exit 1; }

host() {
    local ip
    if [[ -n "${STAND_HOST:-}" ]]; then
        echo "$STAND_HOST"
        return
    fi
    ip="$("$tf" output -raw public_ip 2>/dev/null)" || die "стенда нет: сначала scripts/stand.sh up"
    [[ -n "$ip" ]] || die "Terraform не вернул адрес стенда"
    echo "$login@$ip"
}

# Закрытый ключ — пара к публичному, который Terraform положил на сервер:
# STAND_SSH_PUBLIC_KEY_FILE без «.pub», из окружения или из .env, как у tf.sh.
identity() {
    local pub="${STAND_SSH_PUBLIC_KEY_FILE:-}"
    if [[ -z "$pub" && -f "$env_file" ]]; then
        pub="$(grep -E '^STAND_SSH_PUBLIC_KEY_FILE=' "$env_file" | tail -n 1 | cut -d= -f2- || true)"
    fi
    pub="${pub:-$HOME/.ssh/id_ed25519.pub}"
    pub="${pub/#\~/$HOME}"
    echo "${pub%.pub}"
}

remote() {
    local key
    key="$(identity)"
    local opts=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new
        -o UserKnownHostsFile="$known")
    [[ -f "$key" ]] && opts+=(-i "$key" -o IdentitiesOnly=yes)
    ssh "${opts[@]}" "$target" "$@"
}

# Сервер отвечает по SSH и закончил первую настройку: Docker стоит.
wait_ready() {
    local attempt
    for attempt in $(seq 1 60); do
        if remote true 2>/dev/null; then
            break
        fi
        [[ "$attempt" == 60 ]] && die "сервер не отвечает по SSH"
        sleep 5
    done
    echo "stand: жду окончания первой настройки сервера"
    # Код 2 — настройка закончена с некритичными предупреждениями (degraded done): у образов
    # панели это обычное дело. Провал — только код 1.
    local status=0
    remote cloud-init status --wait >/dev/null || status=$?
    [[ "$status" == 0 || "$status" == 2 ]] \
        || die "первая настройка сервера не удалась: cloud-init status --long на сервере"
}

# Тот же cloud-init, что Terraform передаёт серверу, без комментариев и пустых строк: форма
# создания в панели отвергает его целиком, без указания причины.
cloud_init() {
    local pub key
    pub="$(identity).pub"
    [[ -f "$pub" ]] || die "нет публичного ключа $pub; задайте STAND_SSH_PUBLIC_KEY_FILE"
    key="$(tr -d '\n' < "$pub" | sed 's/"/\\"/g')"
    sed -e "s/\${hostname}/oes-stand/" -e "s/\${login}/$login/g" \
        -e "s|\${ssh_public_key_yaml}|\"$key\"|" "$root/infra/terraform/cloud-init.yaml.tftpl" \
        | grep -v -E '^[[:space:]]*#[^!c]|^[[:space:]]*#$|^$'
}

# Настройки модели из локального .env, строками ИМЯ=значение. Файл не исполняется. Каждое имя
# передаётся, отсутствующее — пустым: иначе на сервере осталось бы значение прошлой выкатки.
model_env() {
    local name line
    [[ -f "$env_file" ]] || die "нет $env_file с настройками модели"
    for name in $MODEL_VARS; do
        line="$(grep -E "^${name}=" "$env_file" | tail -n 1 || true)"
        printf '%s\n' "${line:-$name=}"
    done
}

deploy() {
    target="$(host)"
    mkdir -p "$state"
    wait_ready
    # Сервер из панели cloud-init стенда не получил: Docker и подкачка ставятся здесь.
    if ! remote 'command -v docker >/dev/null'; then
        echo "stand: на сервере нет Docker — ставлю"
        remote "sudo sh -s -- ${target%@*}" < "$root/scripts/stand-bootstrap.sh"
    fi

    [[ -z "$(git -C "$root" status --porcelain)" ]] \
        || echo "stand: в рабочей копии есть незакоммиченные изменения — выкатывается коммит без них" >&2
    local sha
    sha="$(git -C "$root" rev-parse --short HEAD)"
    echo "stand: выкатываю коммит $sha на $target"

    # Настройки модели — через stdin, а не в аргументах: аргументы видны в списке процессов.
    model_env | remote "umask 077 && mkdir -p $remote_home && cat > $remote_home/model.env"
    git -C "$root" archive HEAD \
        | remote "rm -rf $remote_home/app.new && mkdir -p $remote_home/app.new && tar -x -C $remote_home/app.new"
    remote "bash -s -- $remote_home $sha" < "$root/scripts/stand-remote.sh"
    echo "stand: готово. Сервис слушает порт на сервере; снаружи он открыт, только если задан TF_VAR_service_cidr"
}

case "${1:-}" in
    up)
        "$tf" init -input=false >/dev/null
        "$tf" apply "${@:2}"
        # Новый сервер — новый ключ хоста.
        rm -f "$known"
        deploy
        ;;
    deploy) deploy ;;
    cloud-init) cloud_init ;;
    down)
        "$tf" destroy
        rm -rf "$state"
        ;;
    *) die "использование: scripts/stand.sh up | deploy | down | cloud-init" ;;
esac
