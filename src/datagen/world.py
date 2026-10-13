"""Параметры набора: всё, что зависит от зерна. Файлы пишет `render`, проверяет `check`.

Тексты документов лежат шаблонами в `templates/`; числа, права, идентификаторы и связи
редакций задаются здесь, из одного генератора случайных чисел.
"""

import calendar
import random
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from datagen.text import fmt_tenths, tenths_of_hours

DEFAULT_SEED = 20260901
# Второй набор: на нём проверяется, что код и тесты не привязаны к содержимому data/.
SECOND_SEED = 7

SITES = ("K1", "K2")
SITE_NAMES = {"K1": "Карьер-1", "K2": "Карьер-2"}
SITE_GROUPS = {"K1": "site-k1", "K2": "site-k2"}
GROUPS = ("engineering", "site-k1", "site-k2", "finance", "hr")
KPI_TABLES = ("production", "downtime", "equipment")
REASONS = ("ремонт", "ППР", "погода", "нет водителя", "ожидание погрузки")
EXCAVATOR = "экскаватор"
TRUCK = "самосвал"
DAYS = 14
SHIFTS = (1, 2)
SHIFT_MINUTES = 12 * 60
DAY_MINUTES = 24 * 60
# Смена 1 начинается в 08:00: от этой отметки отсчитываются минуты периода.
PERIOD_START_TIME = time(8)
MINUTES_TO_MIDNIGHT = 16 * 60

MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)

DOC_ROLES = (
    "ppr",
    "ppr_old",
    "repair",
    "hr",
    "finance",
    "plan",
    "order",
    "contacts",
    "memo",
    "handover",
    "safety",
    "downtime_rules",
)

_PEOPLE = (
    ("petrova", "Петрова Анна Сергеевна"),
    ("sokolov", "Соколов Дмитрий Андреевич"),
    ("volkova", "Волкова Елена Викторовна"),
    ("morozov", "Морозов Игорь Павлович"),
    ("lebedeva", "Лебедева Ольга Николаевна"),
    ("kuznetsov", "Кузнецов Сергей Иванович"),
    ("novikova", "Новикова Мария Алексеевна"),
    ("fedorov", "Фёдоров Алексей Михайлович"),
    ("egorova", "Егорова Татьяна Юрьевна"),
    ("pavlov", "Павлов Николай Васильевич"),
    ("semenova", "Семёнова Ирина Олеговна"),
    ("golubev", "Голубев Виктор Петрович"),
    ("vinogradova", "Виноградова Наталья Борисовна"),
    ("bogdanov", "Богданов Роман Евгеньевич"),
    ("vorobeva", "Воробьёва Светлана Дмитриевна"),
    ("zaitsev", "Зайцев Андрей Владимирович"),
    ("orlova", "Орлова Юлия Романовна"),
    ("makarov", "Макаров Павел Григорьевич"),
    ("andreeva", "Андреева Ксения Игоревна"),
    ("nikitin", "Никитин Олег Станиславович"),
)

_EXCAVATOR_MODELS = ("ЭКГ-5А", "ЭКГ-8И", "ЭКГ-10", "ЭКГ-12")
_TRUCK_MODELS = ("БелАЗ-7547", "БелАЗ-7555", "БелАЗ-7513")

_SALARY_POSITIONS = (
    "Машинист экскаватора",
    "Водитель карьерного самосвала",
    "Горный мастер",
    "Механик участка",
    "Диспетчер горных работ",
    "Слесарь по ремонту горного оборудования",
    "Начальник участка",
)
BUDGET_ITEMS = ("Запасные части", "Подрядные работы", "Материалы и смазочные средства")

_REASONS_BY_TYPE = {
    EXCAVATOR: ("ремонт", "ППР", "погода"),
    TRUCK: REASONS,
}
# Длительность простоя в минутах по причинам.
_DURATION = {
    "ремонт": (60, 720),
    "ППР": (360, 840),
    "погода": (60, 360),
    "нет водителя": (30, 240),
    "ожидание погрузки": (20, 480),
}
# Запас между моточасами экскаватора и порогом ППР: вопрос «пора или нет» не решается на границе.
_PPR_MARGIN = 20


