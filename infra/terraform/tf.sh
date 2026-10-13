#!/usr/bin/env bash
# Terraform в контейнере: на машине нужен только Docker.
# Использование из корня репозитория: infra/terraform/tf.sh init | plan | apply | destroy | output
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$dir/../.." && pwd)"
env_file="${ENV_FILE:-$root/.env}"

if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$env_file"
  set +a
fi

: "${CLOUD_RU_KEY_ID:?нет CLOUD_RU_KEY_ID: ключ сервисного аккаунта Cloud.ru}"
: "${CLOUD_RU_KEY_SECRET:?нет CLOUD_RU_KEY_SECRET: секрет ключа сервисного аккаунта Cloud.ru}"
: "${CLOUD_RU_PROJECT_ID:?нет CLOUD_RU_PROJECT_ID: идентификатор проекта Cloud.ru}"

key_file="${STAND_SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}"
[[ -f "$key_file" ]] || { echo "нет публичного ключа $key_file; задайте STAND_SSH_PUBLIC_KEY_FILE" >&2; exit 1; }

tty=()
[[ -t 0 && -t 1 ]] && tty=(-t)

# У провайдера Cloud.ru нет сборки под linux/arm64, поэтому контейнер всегда amd64;
# на машинах с ARM он идёт через эмуляцию.
exec docker run --rm -i "${tty[@]}" --platform linux/amd64 \
  -v "$dir":/work -w /work \
  -e TF_CLI_CONFIG_FILE=/work/terraform.rc \
  -e TF_IN_AUTOMATION=1 \
  -e AUTH_KEY_ID="$CLOUD_RU_KEY_ID" \
  -e AUTH_SECRET="$CLOUD_RU_KEY_SECRET" \
  -e TF_VAR_project_id="$CLOUD_RU_PROJECT_ID" \
  -e TF_VAR_ssh_public_key="$(cat "$key_file")" \
  "hashicorp/terraform:${TERRAFORM_VERSION:-1.9}" "$@"
