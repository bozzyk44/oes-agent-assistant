#!/bin/sh
# Выполняется на сервере стенда от root: его передаёт scripts/stand.sh, если на сервере нет
# Docker. Делает то же, что cloud-init из infra/terraform/cloud-init.yaml.tftpl: сервер,
# созданный в панели, cloud-init стенда не получил — панель отвергает его без объяснения.
# Аргумент — пользователь, которому нужен доступ к Docker. Повтор ничего не меняет.
set -eu

user="$1"

# Подкачка: на сервере 4 ГБ памяти. Каждый шаг проверяется отдельно: прерванный запуск
# повтор доделывает. Скрипт читается из stdin, поэтому у команд — </dev/null.
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    [ -f /swapfile ] || fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Пакеты — с ожиданием блокировки и повторами: при первом запуске менеджер пакетов занят
# фоновым обновлением системы.
export DEBIAN_FRONTEND=noninteractive
attempt=1
until apt-get -o DPkg::Lock::Timeout=300 update -q >/dev/null </dev/null \
    && apt-get -o DPkg::Lock::Timeout=300 install -y -q docker.io docker-compose-v2 make git >/dev/null </dev/null; do
    [ "$attempt" -ge 6 ] && { echo "stand: пакеты не поставились" >&2; exit 1; }
    attempt=$((attempt + 1))
    sleep 20
done
systemctl enable --now docker </dev/null
usermod -aG docker "$user"
echo "stand: Docker поставлен"