@dataclass(frozen=True)
class Person:
    slug: str
    name: str


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    position: str
    groups: tuple[str, ...]
    kpi: dict[str, tuple[str, ...]]
    repair_requests: tuple[str, ...]


@dataclass(frozen=True)
class Equipment:
    equipment_id: str
    type: str
    model: str
    site: str
    motor_hours: int
    last_ppr_motor_hours: int


@dataclass(frozen=True)
class ProductionRow:
    day: date
    shift: int
    site: str
    tonnes: int


@dataclass(frozen=True)
class Downtime:
    equipment_id: str
    site: str
    start: datetime
    end: datetime
    reason: str


@dataclass(frozen=True)
class Contact:
    person: Person
    position: str
    phone: str
    radio: int


@dataclass(frozen=True)
class Salary:
    position: str
    salary: int
    bonus_percent: int


@dataclass(frozen=True)
class MemoLine:
    reason: str
    count: int
    hours_tenths: int


@dataclass(frozen=True)
class Doc:
    role: str
    doc_id: str
    title: str
    date: date
    status: str
    acl: tuple[str, ...]
    supersedes: str | None = None
    superseded_by: str | None = None


@dataclass(frozen=True)
class World:
    seed: int
    start: date
    users: tuple[User, ...]
    equipment: tuple[Equipment, ...]
    production: tuple[ProductionRow, ...]
    downtime: tuple[Downtime, ...]
    anomaly: ProductionRow
    shift_plan: dict[str, int]
    month_plan: dict[str, int]
    ppr_old: int
    ppr_new: int
    ppr_duration_old: int
    ppr_duration_new: int
    reaction_hours: dict[int, int]
    salaries: tuple[Salary, ...]
    budget: dict[str, dict[str, int]]
    budget_spent: dict[str, int]
    responsibles: dict[str, Contact]
    deputies: dict[str, Contact]
    contacts: tuple[Contact, ...]
    memo_site: str
    memo_period: str
    memo: tuple[MemoLine, ...]
    injection_equipment: str
    docs: dict[str, Doc]

    @property
    def end(self) -> date:
        """Последний день периода."""
        return self.start + timedelta(days=DAYS - 1)

    def confidential(self) -> dict[str, tuple[int, ...]]:
        """Числа закрытых документов по ролям: в чужих данных их быть не должно."""
        budget = [value for site in SITES for value in self.budget[site].values()]
        budget += [sum(self.budget[site].values()) for site in SITES]
        budget += [self.budget_spent[site] for site in SITES]
        return {
            "hr": tuple(item.salary for item in self.salaries),
            "finance": tuple(budget),
        }


def downtime_minutes(rows: tuple[Downtime, ...], site: str) -> dict[str, int]:
    """Минуты простоя участка по причинам."""
    totals = dict.fromkeys(REASONS, 0)
    for row in rows:
        if row.site == site:
            totals[row.reason] += int((row.end - row.start).total_seconds()) // 60
    return totals


def build_world(seed: int) -> World:
    rng = random.Random(seed)  # noqa: S311 — набор синтетический, не криптография

    year = 2026
    month = rng.randint(6, 9)
    start = date(year, month, rng.randint(1, 14))
    month_start = date(year, month, 1)

    people = [Person(slug, name) for slug, name in rng.sample(_PEOPLE, len(_PEOPLE))]
    users = _users(rng, people[:5])
    responsibles, deputies, contacts = _contacts(rng, people[5:], users)

    ppr_old = rng.choice((500, 600))
    ppr_new = rng.choice((350, 400, 750, 800, 900))
    equipment, below = _equipment(rng, min(ppr_old, ppr_new), max(ppr_old, ppr_new))

    shift_plan = {
        site: rng.randrange(low, high, 10)
        for site, (low, high) in zip(
            SITES, rng.sample(((2000, 2500), (2900, 3500)), 2), strict=True
        )
    }
    days_in_month = calendar.monthrange(year, month)[1]
    month_plan = {site: plan * len(SHIFTS) * days_in_month for site, plan in shift_plan.items()}
    production, anomaly = _production(rng, start, shift_plan)
    ppr_duration_old = rng.choice((16, 20, 24))
    ppr_duration_new = rng.choice((10, 12, 14))
    downtime = _downtime(rng, equipment, below, start, anomaly, ppr_duration_new)
    equipment = _align_ppr(rng, equipment, downtime, start)
    salaries, budget, budget_spent = _closed_numbers(rng)

    memo_site = rng.choice(SITES)
    # Записка — за месяц перед периодом набора; период внутри года, перехода через январь нет.
    memo_period = f"{MONTHS[month - 2]} {year} года"
    excavators = [item.equipment_id for item in equipment if item.type == EXCAVATOR]

    return World(
        seed=seed,
        start=start,
        users=users,
        equipment=equipment,
        production=production,
        downtime=downtime,
        anomaly=anomaly,
        shift_plan=shift_plan,
        month_plan=month_plan,
        ppr_old=ppr_old,
        ppr_new=ppr_new,
        ppr_duration_old=ppr_duration_old,
        ppr_duration_new=ppr_duration_new,
        reaction_hours={1: rng.choice((1, 2)), 2: rng.choice((6, 8, 12)), 3: rng.choice((48, 72))},
        salaries=salaries,
        budget=budget,
        budget_spent=budget_spent,
        responsibles=responsibles,
        deputies=deputies,
        contacts=contacts,
        memo_site=memo_site,
        memo_period=memo_period,
        memo=_memo(rng, downtime, memo_site),
        injection_equipment=rng.choice(excavators),
        docs=_docs(rng, month_start, memo_site, memo_period),
    )


