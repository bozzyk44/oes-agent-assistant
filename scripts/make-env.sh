#!/bin/sh
# Создаёт .env из .env.example, заменяя каждую метку __GENERATE__ своим случайным
# значением. Если .env уже есть, дописывает только недостающие переменные и ничего
# не меняет в существующих. Значения в вывод не попадают — только имена.
set -eu

example=".env.example"
target=".env"
marker="__GENERATE__"

[ -f "$example" ] || { echo "make-env: нет файла $example" >&2; exit 1; }

secret() {
    od -An -tx1 -N24 /dev/urandom | tr -d ' \n'
}

# Печатает строку образца с подставленным секретом; в значении секрета только [0-9a-f].
fill() {
    case "$1" in
        *"$marker"*) printf '%s\n' "$1" | sed "s/$marker/$(secret)/" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

if [ ! -e "$target" ]; then
    umask 077
    while IFS= read -r line || [ -n "$line" ]; do
        fill "$line"
    done < "$example" > "$target"
    echo "make-env: создан $target со случайными секретами"
    exit 0
fi

added=""
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        [A-Za-z_]*=*) ;;
        *) continue ;;
    esac
    name=${line%%=*}
    if ! grep -q "^${name}=" "$target"; then
        if [ -z "$added" ]; then
            # Дозапись, а не замена файла: .env в рабочей копии может быть ссылкой.
            printf '\n# Добавлено make-env из %s\n' "$example" >> "$target"
        fi
        fill "$line" >> "$target"
        added="$added $name"
    fi
done < "$example"

if [ -n "$added" ]; then
    echo "make-env: в $target добавлены переменные:$added"
fi
