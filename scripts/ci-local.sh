#!/bin/sh
# Локальный CI — основной способ проверки в этом проекте: `make test` и `make smoke`
# на чистой копии текущего коммита. Незакоммиченные правки и файлы вне git в проверку
# не входят: проверяется ровно то, что уйдёт в PR.
#
# REPORT=1 — записать итог статусом коммита в GitHub (нужны gh и запушенный коммит).
set -eu

root=$(git rev-parse --show-toplevel)
sha=$(git -C "$root" rev-parse HEAD)
short=$(git -C "$root" rev-parse --short HEAD)

if [ -n "$(git -C "$root" status --porcelain)" ]; then
    echo "ci: в рабочей копии есть незакоммиченные изменения — проверяется коммит $short без них" >&2
fi

# Копия лежит внутри репозитория, а не в системном каталоге временных файлов:
# Docker на macOS (Colima, часть настроек Docker Desktop) видит только домашний каталог,
# и монтирования из /tmp молча оказались бы пустыми.
work="$root/.ci/$short"
cleanup() {
    status=$?
    rm -rf "$root/.ci"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

rm -rf "$root/.ci"
mkdir -p "$work"
git -C "$root" archive "$sha" | tar -x -C "$work"

echo "ci: коммит $short, копия в $work"
status=0
# Свой проект compose и случайный порт: запущенный рядом `make run` не задевается.
(cd "$work" && PROJECT=oes-assistant-ci SERVICE_PORT=0 make test smoke) || status=$?

if [ "$status" -eq 0 ]; then
    state=success
    echo "ci: коммит $short — пройдено"
else
    state=failure
    echo "ci: коммит $short — ПРОВАЛ (код $status)" >&2
fi

if [ "${REPORT:-}" = "1" ]; then
    repo=$(cd "$root" && gh repo view --json nameWithOwner --jq .nameWithOwner)
    gh api --silent --method POST "repos/$repo/statuses/$sha" \
        -f state="$state" \
        -f context="ci/local" \
        -f description="make test и make smoke на машине разработчика"
    echo "ci: статус $state записан для $short в $repo"
fi

exit "$status"
