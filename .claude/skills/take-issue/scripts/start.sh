#!/usr/bin/env bash
# Ветка и рабочая копия под задачу. Запускается из любого каталога репозитория.
# Использование: start.sh <номер задачи> <feat|fix|docs|infra> <кратко-латиницей>
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "использование: start.sh <номер задачи> <feat|fix|docs|infra> <кратко-латиницей>" >&2
  exit 2
fi

number="$1" kind="$2" slug="$3"

[[ "$number" =~ ^[0-9]+$ ]] || { echo "номер задачи — число, получено: $number" >&2; exit 2; }
case "$kind" in
  feat | fix | docs | infra) ;;
  *) echo "тип — feat, fix, docs или infra; получено: $kind" >&2; exit 2 ;;
esac
[[ "$slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || { echo "краткое имя — латиница, цифры и дефисы: $slug" >&2; exit 2; }

# Основной каталог репозитория, даже если скрипт запущен из рабочей копии.
main_dir="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
branch="$kind/$number-$slug"
tree="$(dirname "$main_dir")/$(basename "$main_dir").worktrees/$branch"

if [[ -e "$tree" ]]; then
  echo "рабочая копия уже есть: $tree" >&2
  exit 1
fi
if git -C "$main_dir" show-ref --verify --quiet "refs/heads/$branch"; then
  echo "ветка уже есть: $branch" >&2
  exit 1
fi

git -C "$main_dir" fetch --quiet origin
git -C "$main_dir" worktree add --quiet "$tree" -b "$branch" origin/main

# Секреты живут в одном файле основного каталога; в рабочей копии — ссылка на него.
if [[ -f "$main_dir/.env" ]]; then
  ln -s "$main_dir/.env" "$tree/.env"
  env_note="подключён ссылкой"
else
  env_note="в основном каталоге нет .env"
fi

echo "ветка:         $branch"
echo "рабочая копия: $tree"
echo ".env:          $env_note"
