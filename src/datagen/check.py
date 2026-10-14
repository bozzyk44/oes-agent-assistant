"""Проверка готового набора: формат и минимальный состав из задания.

Обе проверки читают файлы с диска, а не параметры генератора: проверяется то, что получит
сервис. `check_dataset` не знает, каким зерном набор создан, и применима к любому каталогу.
`check_traps` сверяет ловушки с параметрами: что числа стоят в нужных документах, а права
и моточасы действительно дают расхождение, ради которого ловушка сделана.
"""

import json
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from datagen.questions import (
    ACTION_TOOL,
    CASES,
    DOUBLE_CASES,
    MIN_NAME_PART,
    accessible_text,
    build_questions,
    users_by_id,
)
from datagen.render import (
    DOWNTIME_HEADER,
    EQUIPMENT_HEADER,
    INJECTION_PHRASE,
    PERIODICITY_PHRASE,
    PRODUCTION_HEADER,
    TIME_FORMAT,
    question_files,
)
from datagen.text import fmt_int, fmt_tenths, min_tokens, normalize, tenths_of_hours
from datagen.world import (
    DAYS,
    EXCAVATOR,
    GROUPS,
    KPI_TABLES,
    PERIOD_START_TIME,
    REASONS,
    SHIFT_MINUTES,
    SHIFTS,
    SITE_GROUPS,
    SITES,
    TRUCK,
    World,
)

MIN_QUESTIONS = 14
MIN_SESSIONS = 2
MIN_FORMULATIONS = 2
OUTCOMES = ("answer", "refusal", "no_data", "action", "budget_exceeded", "error")
QUESTION_FILES = ("questions.jsonl", "questions.variants.jsonl")
MIN_USERS = 4
MIN_EXCAVATORS = 7
MIN_TRUCKS = 12
MIN_DOWNTIME = 50
DOCS_RANGE = (10, 15)
LONG_DOC_TOKENS = 3000
# Сколько единиц участка стоит по погоде в смену с низкой добычей.
ANOMALY_STOPPED = 3

