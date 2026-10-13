#!/usr/bin/env bash
# Уборка после слияния: рабочая копия, локальная и удалённая ветка, обновление main.
# Использование: finish.sh <номер PR> [--discard-ignored]
# Отказывается работать, пока PR не слит, и пока в рабочей копии есть что терять.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "использование: finish.sh <номер PR> [--discard-ignored]" >&2
  exit 2
fi
pr="$1" discard="${2:-}"
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || { echo "номер PR — число, получено: $pr" >&2; exit 2; }
[[ -z "$discard" || "$discard" == "--discard-ignored" ]] || { echo "неизвестный параметр: $discard" >&2; exit 2; }

script_dir="$(cd "$(dirname "$0")" && pwd)"
main_dir="$(dirname "$(git -C "$script_dir" rev-parse --path-format=absolute --git-common-dir)")"
cd "$main_dir"

state="$(gh pr view "$pr" --json state -q .state)"
branch="$(gh pr view "$pr" --json headRefName -q .headRefName)"
if [[ "$state" != "MERGED" ]]; then
  echo "PR #$pr не слит (состояние: $state) — убирать рано" >&2
  exit 1
fi

tree="$(git worktree list --porcelain | awk -v b="refs/heads/$branch" '$1=="worktree"{w=$2} $1=="branch" && $2==b{print w}')"

if [[ -n "$tree" && -d "$tree" ]]; then
  # Удаление рабочей копии молча стирает игнорируемые файлы: состояние Terraform,
  # результаты прогона. Ссылка на .env не в счёт — сам файл лежит в основном каталоге.
  leftovers="$(git -C "$tree" status --short --ignored | grep -v -E '^!! \.env$' || true)"
  if [[ -n "$leftovers" && "$discard" != "--discard-ignored" ]]; then
    echo "в рабочей копии $tree есть то, чего нет в git:" >&2
    echo "$leftovers" | sed 's/^/  /' >&2
    echo "перенеси нужное и запусти снова с --discard-ignored" >&2
    exit 1
  fi
  git worktree remove --force "$tree"
  echo "рабочая копия удалена: $tree"
fi

if git show-ref --verify --quiet "refs/heads/$branch"; then
  git branch -D "$branch" >/dev/null
  echo "локальная ветка удалена: $branch"
fi
if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
  git push --quiet origin --delete "$branch"
  echo "ветка в origin удалена: $branch"
fi

if [[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]]; then
  git pull --quiet --ff-only origin main
  echo "main обновлён: $(git log --oneline -1)"
else
  echo "основной каталог не на main — обнови его сам" >&2
fi
