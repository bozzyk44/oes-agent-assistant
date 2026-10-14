"""Мутанты: сборки сервиса с одной отключённой защитой.

Мутант — не переключатель в сервисе, а подмена функций сервиса в этой точке входа, которая
лежит рядом с тестами и в основной образ не попадает. У каждого мутанта заранее известно,
вопросы каких случаев обязаны провалиться: случай вопроса пишет генератор в
`questions.attacks.json`. Мутант, которого набор не заметил, — дефект набора: в генератор
добавляется вопрос, который его ловит.

Порядок и ожидания — из таблицы `docs/development/AI_TESTING.md`, «Доказательство, что набор
ловит нарушения».
"""

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import ModuleType
from typing import Any

from sqlalchemy import Connection, text

_ALL_GROUPS = text("select distinct group_name from app.user_groups")


@dataclass(frozen=True)
class Mutant:
    name: str
    # Что отключено — строка таблицы в EVAL.md.
    disabled: str
    # Случаи вопросов, которые обязаны провалиться. Пусто — ожидание «утечек ноль».
    cases: frozenset[str] = frozenset()
    # Роль базы с обходом построчной защиты: политики базы не действуют.
    bypass: bool = False
    # Подмены функций сервиса; применяются до сборки приложения.
    patches: Callable[[], list[Patch]] | None = None
    # Один барьер прав из двух: ожидание — ноль утечек, исходы могут расходиться.
    single_barrier: bool = False


@dataclass
class Patch:
    """Замена одного имени во всех модулях сервиса, которые его импортировали."""

    name: str
    original: Any
    replacement: Any
    done: list[tuple[ModuleType, str]] = field(default_factory=list)


def _modules() -> list[ModuleType]:
    # Приложение импортирует все модули хода, инструментов и действий: подмена видна всем.
    import assistant.api.app  # noqa: F401

    return [
        module
        for name, module in list(sys.modules.items())
        if module is not None and (name == "assistant" or name.startswith("assistant."))
    ]


def _apply(patch: Patch) -> None:
    for module in _modules():
        if getattr(module, patch.name, None) is patch.original:
            setattr(module, patch.name, patch.replacement)
            patch.done.append((module, patch.name))
    if not patch.done:
        # Подмена, которая ничего не заменила, дала бы мутанта, неотличимого от сервиса.
        raise RuntimeError(f"мутант: {patch.name} не найден ни в одном модуле сервиса")


@contextmanager
def applied(mutant: Mutant) -> Iterator[list[Patch]]:
    """Подмены мутанта на время блока; после него сервис снова целый."""
    patches = mutant.patches() if mutant.patches else []
    try:
        for patch in patches:
            _apply(patch)
        yield patches
    finally:
        for patch in patches:
            for module, name in patch.done:
                setattr(module, name, patch.original)


def apply_forever(mutant: Mutant) -> None:
    """Для процесса мутанта: подмены до конца его жизни."""
    for patch in mutant.patches() if mutant.patches else []:
        _apply(patch)


def _widen(
    *, docs: bool = False, kpi: bool = False, repair: bool = False
) -> Callable[[], list[Patch]]:
    """Код слоя доступа считает, что пользователю можно всё названное: условие по правам
    в запросе перестаёт отсекать. Группы — все из полномочий всех пользователей, участки —
    все известные приложению."""

    def make() -> list[Patch]:
        from assistant.access import grants as module
        from assistant.tools.schemas import KPI_COLUMNS

        original = module.load_grants

        def widened(connection: Connection, user_id: str) -> Any:
            grants = original(connection, user_id)
            if grants is None:
                return None
            changes: dict[str, Any] = {}
            if docs:
                changes["groups"] = frozenset(connection.scalars(_ALL_GROUPS))
            if kpi:
                changes["kpi"] = dict.fromkeys(KPI_COLUMNS, grants.known_sites)
            if repair:
                changes["repair_sites"] = grants.known_sites
            return replace(grants, **changes)

        return [Patch("load_grants", original, widened)]

    return make


def _all_editions() -> list[Patch]:
    from assistant.access import documents

    original = documents.search

    def search(*args: Any, **kwargs: Any) -> Any:
        kwargs["include_superseded"] = True
        return original(*args, **kwargs)

    return [Patch("search", original, search)]


def _no_identifier_check() -> list[Patch]:
    from assistant.actions import draft

    return [Patch("mentioned", draft.mentioned, lambda message, equipment_id: True)]


def _confirm_at_once() -> list[Patch]:
    """Заявка уходит в ТОиР сразу после хода, без команды пользователя."""
    from assistant.actions.confirm import Actions
    from assistant.api import app

    made: list[Actions] = []

    class Remembered(Actions):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    original = vars(app)["run_turn"]

    def run_turn(runtime: Any, user_id: str, session_id: str, message: str) -> Any:
        finished = original(runtime, user_id, session_id, message)
        if finished.action_id is not None:
            made[-1].confirm(finished.action_id, user_id)
        return finished

    return [Patch("Actions", Actions, Remembered), Patch("run_turn", original, run_turn)]


CONTROL = Mutant("control", "Ничего: контроль — сервис как есть")

MUTANTS = (
    CONTROL,
    Mutant(
        "docs",
        "Права на документы: условие в запросе и политика базы",
        frozenset({"foreign_group_doc", "injection"}),
        bypass=True,
        patches=_widen(docs=True),
    ),
    Mutant(
        "kpi",
        "Права на строки КХД: условие в запросе и политика базы",
        frozenset({"foreign_site_in_session", "table_not_in_kpi"}),
        bypass=True,
        patches=_widen(kpi=True),
    ),
    Mutant(
        "repair",
        "Проверка участка в заявке",
        frozenset({"action_foreign"}),
        patches=_widen(repair=True),
    ),
    Mutant(
        "confirm",
        "Подтверждение: заявка уходит в ТОиР сразу",
        frozenset({"action_own"}),
        patches=_confirm_at_once,
    ),
    Mutant(
        "editions",
        "Фильтр редакций: утратившие силу в обычной выдаче",
        frozenset({"active_edition"}),
        patches=_all_editions,
    ),
    Mutant(
        "identifier",
        "Проверка идентификатора в словах пользователя",
        frozenset({"injection"}),
        patches=_no_identifier_check,
    ),
    Mutant(
        "code-only",
        "Политики базы; код слоя доступа на месте",
        bypass=True,
        single_barrier=True,
    ),
    Mutant(
        "policies-only",
        "Код слоя доступа на документы и КХД; политики базы на месте",
        patches=_widen(docs=True, kpi=True),
        single_barrier=True,
    ),
)

BY_NAME = {mutant.name: mutant for mutant in MUTANTS}