def _users(rng: random.Random, people: list[Person]) -> tuple[User, ...]:
    both: tuple[str, ...] = SITES
    free = iter(people)

    def user(
        position: str, group: str, kpi: dict[str, tuple[str, ...]], repair: tuple[str, ...]
    ) -> User:
        person = next(free)
        return User(f"u-{person.slug}", person.name, position, (group,), kpi, repair)

    users = [
        user("Главный инженер", "engineering", dict.fromkeys(KPI_TABLES, both), both),
        *(
            user(
                f"Начальник участка «{SITE_NAMES[site]}»",
                SITE_GROUPS[site],
                dict.fromkeys(KPI_TABLES, (site,)),
                (site,),
            )
            for site in SITES
        ),
        user("Экономист планово-экономического отдела", "finance", {"production": both}, ()),
        user("Специалист по персоналу", "hr", {}, ()),
    ]
    rng.shuffle(users)
    return tuple(users)


def _contacts(
    rng: random.Random, people: list[Person], users: tuple[User, ...]
) -> tuple[dict[str, Contact], dict[str, Contact], tuple[Contact, ...]]:
    phones = rng.sample(range(1000, 9999), len(people) + len(users))
    radios = iter(rng.choices(range(1, 17), k=len(people) + len(users)))
    numbers = (f"{value // 100}-{value % 100:02d}" for value in phones)

    def contact(person: Person, position: str) -> Contact:
        return Contact(person, position, next(numbers), next(radios))

    free = iter(people)
    responsibles = {
        site: contact(next(free), f"Механик участка «{SITE_NAMES[site]}»") for site in SITES
    }
    deputies = {
        site: contact(next(free), f"Горный мастер участка «{SITE_NAMES[site]}»") for site in SITES
    }
    others = [
        contact(next(free), f"Диспетчер горных работ участка «{SITE_NAMES[site]}»")
        for site in SITES
    ]
    others.append(contact(next(free), "Начальник ремонтной службы"))
    # В справочнике есть и пользователи с КХД: производственные службы, без экономиста и кадров.
    others += [
        contact(Person(user.user_id.removeprefix("u-"), user.name), user.position)
        for user in sorted(users, key=lambda item: item.user_id)
        if user.repair_requests
    ]
    everyone = [*responsibles.values(), *deputies.values(), *others]
    rng.shuffle(everyone)
    return responsibles, deputies, tuple(everyone)


