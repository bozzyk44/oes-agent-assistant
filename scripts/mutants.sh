#!/bin/sh
# Прогон мутантов: `make mutants`. Каждый мутант — сервис с одной отключённой защитой;
# прогон эталонных вопросов идёт по нему так же, как `make eval` по сервису.
#
# MUTANT_MODE=stub (по умолчанию) — послушная заглушка модели, бесплатно и без сети наружу.
# MUTANT_MODE=model — настоящая модель из .env: платный прогон, только с подтверждения.
# MUTANTS — имена через пробел; по умолчанию все, контроль первым.
#
# Работает в своём проекте compose со своим томом базы: роль с обходом построчной защиты
# создаётся только в нём, запущенный рядом `make run` не задевается. Том удаляется
# в начале и в конце.
set -eu

: "${MUTANTS_PROJECT:=oes-assistant-mutants}"
: "${MUTANT_MODE:=stub}"
compose="docker compose -p $MUTANTS_PROJECT -f compose.yaml -f compose.mutants.yaml --profile mutants"

case "$MUTANT_MODE" in
    stub)
        export MUTANT_MODEL_BASE_URL="http://obedient:8002/v1"
        export MUTANT_MODEL_API_KEY="obedient"
        export MUTANT_MODEL_NAME="obedient"
        # Заглушка отвечает только на вопросы с атакой: один проход, без повторов.
        export EVAL_ATTEMPTS=1
        ;;
    model) : "${EVAL_ATTEMPTS:=1}"; export EVAL_ATTEMPTS ;;
    *) echo "mutants: MUTANT_MODE — stub или model" >&2; exit 2 ;;
esac
export MUTANT_MODE

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        $compose logs --no-color --tail 40 mutant mutant-role obedient >&2 || true
    fi
    $compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT INT TERM

$compose down --volumes --remove-orphans >/dev/null 2>&1 || true
$compose build --quiet
names=${MUTANTS:-$($compose run --rm --no-deps mutant-table python -m tests.mutants names)}
case " $names " in
    *" control "*) ;;
    # Без контроля провал мутанта нечем отличить от провала, который был бы и без него.
    *) names="control $names" ;;
esac

if [ "$MUTANT_MODE" = stub ]; then
    $compose up --detach --wait obedient
fi
for name in $names; do
    printf '\n== мутант %s (%s)\n' "$name" "$MUTANT_MODE"
    mkdir -p "eval/mutants/$MUTANT_MODE/$name"
    MUTANT=$name $compose up --detach --wait --force-recreate mutant
    MUTANT=$name $compose run --rm --no-deps --user "$(id -u):$(id -g)" evalrun
done
$compose run --rm --no-deps --user "$(id -u):$(id -g)" mutant-table >/dev/null
printf '\nmutants: таблица — eval/mutants/mutants.md\n'
