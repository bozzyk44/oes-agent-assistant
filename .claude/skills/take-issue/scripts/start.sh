#!/usr/bin/env bash
# Ветка и рабочая копия под задачу. Запускается из любого каталога этого репозитория.
# Использование: start.sh <номер задачи> <feat|fix|docs|infra> <кратко-латиницей> [база]
# База — от чего ветвиться; по умолчанию origin/main. Нужна, когда задача зависит от ещё не слитой ветки.
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "использование: start.sh <номер задачи> <feat|fix|docs|infra> <кратко-латиницей> [база]" >&2
  exit 2
fi

number="$1" kind="$2" slug="$3" base="${4:-origin/main}"

[[ "$number" =~ ^[1-9][0-9]*$ ]] || { echo "номер задачи — положительное число без ведущих нулей, получено: $number" >&2; exit 2; }
case "$kind" in
  feat | fix | docs | infra) ;;
  *) echo "тип — feat, fix, docs или infra; получено: $kind" >&2; exit 2 ;;
esac
[[ "$slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || { echo "краткое имя — латиница, цифры и дефисы: $slug" >&2; exit 2; }

# Скрипт работает с тем репозиторием, в котором лежит сам, а не с текущим каталогом.
script_dir="$(cd "$(dirname "$0")" && pwd)"
common_dir="$(git -C "$script_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
  echo "не удалось определить каталог репозитория; нужен git 2.31 или новее" >&2
  exit 1
}
main_dir="$(dirname "$common_dir")"
branch="$kind/$number-$slug"
tree="$(dirname "$main_dir")/$(basename "$main_dir").worktrees/$branch"

git -C "$main_dir" fetch --quiet --prune origin

# На одну задачу — одна ветка. Ищем по номеру, а не по полному имени: в новой сессии
# краткое имя легко придумать другим.
existing="$(
  {
    git -C "$main_dir" for-each-ref --format='%(refname:short)' "refs/heads/*/$number-*"
    git -C "$main_dir" for-each-ref --format='%(refname:short)' "refs/remotes/origin/*/$number-*" | sed 's#^origin/#origin: #'
  } | sort -u
)"
if [[ -n "$existing" ]]; then
  echo "для задачи $number ветка уже есть — продолжай в ней, новую не создавай:" >&2
  echo "$existing" | sed 's/^/  /' >&2
  git -C "$main_dir" worktree list | grep -E "/$number-" | sed 's/^/  рабочая копия: /' >&2 || true
  exit 1
fi
if [[ -e "$tree" ]]; then
  echo "каталог уже существует: $tree" >&2
  exit 1
fi
git -C "$main_dir" rev-parse --verify --quiet "$base^{commit}" >/dev/null || { echo "нет такой базы: $base" >&2; exit 1; }

# --no-track: ветка не должна считать своим источником main, иначе обычный push
# подскажет отправить изменения прямо туда.
git -C "$main_dir" worktree add --quiet --no-track "$tree" -b "$branch" "$base"

# Секреты живут в одном файле основного каталога; в рабочей копии — ссылка на него.
if [[ -f "$main_dir/.env" ]]; then
  ln -s "$main_dir/.env" "$tree/.env"
  env_note="подключён ссылкой на $main_dir/.env"
else
  env_note="НЕ подключён: в основном каталоге нет .env. Создай его там, а не в рабочей копии"
fi

echo "ветка:         $branch (от $base)"
echo "рабочая копия: $tree"
echo ".env:          $env_note"