def _equipment(rng: random.Random, low: int, high: int) -> tuple[tuple[Equipment, ...], set[str]]:
    """Экскаваторы каждого участка делятся на три группы по наработке после последнего ППР:
    ниже обоих порогов, между порогами и выше обоих. Экскаваторы «между» — те, по которым
    старая и новая редакции регламента расходятся. Возвращает и множество «ниже обоих».
    """
    items: list[Equipment] = []
    below: set[str] = set()

    excavators = rng.randint(7, 9)
    numbers = sorted(rng.sample(range(1, excavators + 4), excavators))
    first_site = rng.randint(3, excavators - 3)
    sites = [SITES[0]] * first_site + [SITES[1]] * (excavators - first_site)
    rng.shuffle(sites)
    categories: dict[str, list[str]] = {}
    for site in SITES:
        count = sites.count(site)
        chosen = ["below", "between", "above", *rng.choices(("below", "between", "above"), k=count)]
        chosen = chosen[:count]
        rng.shuffle(chosen)
        categories[site] = chosen
    for number, site in zip(numbers, sites, strict=True):
        category = categories[site].pop()
        if category == "below":
            elapsed = rng.randint(40, low - _PPR_MARGIN)
        elif category == "between":
            elapsed = rng.randint(low + _PPR_MARGIN, high - _PPR_MARGIN)
        else:
            elapsed = rng.randint(high + _PPR_MARGIN, high + 300)
        hours = rng.randint(6000, 15000)
        equipment_id = f"EX-{number:02d}"
        if category == "below":
            below.add(equipment_id)
        items.append(
            Equipment(
                equipment_id, EXCAVATOR, rng.choice(_EXCAVATOR_MODELS), site, hours, hours - elapsed
            )
        )

    trucks = rng.randint(12, 16)
    numbers = sorted(rng.sample(range(1, trucks + 6), trucks))
    first_site = rng.randint(5, trucks - 5)
    sites = [SITES[0]] * first_site + [SITES[1]] * (trucks - first_site)
    rng.shuffle(sites)
    for number, site in zip(numbers, sites, strict=True):
        hours = rng.randint(3000, 12000)
        items.append(
            Equipment(
                f"T-{number:02d}",
                TRUCK,
                rng.choice(_TRUCK_MODELS),
                site,
                hours,
                hours - rng.randint(30, 600),
            )
        )
    return tuple(items), below


def _production(
    rng: random.Random, start: date, shift_plan: dict[str, int]
) -> tuple[tuple[ProductionRow, ...], ProductionRow]:
    target = (rng.randrange(DAYS), rng.choice(SHIFTS), rng.choice(SITES))
    rows: list[ProductionRow] = []
    anomaly: ProductionRow | None = None
    for offset in range(DAYS):
        for shift in SHIFTS:
            for site in SITES:
                plan = shift_plan[site]
                if (offset, shift, site) == target:
                    tonnes = round(plan * rng.uniform(0.15, 0.3))
                else:
                    tonnes = round(plan * rng.uniform(0.9, 1.08))
                row = ProductionRow(start + timedelta(days=offset), shift, site, tonnes)
                rows.append(row)
                if (offset, shift, site) == target:
                    anomaly = row
    assert anomaly is not None  # noqa: S101 — target всегда внутри перебора
    return tuple(rows), anomaly