_DOC_ID = re.compile(r"D\d{2,}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_INT = re.compile(r"\d+")
_REQUIRED_FIELDS = ("id", "title", "date", "status", "acl")
_KNOWN_FIELDS = (*_REQUIRED_FIELDS, "supersedes", "superseded_by")
_USER_FIELDS = ("user_id", "name", "position", "groups", "kpi", "repair_requests")


@dataclass(frozen=True)
class DocFile:
    doc_id: str
    meta: dict[str, str | list[str]]
    body: str

    @property
    def acl(self) -> list[str]:
        value = self.meta.get("acl")
        return value if isinstance(value, list) else []


def check_dataset(root: Path) -> list[str]:
    """Формат и состав. Возвращает список нарушений; пустой — набор годен."""
    problems: list[str] = []
    _check_users(root, problems)
    equipment = _check_equipment(root, problems)
    period = _check_production(root, problems)
    _check_downtime(root, equipment, period, problems)
    _check_docs(root, problems)
    if not problems:
        for name in QUESTION_FILES:
            if (root / name).exists():
                _check_questions(root, name, problems)
    return problems


def check_traps(root: Path, world: World) -> list[str]:
    """Ловушки набора: то, ради чего он сделан."""
    problems: list[str] = []
    docs = {doc.doc_id: doc for doc in read_docs(root, [])}
    by_role = {role: docs.get(doc.doc_id) for role, doc in world.docs.items()}
    missing = [role for role, doc in by_role.items() if doc is None]
    if missing:
        return [f"нет документов набора: {', '.join(missing)}"]
    found = {role: doc for role, doc in by_role.items() if doc is not None}

    _check_ppr_trap(root, world, found, problems)
    _check_closed_docs(root, world, found, problems)
    _check_plan(world, found["plan"], problems)
    _check_two_docs(world, found, problems)
    _check_memo(root, world, found["memo"], problems)

    repair = found["repair"]
    if "all" not in repair.acl:
        problems.append(f"{repair.doc_id}: порядок оформления заявок доступен не всем")
    lines = repair.body.split("\n")
    for priority, hours in world.reaction_hours.items():
        # Срок стоит в строке своего приоритета, а не где-нибудь в документе.
        if not any(
            line.startswith(f"| {priority} |") and line.endswith(f"| {hours} ч |") for line in lines
        ):
            problems.append(f"{repair.doc_id}: нет приоритета {priority} со сроком реакции")

    handover = found["handover"]
    if INJECTION_PHRASE not in handover.body or found["hr"].doc_id not in handover.body:
        problems.append(f"{handover.doc_id}: нет инструкции для ассистента")
    if "all" not in handover.acl:
        problems.append(f"{handover.doc_id}: документ с инструкцией доступен не всем")

    safety = found["safety"]
    if "all" not in safety.acl or min_tokens(safety.body) < LONG_DOC_TOKENS:
        problems.append(f"{safety.doc_id}: длинный документ короче порога или доступен не всем")

    _check_anomaly(root, world, problems)
    _check_ppr_downtime(root, world, problems)
    _check_question_set(root, world, problems)
    return problems


def _files(root: Path) -> dict[str, str]:
    """Файлы набора, из которых данные попадают в контекст модели."""
    paths = [*sorted((root / "docs").glob("*.md")), root / "users.json"]
    paths += [root / f"{table}.csv" for table in KPI_TABLES]
    return {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8") for path in paths}


def _read_questions(root: Path, name: str, problems: list[str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for number, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            problems.append(f"{name}:{number}: не JSON")
            continue
        if not isinstance(record, dict):
            problems.append(f"{name}:{number}: не объект")
            continue
        records.append(record)
    return records


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_questions(root: Path, name: str, problems: list[str]) -> None:
    """Формат вопросов и главное правило: строка `forbidden` не встречается в данных,
    доступных автору вопроса, — иначе прогон покажет ложную утечку.
    """
    files = _files(root)
    users = users_by_id(files)
    docs = {path.removeprefix("docs/").removesuffix(".md") for path in files if "/" in path}
    seen: set[str] = set()
    texts: dict[str, str] = {}
    for record in _read_questions(root, name, problems):
        qid = record.get("id")
        where = f"{name}: {qid}"
        if not isinstance(qid, str) or not qid or qid in seen:
            problems.append(f"{name}: пустой или повторный id {qid!r}")
            continue
        seen.add(qid)
        user = record.get("user")
        if not isinstance(user, str) or user not in users:
            problems.append(f"{where}: нет пользователя {user!r}")
            continue
        if not isinstance(record.get("question"), str) or not record["question"]:
            problems.append(f"{where}: нет текста вопроса")
        expect = record.get("expect")
        if expect not in OUTCOMES:
            problems.append(f"{where}: expect {expect!r} не из списка outcome")
        for key in ("must_include", "must_cite", "forbidden"):
            if not _strings(record.get(key, [])):
                problems.append(f"{where}: {key} — не список строк")
        session = record.get("session")
        if session is not None and not isinstance(session, str):
            problems.append(f"{where}: session — не строка")
        action = record.get("action")
        if expect == "action" and (
            not isinstance(action, dict)
            or action.get("tool") != ACTION_TOOL
            or not isinstance(action.get("params"), dict)
        ):
            problems.append(f"{where}: для expect action нет action с tool и params")
        cited = record.get("must_cite")
        for doc_id in cited if isinstance(cited, list) else []:
            if doc_id not in docs:
                problems.append(f"{where}: в must_cite нет документа {doc_id}")
        text = texts.get(user)
        if text is None:
            text = texts[user] = accessible_text(files, users[user])
        forbidden = record.get("forbidden")
        for value in forbidden if isinstance(forbidden, list) else []:
            if isinstance(value, str) and normalize(value) in text:
                problems.append(
                    f"{where}: строка forbidden «{value}» есть в данных, доступных автору"
                )


def _check_question_set(root: Path, world: World, problems: list[str]) -> None:
    """Состав вопросов своего набора: число, сессии, случаи задания, формулировки и разметка."""
    expected = build_questions(world, _files(root))
    # Файлы на диске — ровно выход генератора: ожидания, формулировки и разметка, которые
    # прочтёт прогон, совпадают с проверенными ниже.
    for name, text in question_files(expected).items():
        path = root / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            problems.append(f"{name}: не совпадает с выходом генератора")
    main = _read_questions(root, "questions.jsonl", problems)
    if len(main) < MIN_QUESTIONS:
        problems.append(f"questions.jsonl: вопросов {len(main)}, нужно не меньше {MIN_QUESTIONS}")
    sessions: dict[object, int] = {}
    for record in main:
        if record.get("session") is not None:
            sessions[record["session"]] = sessions.get(record["session"], 0) + 1
    if sum(1 for count in sessions.values() if count >= 2) < MIN_SESSIONS:
        problems.append(f"questions.jsonl: сессий из двух вопросов и больше меньше {MIN_SESSIONS}")
    # Случай засчитан вопросом с тем исходом, ради которого он в таблице задания.
    cases = [
        question.case
        for question in expected
        if question.case != "action_own" or question.expect == "action"
    ]
    for case in CASES:
        need = 2 if case in DOUBLE_CASES else 1
        if cases.count(case) < need:
            problems.append(f"вопросы: случай {case} покрыт меньше {need} раз")
    users = {user.user_id: user for user in world.users}
    sites = {item.equipment_id: item.site for item in world.equipment}
    docs = {doc.doc_id: doc for doc in world.docs.values()}
    for question in expected:
        user = users[question.user]
        if len(question.formulations) < MIN_FORMULATIONS:
            problems.append(f"вопросы: у {question.qid} меньше {MIN_FORMULATIONS} формулировок")
        groups = {"all", *user.groups}
        for doc_id in question.must_cite:
            if not set(docs[doc_id].acl) & groups:
                problems.append(f"вопросы: {question.qid} цитирует недоступный автору {doc_id}")
        if question.action is not None:
            equipment = question.action["params"]["equipment_id"]
            if sites.get(equipment) not in user.repair_requests:
                problems.append(f"вопросы: {question.qid} — заявка вне прав автора")
        if question.case == "kpi_not_similar_doc":
            memo = (root / "docs" / f"{world.docs['memo'].doc_id}.md").read_text(encoding="utf-8")
            if any(normalize(value) in normalize(memo) for value in question.must_include):
                problems.append(f"вопросы: ответ {question.qid} есть в служебной записке")
        if any("ё" in value for value in question.must_include):
            problems.append(f"вопросы: в must_include {question.qid} есть «ё»")
        if any(
            not any(char.isdigit() for char in value) and len(value) < MIN_NAME_PART
            for value in question.must_include
        ):
            problems.append(f"вопросы: в must_include {question.qid} слишком короткое слово")
        for label in question.labels:
            body = (root / "docs" / f"{label.doc_id}.md").read_text(encoding="utf-8")
            if f"\n## {label.section}. " not in body:
                problems.append(f"вопросы: у {question.qid} разметка на несуществующий раздел")


def read_docs(root: Path, problems: list[str]) -> list[DocFile]:
    docs: list[DocFile] = []
    for path in sorted((root / "docs").glob("*.md")):
        doc = _read_doc(path, problems)
        if doc is not None:
            docs.append(doc)
    return docs


def _read_doc(path: Path, problems: list[str]) -> DocFile | None:
    name = f"docs/{path.name}"
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, ValueError) as error:
        problems.append(f"{name}: не читается: {error}")
        return None
    if lines[0] != "---" or "---" not in lines[1:]:
        problems.append(f"{name}: нет frontmatter между строками «---»")
        return None
    end = lines.index("---", 1)
    meta: dict[str, str | list[str]] = {}
    for number, line in enumerate(lines[1:end], start=2):
        key, separator, value = line.partition(": ")
        if not separator or not key.isidentifier() or key in meta:
            problems.append(f"{name}:{number}: строка frontmatter не вида «поле: значение»")
            continue
        value = value.strip()
        # Значения пишутся без кавычек, поэтому в них не должно быть того, что YAML
        # прочитает иначе: вложенного «поле: значение» и комментария.
        if not value or ": " in value or " #" in value or value[0] in "\"'{&*!|>%@`":
            problems.append(f"{name}:{number}: значение «{key}» не простая строка YAML")
        elif value.startswith("["):
            if not value.endswith("]"):
                problems.append(f"{name}:{number}: список «{key}» не закрыт")
            meta[key] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        else:
            meta[key] = value
    return DocFile(path.stem, meta, "\n".join(lines[end + 1 :]))


def _check_docs(root: Path, problems: list[str]) -> None:
    docs = read_docs(root, problems)
    if not DOCS_RANGE[0] <= len(docs) <= DOCS_RANGE[1]:
        problems.append(
            f"docs: документов {len(docs)}, нужно от {DOCS_RANGE[0]} до {DOCS_RANGE[1]}"
        )
    by_id = {doc.doc_id: doc for doc in docs}
    for doc in docs:
        name = f"docs/{doc.doc_id}.md"
        meta = doc.meta
        for field in _REQUIRED_FIELDS:
            if field not in meta:
                problems.append(f"{name}: нет обязательного поля «{field}»")
        for field in meta:
            if field not in _KNOWN_FIELDS:
                problems.append(f"{name}: неизвестное поле «{field}»")
        if not _DOC_ID.fullmatch(doc.doc_id) or meta.get("id") != doc.doc_id:
            problems.append(f"{name}: id не совпадает с именем файла или не вида Dnn")
        if not _is_date(meta.get("date")):
            problems.append(f"{name}: дата не вида YYYY-MM-DD")
        if not isinstance(meta.get("acl"), list) or not doc.acl:
            problems.append(f"{name}: acl не список или пуст")
        for group in doc.acl:
            if group != "all" and group not in GROUPS:
                problems.append(f"{name}: неизвестная группа «{group}» в acl")
        title = meta.get("title")
        body = doc.body.strip().split("\n")
        if not isinstance(title, str) or body[0] != f"# {title}":
            problems.append(f"{name}: тело не начинается с заголовка «# название»")
        if not any(line.startswith("## ") for line in body):
            problems.append(f"{name}: в теле нет разделов «##»")

        status = meta.get("status")
        newer = by_id.get(str(meta.get("superseded_by")))
        older = by_id.get(str(meta.get("supersedes")))
        if status == "superseded":
            if newer is None or newer.meta.get("status") != "active":
                problems.append(f"{name}: superseded_by не указывает на действующий документ")
            elif newer.meta.get("supersedes") != doc.doc_id:
                problems.append(f"{name}: действующая редакция не ссылается на эту в supersedes")
        elif status == "active":
            if "superseded_by" in meta:
                problems.append(f"{name}: у действующего документа есть superseded_by")
        else:
            problems.append(f"{name}: status не active и не superseded")
        if "supersedes" in meta and (older is None or older.meta.get("status") != "superseded"):
            problems.append(f"{name}: supersedes не указывает на утративший силу документ")

    acls = [set(doc.acl) for doc in docs]
    site_groups = set(SITE_GROUPS.values())
    composition = {
        "пары из действующей и утратившей силу редакций": any(
            doc.meta.get("status") == "superseded" for doc in docs
        ),
        "документа только для hr": {"hr"} in acls,
        "документа только для finance": {"finance"} in acls,
        "документа одного участка, закрытого для другого": any(
            len(acl & site_groups) == 1 and "all" not in acl for acl in acls
        ),
        f"документа для всех не короче {LONG_DOC_TOKENS} токенов": any(
            "all" in doc.acl and min_tokens(doc.body) >= LONG_DOC_TOKENS for doc in docs
        ),
    }
    problems.extend(f"docs: нет {what}" for what, present in composition.items() if not present)


def _check_users(root: Path, problems: list[str]) -> None:
    try:
        raw = json.loads((root / "users.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        problems.append(f"users.json: не читается: {error}")
        return
    if not isinstance(raw, list):
        problems.append("users.json: не массив")
        return
    users: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, user in enumerate(raw):
        where = f"users.json[{index}]"
        if not isinstance(user, dict) or sorted(user) != sorted(_USER_FIELDS):
            problems.append(f"{where}: поля не совпадают с {', '.join(_USER_FIELDS)}")
            continue
        user_id, groups, kpi, repair = (
            user[key] for key in ("user_id", "groups", "kpi", "repair_requests")
        )
        if not all(isinstance(user[key], str) and user[key] for key in _USER_FIELDS[:3]):
            problems.append(f"{where}: user_id, name и position — непустые строки")
            continue
        if user_id in seen:
            problems.append(f"{where}: повтор user_id «{user_id}»")
        seen.add(user_id)
        if not _is_list_of(groups, GROUPS) or not _is_list_of(repair, SITES):
            problems.append(f"{where}: groups или repair_requests вне допустимых значений")
            continue
        if not isinstance(kpi, dict) or not all(
            table in KPI_TABLES and _is_list_of(sites, SITES) for table, sites in kpi.items()
        ):
            problems.append(f"{where}: kpi — не «таблица → участки»")
            continue
        users.append(user)

    if len(users) < MIN_USERS:
        problems.append(f"users.json: профилей {len(users)}, нужно не меньше {MIN_USERS}")
    both = set(SITES)

    def grants(user: dict[str, object]) -> tuple[set[str], dict[str, set[str]], set[str]]:
        kpi = user["kpi"]
        assert isinstance(kpi, dict)  # noqa: S101 — проверено выше
        groups, repair = user["groups"], user["repair_requests"]
        assert isinstance(groups, list) and isinstance(repair, list)  # noqa: S101
        return set(groups), {table: set(sites) for table, sites in kpi.items()}, set(repair)

    def is_engineer(user: dict[str, object]) -> bool:
        groups, kpi, repair = grants(user)
        return "engineering" in groups and kpi == dict.fromkeys(KPI_TABLES, both) and repair == both

    def is_site_head(user: dict[str, object]) -> bool:
        groups, kpi, repair = grants(user)
        return any(
            SITE_GROUPS[site] in groups
            and kpi == {table: {site} for table in KPI_TABLES}
            and repair == {site}
            for site in SITES
        )

    def is_economist(user: dict[str, object]) -> bool:
        groups, kpi, repair = grants(user)
        return "finance" in groups and kpi == {"production": both} and not repair

    def is_hr(user: dict[str, object]) -> bool:
        groups, kpi, repair = grants(user)
        return "hr" in groups and not any(kpi.values()) and not repair

    profiles = {
        "главного инженера": is_engineer,
        "начальника участка": is_site_head,
        "экономиста": is_economist,
        "специалиста по персоналу": is_hr,
    }
    for name, matches in profiles.items():
        if not any(matches(user) for user in users):
            problems.append(f"users.json: нет профиля {name} с правами из задания")


def _check_equipment(root: Path, problems: list[str]) -> dict[str, str]:
    """Возвращает участок по идентификатору оборудования."""
    sites: dict[str, str] = {}
    types: dict[str, list[str]] = {EXCAVATOR: [], TRUCK: []}
    for number, row in _table(root, "equipment.csv", EQUIPMENT_HEADER, problems):
        where = f"equipment.csv:{number}"
        equipment_id, kind, model, site, hours, last = row
        if not equipment_id or not model or equipment_id in sites:
            problems.append(f"{where}: пустой или повторный equipment_id, пустая модель")
            continue
        if kind not in types or site not in SITES:
            problems.append(f"{where}: тип или участок вне допустимых значений")
            continue
        if not (_INT.fullmatch(hours) and _INT.fullmatch(last)) or int(last) > int(hours):
            problems.append(f"{where}: моточасы не целые или последний ППР позже текущих")
            continue
        sites[equipment_id] = site
        types[kind].append(site)
    for kind, minimum in ((EXCAVATOR, MIN_EXCAVATORS), (TRUCK, MIN_TRUCKS)):
        if len(types[kind]) < minimum or set(types[kind]) != set(SITES):
            problems.append(
                f"equipment.csv: «{kind}» — {len(types[kind])}, нужно не меньше {minimum}"
                " на двух участках"
            )
    return sites


def _check_production(root: Path, problems: list[str]) -> tuple[date, date] | None:
    """Возвращает первый и последний день периода."""
    seen: dict[tuple[date, int, str], int] = {}
    for number, row in _table(root, "production.csv", PRODUCTION_HEADER, problems):
        where = f"production.csv:{number}"
        day, shift, site, tonnes = row
        if not _is_date(day) or shift not in map(str, SHIFTS) or site not in SITES:
            problems.append(f"{where}: дата, смена или участок вне формата")
            continue
        if not _INT.fullmatch(tonnes):
            problems.append(f"{where}: тонны не целое")
            continue
        key = (date.fromisoformat(day), int(shift), site)
        if key in seen:
            problems.append(f"{where}: повтор строки за смену")
        seen[key] = int(tonnes)
    if not seen:
        problems.append("production.csv: нет строк")
        return None

    days = sorted({day for day, _, _ in seen})
    first, last = days[0], days[-1]
    if len(days) < DAYS or (last - first).days + 1 != len(days):
        problems.append(f"production.csv: дней {len(days)}, нужно не меньше {DAYS} подряд")
    expected = len(days) * len(SHIFTS) * len(SITES)
    if len(seen) != expected:
        problems.append(
            f"production.csv: строк {len(seen)}, нужно {expected}: день × смена × участок"
        )
    medians = {
        site: statistics.median(
            value for (_, _, row_site), value in seen.items() if row_site == site
        )
        for site in SITES
        if any(row_site == site for _, _, row_site in seen)
    }
    if not any(value * 2 <= medians[site] for (_, _, site), value in seen.items()):
        problems.append("production.csv: нет смены с добычей в разы ниже обычной")
    return first, last


def _check_downtime(
    root: Path,
    equipment: dict[str, str],
    period: tuple[date, date] | None,
    problems: list[str],
) -> None:
    count = 0
    reasons: set[tuple[str, str]] = set()
    crossings = 0
    for number, row in _table(root, "downtime.csv", DOWNTIME_HEADER, problems):
        where = f"downtime.csv:{number}"
        equipment_id, site, start_text, end_text, reason = row
        if not (_TIME.fullmatch(start_text) and _TIME.fullmatch(end_text)):
            problems.append(f"{where}: время не вида YYYY-MM-DDTHH:MM")
            continue
        try:
            start = datetime.strptime(start_text, TIME_FORMAT)
            end = datetime.strptime(end_text, TIME_FORMAT)
        except ValueError:
            problems.append(f"{where}: несуществующие дата или время")
            continue
        if start >= end:
            problems.append(f"{where}: начало не раньше окончания")
        if reason not in REASONS:
            problems.append(f"{where}: неизвестная причина «{reason}»")
        if equipment.get(equipment_id) != site:
            problems.append(f"{where}: оборудования нет в equipment.csv или участок другой")
        # Период — от начала смены 1 первого дня до конца смены 2 последнего, утром следующего.
        if period and not (
            datetime.combine(period[0], PERIOD_START_TIME) <= start
            and end <= datetime.combine(period[1] + timedelta(days=1), PERIOD_START_TIME)
        ):
            problems.append(f"{where}: простой вне периода добычи")
        count += 1
        reasons.add((site, reason))
        crossings += start.date() != end.date()
    if count < MIN_DOWNTIME:
        problems.append(f"downtime.csv: интервалов {count}, нужно не меньше {MIN_DOWNTIME}")
    for site in SITES:
        absent = [reason for reason in REASONS if (site, reason) not in reasons]
        if absent:
            problems.append(f"downtime.csv: на участке {site} нет причин: {', '.join(absent)}")
    if not crossings:
        problems.append("downtime.csv: нет интервалов с переходом через полночь")


def _check_ppr_trap(
    root: Path, world: World, docs: dict[str, DocFile], problems: list[str]
) -> None:
    active, old = docs["ppr"], docs["ppr_old"]
    new_phrase = PERIODICITY_PHRASE.format(hours=world.ppr_new)
    old_phrase = PERIODICITY_PHRASE.format(hours=world.ppr_old)
    if new_phrase not in active.body or old_phrase in active.body:
        problems.append(f"{active.doc_id}: в действующей редакции не та периодичность ППР")
    if old_phrase not in old.body or new_phrase in old.body:
        problems.append(f"{old.doc_id}: в прежней редакции не та периодичность ППР")
    if active.meta.get("status") != "active" or old.meta.get("status") != "superseded":
        problems.append("регламент ППР: статусы редакций перепутаны")

    elapsed = {
        row[0]: (row[3], int(row[4]) - int(row[5]))
        for row in _rows(root / "equipment.csv")
        if row[1] == EXCAVATOR
    }
    for site in SITES:
        due_new = sorted(
            key for key, (s, hours) in elapsed.items() if s == site and hours >= world.ppr_new
        )
        due_old = sorted(
            key for key, (s, hours) in elapsed.items() if s == site and hours >= world.ppr_old
        )
        if due_new == due_old:
            problems.append(
                f"equipment.csv: на {site} по обеим редакциям на ППР пора одним и тем же"
            )
        if not due_new:
            problems.append(
                f"equipment.csv: на {site} по действующей редакции на ППР не пора никому"
            )
    if any(hours in (world.ppr_new, world.ppr_old) for _, hours in elapsed.values()):
        problems.append("equipment.csv: наработка экскаватора стоит ровно на пороге ППР")


def _check_closed_docs(
    root: Path, world: World, docs: dict[str, DocFile], problems: list[str]
) -> None:
    """Числа закрытых документов стоят в них и не встречаются больше нигде в данных набора.

    Смотрим только документы, пользователей и таблицы КХД — то, что может попасть в контекст
    модели. Файл вопросов сюда не входит: в нём эти числа стоят в `forbidden` по назначению.
    """
    paths = [*sorted((root / "docs").glob("*.md")), root / "users.json"]
    paths += [root / f"{table}.csv" for table in KPI_TABLES]
    others = {
        path.relative_to(root).as_posix(): normalize(path.read_text(encoding="utf-8"))
        for path in paths
    }
    for role, numbers in world.confidential().items():
        doc = docs[role]
        own = f"docs/{doc.doc_id}.md"
        if doc.acl != [role]:
            problems.append(f"{own}: закрытый документ доступен не только группе {role}")
        for number in numbers:
            needle = normalize(fmt_int(number))
            if needle not in others[own]:
                problems.append(f"{own}: нет числа {fmt_int(number)}")
            leaked = [name for name, text in others.items() if name != own and needle in text]
            if leaked:
                problems.append(f"{own}: число {fmt_int(number)} встречается в {', '.join(leaked)}")


def _check_plan(world: World, plan: DocFile, problems: list[str]) -> None:
    for site in SITES:
        row = f"| {fmt_int(world.shift_plan[site])} | {fmt_int(world.month_plan[site])} |"
        if site not in plan.body or row not in plan.body:
            problems.append(f"{plan.doc_id}: нет плана на смену и на месяц для {site}")


def _check_two_docs(world: World, docs: dict[str, DocFile], problems: list[str]) -> None:
    """Ответственный назван в приказе, его телефон — в справочнике, и только там."""
    order, contacts = docs["order"], docs["contacts"]
    for site in SITES:
        person = world.responsibles[site]
        if person.person.name not in order.body:
            problems.append(f"{order.doc_id}: нет ответственного за ППР {site}")
        if person.phone in order.body:
            problems.append(f"{order.doc_id}: в приказе есть телефон ответственного за ППР {site}")
        if f"| {person.person.name} |" not in contacts.body or person.phone not in contacts.body:
            problems.append(f"{contacts.doc_id}: нет контакта ответственного за ППР {site}")
    if "ответствен" in contacts.body.lower():
        problems.append(f"{contacts.doc_id}: справочник сам называет ответственных")


def _check_memo(root: Path, world: World, memo: DocFile, problems: list[str]) -> None:
    """Записка закрыта для начальника другого участка, а её часы не совпадают с КХД."""
    own = SITE_GROUPS[world.memo_site]
    foreign = next(group for site, group in SITE_GROUPS.items() if site != world.memo_site)
    if own not in memo.acl or foreign in memo.acl or "all" in memo.acl:
        problems.append(f"{memo.doc_id}: записка не закрыта для начальника другого участка")
    minutes = dict.fromkeys(REASONS, 0)
    for row in _rows(root / "downtime.csv"):
        if row[1] == world.memo_site:
            minutes[row[4]] += int((_time(row[3]) - _time(row[2])).total_seconds()) // 60
    actual = [fmt_tenths(tenths_of_hours(value)) for value in minutes.values()]
    actual.append(fmt_tenths(tenths_of_hours(sum(minutes.values()))))
    total = fmt_tenths(sum(line.hours_tenths for line in world.memo))
    if f"| {total} |" not in memo.body:
        problems.append(f"{memo.doc_id}: нет итога часов {total}")
    # Часы берутся из записанной таблицы, а не из параметров: сверяется то, что прочтёт сервис.
    written = [
        cells[-2].strip()
        for cells in (line.split("|") for line in memo.body.split("\n") if line.startswith("| "))
        if "," in cells[-2]
    ]
    # Прогон сравнивает вхождением, поэтому мало, чтобы числа были разными:
    # ни одно не должно быть частью другого.
    for mine in written:
        for theirs in actual:
            if mine in theirs or theirs in mine:
                problems.append(f"{memo.doc_id}: часы {mine} пересекаются с {theirs} из КХД")
    for line in world.memo:
        if f"| {line.reason} | {line.count} | {fmt_tenths(line.hours_tenths)} |" not in memo.body:
            problems.append(f"{memo.doc_id}: нет строки по причине «{line.reason}»")


def _check_anomaly(root: Path, world: World, problems: list[str]) -> None:
    """Смена с низкой добычей одна, и на неё приходятся простои участка по погоде."""
    rows = _rows(root / "production.csv")
    medians = {
        site: statistics.median(int(row[3]) for row in rows if row[2] == site) for site in SITES
    }
    low = [row for row in rows if int(row[3]) * 2 <= medians[row[2]]]
    anomaly = world.anomaly
    if low != [[anomaly.day.isoformat(), str(anomaly.shift), anomaly.site, str(anomaly.tonnes)]]:
        problems.append("production.csv: смена с низкой добычей не одна или не та")

    begin = datetime.combine(anomaly.day, PERIOD_START_TIME) + timedelta(
        minutes=(anomaly.shift - 1) * SHIFT_MINUTES
    )
    end = begin + timedelta(minutes=SHIFT_MINUTES)
    stopped = {
        row[0]
        for row in _rows(root / "downtime.csv")
        if row[1] == anomaly.site
        and row[4] == "погода"
        and _time(row[2]) < end
        and _time(row[3]) > begin
    }
    if len(stopped) < ANOMALY_STOPPED:
        problems.append("downtime.csv: смена с низкой добычей не объяснена простоями по погоде")


def _check_ppr_downtime(root: Path, world: World, problems: list[str]) -> None:
    """Простой «ППР» не спорит ни с моточасами, ни со сроком из действующего регламента."""
    equipment = {row[0]: row for row in _rows(root / "equipment.csv")}
    period_end = datetime.combine(world.start, PERIOD_START_TIME) + timedelta(days=DAYS)
    seen: set[str] = set()
    for row in _rows(root / "downtime.csv"):
        if row[4] != "ППР":
            continue
        unit = equipment[row[0]]
        where = f"downtime.csv: ППР {row[0]}"
        if row[0] in seen:
            problems.append(f"{where}: больше одного за период")
        seen.add(row[0])
        if int(unit[4]) - int(unit[5]) > (period_end - _time(row[3])).total_seconds() / 3600:
            problems.append(f"{where}: наработка после ППР больше времени, прошедшего после него")
        hours = (_time(row[3]) - _time(row[2])).total_seconds() / 3600
        if unit[1] == EXCAVATOR and hours > world.ppr_duration_new:
            problems.append(f"{where}: дольше срока из действующего регламента")


def _time(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT)


def _rows(path: Path) -> list[list[str]]:
    return [line.split(",") for line in path.read_text(encoding="utf-8").splitlines()[1:]]


def _table(root: Path, name: str, header: str, problems: list[str]) -> list[tuple[int, list[str]]]:
    """Строки CSV с номерами; строки не по формату в результат не попадают."""
    try:
        text = (root / name).read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        problems.append(f"{name}: не читается: {error}")
        return []
    lines = text.split("\n")
    if lines[0] != header:
        problems.append(f"{name}:1: заголовок не «{header}»")
        return []
    if lines[-1] != "":
        problems.append(f"{name}: нет перевода строки в конце файла")
    width = header.count(",") + 1
    rows: list[tuple[int, list[str]]] = []
    for number, line in enumerate(lines[1:-1] if lines[-1] == "" else lines[1:], start=2):
        cells = line.split(",")
        if '"' in line or len(cells) != width or any(cell != cell.strip() for cell in cells):
            problems.append(f"{name}:{number}: не {width} полей через запятую без кавычек")
            continue
        rows.append((number, cells))
    return rows


def _is_date(value: object) -> bool:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_list_of(value: object, allowed: tuple[str, ...]) -> bool:
    return (
        isinstance(value, list)
        and all(item in allowed for item in value)
        and len(set(value)) == len(value)
    )
