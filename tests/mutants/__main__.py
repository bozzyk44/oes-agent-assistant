"""Мутанты в контейнерах: `python -m tests.mutants <команда>`.

    serve <мутант>        сервис с подменами мутанта на порту 8000
    obedient              послушная модель по HTTP на порту 8002
    role                  роль с обходом построчной защиты в базе проекта мутантов
    names                 имена мутантов по порядку таблицы
    table <каталог>       таблица «мутант, ожидаемые провалы, фактические» из прогонов

Запускается сценарием `scripts/mutants.sh` в отдельном проекте compose; см. `make mutants`.
"""

import os
import sys
from pathlib import Path

from tests.mutants import BY_NAME, MUTANTS, apply_forever


def serve(name: str) -> int:
    import uvicorn

    mutant = BY_NAME[name]
    if mutant.bypass:
        # Адрес роли с обходом есть только у процесса мутанта; сервису он не передаётся.
        os.environ["DATABASE_URL"] = os.environ["BYPASS_DATABASE_URL"]
    apply_forever(mutant)
    from assistant.api.app import create_app

    print(f"мутант {mutant.name}: {mutant.disabled}", flush=True)
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)  # noqa: S104 — сеть compose
    return 0


def obedient() -> int:
    import uvicorn

    from tests.mutants.obedient import ObedientModel, http_app, load_attacks

    questions = Path(os.environ.get("QUESTIONS", "/data/questions.jsonl"))
    model = ObedientModel(load_attacks(questions))
    uvicorn.run(http_app(model), host="0.0.0.0", port=8002)  # noqa: S104 — сеть compose
    return 0


def role() -> int:
    from sqlalchemy import create_engine

    from assistant.ingest.bootstrap import read_account
    from assistant.ingest.startup import connect_admin
    from tests.mutants.role import ensure_bypass_role

    account = read_account("BYPASS_DATABASE_URL", os.environ["BYPASS_DATABASE_URL"])
    owner = create_engine(os.environ["DATABASE_OWNER_URL"])
    with connect_admin(
        os.environ.get("POSTGRES_HOST", "postgres"),
        int(os.environ.get("POSTGRES_PORT", "5432")),
        os.environ["POSTGRES_PASSWORD"],
    ) as admin:
        ensure_bypass_role(admin, owner, account)
    owner.dispose()
    print(f"роль с обходом построчной защиты на месте: {account.role}")
    return 0


def main(argv: list[str]) -> int:
    match argv:
        case ["serve", name] if name in BY_NAME:
            return serve(name)
        case ["obedient"]:
            return obedient()
        case ["role"]:
            return role()
        case ["names"]:
            print(" ".join(mutant.name for mutant in MUTANTS))
            return 0
        case ["table", root]:
            from tests.mutants.table import write_table

            print(write_table(Path(root), Path(os.environ.get("DATA_DIR", "/data"))))
            return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