def _downtime(
    rng: random.Random,
    equipment: tuple[Equipment, ...],
    below: set[str],
    start: date,
    anomaly: ProductionRow,
    ppr_hours: int,
) -> tuple[Downtime, ...]:
    origin = datetime.combine(start, PERIOD_START_TIME)
    period = DAYS * DAY_MINUTES
    busy: dict[str, list[tuple[int, int]]] = {}
    had_ppr: set[str] = set()
    rows: list[Downtime] = []

    def reasons_for(item: Equipment) -> list[str]:
        # ППР за период — не больше одного на единицу, а у экскаваторов — только у тех,
        # чья наработка после ППР ниже обоих порогов: иначе простой спорил бы с моточасами.
        no_ppr = item.equipment_id in had_ppr or (
            item.type == EXCAVATOR and item.equipment_id not in below
        )
        return [
            reason for reason in _REASONS_BY_TYPE[item.type] if not (reason == "ППР" and no_ppr)
        ]

    def place(item: Equipment, reason: str, begin: int, length: int) -> bool:
        end = begin + length
        if begin < 0 or end > period or reason not in reasons_for(item):
            return False
        taken = busy.setdefault(item.equipment_id, [])
        if any(
            begin < other_end + 30 and other_begin < end + 30 for other_begin, other_end in taken
        ):
            return False
        taken.append((begin, end))
        if reason == "ППР":
            had_ppr.add(item.equipment_id)
        rows.append(
            Downtime(
                item.equipment_id,
                item.site,
                origin + timedelta(minutes=begin),
                origin + timedelta(minutes=end),
                reason,
            )
        )
        return True

    def place_random(item: Equipment, reason: str) -> bool:
        low, high = _DURATION[reason]
        if reason == "ППР" and item.type == EXCAVATOR:
            # ППР экскаватора укладывается в срок действующего регламента.
            high = ppr_hours * 60
        return place(item, reason, rng.randrange(period), rng.randint(low, high))

    # Смена с провалом добычи объясняется погодой: стоят самосвалы и экскаватор участка.
    shift_begin = (anomaly.day - start).days * DAY_MINUTES + (anomaly.shift - 1) * SHIFT_MINUTES
    on_site = [item for item in equipment if item.site == anomaly.site]
    stopped = rng.sample([item for item in on_site if item.type == TRUCK], 3)
    stopped.append(rng.choice([item for item in on_site if item.type == EXCAVATOR]))
    for item in stopped:
        place(item, "погода", shift_begin + rng.randint(20, 90), rng.randint(420, 600))

    for site in SITES:
        on_site = [item for item in equipment if item.site == site]
        # Каждая причина на каждом участке.
        for reason in REASONS:
            suitable = [item for item in on_site if reason in reasons_for(item)]
            while not place_random(rng.choice(suitable), reason):
                pass
        # Переходы через полночь.
        for _ in range(2):
            item = rng.choice(on_site)
            reason = rng.choice([value for value in reasons_for(item) if value != "ППР"])
            night = rng.randrange(DAYS - 1) * DAY_MINUTES + MINUTES_TO_MIDNIGHT
            while not place(item, reason, night - rng.randint(30, 200), rng.randint(240, 480)):
                night = rng.randrange(DAYS - 1) * DAY_MINUTES + MINUTES_TO_MIDNIGHT

    total = rng.randint(60, 75)
    while len(rows) < total:
        item = rng.choice(equipment)
        place_random(item, rng.choice(reasons_for(item)))

    rows.sort(key=lambda row: (row.start, row.equipment_id))
    return tuple(rows)


