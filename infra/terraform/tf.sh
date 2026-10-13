#!/usr/bin/env bash
# Terraform в контейнере: на машине нужен только Docker.
# Использование из корня репозитория: infra/terraform/tf.sh init | plan | apply | destroy | output
#
# Переменные Terraform задаются окружением: TF_VAR_service_cidr=203.0.113.7/32 infra/terraform/tf.sh apply
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$dir/../.." && pwd)"
env_file="${ENV_FILE:-$root/.env}"

# Значение переменной из файла окружения. Файл не исполняется: в нём могут быть значения,
# которые не являются корректным кодом оболочки. Заданное в окружении имеет приоритет.
from_env_file() {
  local name="$1" value
  [[ -n "${!name:-}" ]] && return 0
  [[ -f "$env_file" ]] || return 0
  value="$(grep -E "^${name}=" "$env_file" | tail -n 1 | cut -d= -f2- || true)"
  value="${value%$'\r'}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  [[ -n "$value" ]] && export "$name=$value"
  return 0
}

# Командам, которые не обращаются к облаку, ключи не нужны.
needs_cloud=1
case "${1:-}" in
  fmt | validate | version | "") needs_cloud=0 ;;
esac

args=(--rm -i --platform linux/amd64 --user "$(id -u):$(id -g)" -e HOME=/tmp
  -v "$dir":/work -w /work
  -e TF_CLI_CONFIG_FILE=/work/terraform.rc -e TF_IN_AUTOMATION=1)
[[ -t 0 && -t 1 ]] && args+=(-t)

if [[ "$needs_cloud" == 1 ]]; then
  for name in CLOUD_RU_KEY_ID CLOUD_RU_KEY_SECRET CLOUD_RU_PROJECT_ID STAND_SSH_PUBLIC_KEY_FILE; do
    from_env_file "$name"
  done
  : "${CLOUD_RU_KEY_ID:?нет CLOUD_RU_KEY_ID: ключ сервисного аккаунта Cloud.ru}"
  : "${CLOUD_RU_KEY_SECRET:?нет CLOUD_RU_KEY_SECRET: секрет ключа сервисного аккаунта Cloud.ru}"
  : "${CLOUD_RU_PROJECT_ID:?нет CLOUD_RU_PROJECT_ID: идентификатор проекта Cloud.ru}"

  key_file="${STAND_SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}"
  key_file="${key_file/#\~/$HOME}"
  [[ -f "$key_file" ]] || { echo "нет публичного ключа $key_file; задайте STAND_SSH_PUBLIC_KEY_FILE" >&2; exit 1; }

  # Значения передаются контейнеру через окружение по имени, а не в аргументах команды:
  # аргументы видны в списке процессов.
  export AUTH_KEY_ID="$CLOUD_RU_KEY_ID" AUTH_SECRET="$CLOUD_RU_KEY_SECRET"
  export TF_VAR_project_id="$CLOUD_RU_PROJECT_ID"
  TF_VAR_ssh_public_key="$(cat "$key_file")"
  export TF_VAR_ssh_public_key
  args+=(-e AUTH_KEY_ID -e AUTH_SECRET)
fi

# Все TF_VAR_* из окружения доходят до Terraform.
while IFS= read -r name; do
  args+=(-e "$name")
done < <(compgen -e | grep '^TF_VAR_' || true)

# У провайдера Cloud.ru нет сборки под linux/arm64, поэтому контейнер всегда amd64;
# на машинах с ARM он идёт через эмуляцию.
exec docker run "${args[@]}" "hashicorp/terraform:${TERRAFORM_VERSION:-1.9.8}" "$@"