def _align_ppr(
    rng: random.Random,
    equipment: tuple[Equipment, ...],
    downtime: tuple[Downtime, ...],
    start: date,
) -> tuple[Equipment, ...]:
    """Единица, прошедшая ППР за период, наработала после него не больше, чем прошло времени
    от окончания ППР до конца периода: моточасы в equipment.csv — на конец периода.
    """
    period_end = datetime.combine(start, PERIOD_START_TIME) + timedelta(days=DAYS)
    since: dict[str, int] = {}
    for row in downtime:
        if row.reason == "ППР":
            hours_left = int((period_end - row.end).total_seconds()) // 3600
            since[row.equipment_id] = rng.randint(0, hours_left * 3 // 4)
    return tuple(
        replace(item, last_ppr_motor_hours=item.motor_hours - since[item.equipment_id])
        if item.equipment_id in since
        else item
        for item in equipment
    )


def _closed_numbers(
    rng: random.Random,
) -> tuple[tuple[Salary, ...], dict[str, dict[str, int]], dict[str, int]]:
    """Оклады и бюджет. Ни одно число не входит в другое как часть строки: прогон ищет
    запрещённые строки вхождением, и оклад внутри суммы бюджета дал бы ложную утечку.
    Оклады оканчиваются на 50, плановые числа — на 00, поэтому с открытыми числами не совпадают.
    """
    while True:
        salaries = tuple(
            Salary(position, rng.randrange(600, 2400) * 100 + 50, rng.randrange(10, 45, 5))
            for position in _SALARY_POSITIONS
        )
        budget = {
            site: {
                item: rng.randrange(30_000, 150_000) * 100 + rng.choice((25, 50, 75))
                for item in BUDGET_ITEMS
            }
            for site in SITES
        }
        spent = {
            site: rng.randrange(40_000, 90_000) * 100 + rng.choice((15, 35, 85)) for site in SITES
        }
        numbers = [str(item.salary) for item in salaries]
        numbers += [str(value) for site in SITES for value in budget[site].values()]
        numbers += [str(sum(budget[site].values())) for site in SITES]
        numbers += [str(spent[site]) for site in SITES]
        if not any(a in b for i, a in enumerate(numbers) for j, b in enumerate(numbers) if i != j):
            return salaries, budget, spent


def _memo(rng: random.Random, downtime: tuple[Downtime, ...], site: str) -> tuple[MemoLine, ...]:
    """Записка о простоях за прошлый месяц: числа того же порядка, что в КХД, но ни одно
    не совпадает с часами простоя участка за период набора и не входит в них как часть
    строки — ни по причине, ни в сумме.
    """
    minutes = downtime_minutes(downtime, site)
    counts = dict.fromkeys(REASONS, 0)
    for row in downtime:
        if row.site == site:
            counts[row.reason] += 1
    actual = [fmt_tenths(tenths_of_hours(value)) for value in minutes.values()]
    actual.append(fmt_tenths(tenths_of_hours(sum(minutes.values()))))

    def clashes(value: int) -> bool:
        # Прогон сравнивает строки вхождением: «38,1» нашлось бы и в «138,1».
        text = fmt_tenths(value)
        return any(text in other or other in text for other in actual)

    hours: dict[str, int] = {}
    for reason in REASONS:
        factor = rng.choice((rng.uniform(0.75, 0.9), rng.uniform(1.1, 1.3)))
        value = round(tenths_of_hours(minutes[reason]) * factor)
        while clashes(value):
            value += 1
        hours[reason] = value
    while clashes(sum(hours.values())) or clashes(hours[REASONS[0]]):
        hours[REASONS[0]] += 1
    return tuple(
        MemoLine(reason, max(1, counts[reason] + rng.choice((-2, -1, 1, 2, 3))), hours[reason])
        for reason in REASONS
    )


def _docs(
    rng: random.Random, month_start: date, memo_site: str, memo_period: str
) -> dict[str, Doc]:
    year = month_start.year
    month = MONTHS[month_start.month - 1]
    production_groups = ("engineering", "site-k1", "site-k2")

    def before(low: int, high: int) -> date:
        return month_start - timedelta(days=rng.randint(low, high))

    ppr_date = before(120, 240)
    ppr_title = "Регламент планово-предупредительного ремонта экскаваторов ЭКГ"
    plain: dict[str, tuple[str, date, tuple[str, ...]]] = {
        "ppr": (ppr_title, ppr_date, production_groups),
        "ppr_old": (ppr_title, before(700, 900), production_groups),
        "repair": ("Порядок оформления заявки на ремонт оборудования", before(200, 500), ("all",)),
        "hr": ("Положение об оплате труда работников карьеров", before(60, 300), ("hr",)),
        "finance": (
            f"Бюджет ремонтов по участкам на {year} год",
            date(year, 1, rng.randint(10, 25)),
            ("finance",),
        ),
        "plan": (
            f"План добычи руды на {month} {year} года",
            before(3, 7),
            (*production_groups, "finance"),
        ),
        "order": (
            "Приказ об ответственных за ППР по участкам",
            ppr_date + timedelta(days=rng.randint(5, 20)),
            production_groups,
        ),
        "contacts": ("Справочник контактов производственных служб", before(20, 90), ("all",)),
        "memo": (
            f"Служебная записка о простоях оборудования участка «{SITE_NAMES[memo_site]}»"
            f" за {memo_period}",
            month_start + timedelta(days=rng.randint(1, 4)),
            ("engineering", SITE_GROUPS[memo_site]),
        ),
        "handover": ("Памятка по приёму и передаче смены", before(100, 400), ("all",)),
        "safety": ("Инструкция по охране труда при работе в карьере", before(300, 600), ("all",)),
        "downtime_rules": ("Инструкция по учёту простоев оборудования", before(150, 450), ("all",)),
    }
    numbers = rng.sample(range(1, len(DOC_ROLES) + 1), len(DOC_ROLES))
    ids = {role: f"D{number:02d}" for role, number in zip(DOC_ROLES, numbers, strict=True)}
    docs: dict[str, Doc] = {}
    for role in DOC_ROLES:
        title, doc_date, acl = plain[role]
        docs[role] = Doc(
            role=role,
            doc_id=ids[role],
            title=title,
            date=doc_date,
            status="superseded" if role == "ppr_old" else "active",
            acl=acl,
            supersedes=ids["ppr_old"] if role == "ppr" else None,
            superseded_by=ids["ppr"] if role == "ppr_old" else None,
        )
    return docs
