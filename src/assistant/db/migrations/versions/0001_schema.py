"""Три схемы, таблицы, построчная защита, автомат действия и функции в обход политик."""

from alembic import op

revision = "0001"
down_revision = None

# Кто пользователь, база узнаёт из параметра транзакции. Пустая строка — то же, что
# отсутствие: после первой транзакции на соединении из пула незаданный параметр читается
# как пустая строка, а не как NULL.
CURRENT_USER = """
create function app.current_user_id() returns text
language sql stable
set search_path = pg_catalog, pg_temp
as $$ select nullif(current_setting('app.user_id', true), '') $$;
"""

APP_TABLES = """
create table app.dataset_version (
    singleton boolean primary key default true check (singleton),
    content_hash text not null,
    loaded_at timestamptz not null default now()
);

create table app.users (
    user_id text primary key,
    name text not null,
    position_title text not null
);

create table app.user_groups (
    user_id text not null references app.users on delete cascade,
    group_name text not null,
    primary key (user_id, group_name)
);

create table app.kpi_grants (
    user_id text not null references app.users on delete cascade,
    table_name text not null,
    site text not null,
    primary key (user_id, table_name, site)
);

create table app.repair_grants (
    user_id text not null references app.users on delete cascade,
    site text not null,
    primary key (user_id, site)
);

create table app.sessions (
    user_id text not null references app.users on delete cascade,
    session_id text not null,
    busy_until timestamptz,
    capture_token uuid,
    created_at timestamptz not null default now(),
    primary key (user_id, session_id),
    constraint sessions_capture_complete check ((busy_until is null) = (capture_token is null))
);

create table app.turns (
    turn_id text primary key,
    user_id text not null,
    session_id text not null,
    question text not null,
    outcome text not null,
    answer text not null,
    citations jsonb not null default '[]',
    model_calls integer not null,
    input_tokens integer not null,
    cached_input_tokens integer,
    output_tokens integer not null,
    max_call_input_tokens integer not null,
    prefix_hash text not null,
    latency_ms integer not null,
    record jsonb not null default '{}',
    created_at timestamptz not null default now(),
    foreign key (user_id, session_id) references app.sessions on delete cascade,
    constraint turns_outcome check (
        outcome in ('answer', 'refusal', 'no_data', 'action', 'budget_exceeded', 'error')
    )
);
create index turns_session on app.turns (user_id, session_id, created_at);

create table app.messages (
    message_id bigint generated always as identity primary key,
    user_id text not null,
    session_id text not null,
    turn_id text not null references app.turns on delete cascade,
    seq integer not null,
    role text not null,
    content jsonb not null,
    chunk_ids text[] not null default '{}',
    foreign key (user_id, session_id) references app.sessions on delete cascade,
    unique (turn_id, seq)
);
create index messages_session on app.messages (user_id, session_id, message_id);

create table app.actions (
    action_id text primary key,
    user_id text not null references app.users on delete cascade,
    session_id text not null,
    -- В одном ходе — не больше одного действия.
    turn_id text not null unique references app.turns on delete cascade,
    tool text not null,
    params jsonb not null,
    status text not null default 'proposed',
    -- Ключ создаётся вместе со строкой, один раз; менять его запрещает триггер.
    idempotency_key uuid not null unique default gen_random_uuid(),
    request_id text,
    failure_reason text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint actions_status check (
        status in ('proposed', 'confirming', 'done', 'rejected', 'failed')
    ),
    constraint actions_request_iff_done check ((status = 'done') = (request_id is not null)),
    constraint actions_reason_iff_failed check ((status = 'failed') = (failure_reason is not null))
);
"""

# Автомат действия держит база: условное обновление в коде — первая проверка, триггер —
# вторая. Недопустимый переход, правка завершённого действия или смена ключа — ошибка.
ACTIONS_GUARD = """
create function app.actions_guard() returns trigger
language plpgsql
set search_path = pg_catalog, pg_temp
as $$
begin
    if tg_op = 'INSERT' then
        if new.status <> 'proposed' then
            raise exception 'действие создаётся в статусе proposed, а не %', new.status
                using errcode = 'check_violation';
        end if;
        return new;
    end if;

    if new.action_id is distinct from old.action_id
        or new.user_id is distinct from old.user_id
        or new.session_id is distinct from old.session_id
        or new.turn_id is distinct from old.turn_id
        or new.tool is distinct from old.tool
        or new.params is distinct from old.params
        or new.idempotency_key is distinct from old.idempotency_key
        or new.created_at is distinct from old.created_at
    then
        raise exception 'у действия % меняются только статус и результат', old.action_id
            using errcode = 'check_violation';
    end if;

    if new.status = old.status then
        if new.request_id is distinct from old.request_id
            or new.failure_reason is distinct from old.failure_reason
        then
            raise exception 'результат действия % записывается вместе с переходом', old.action_id
                using errcode = 'check_violation';
        end if;
        return new;
    end if;

    if (old.status, new.status) not in (
        ('proposed', 'confirming'),
        ('proposed', 'rejected'),
        ('confirming', 'done'),
        ('confirming', 'failed')
    ) then
        raise exception 'недопустимый переход действия %: % -> %',
            old.action_id, old.status, new.status
            using errcode = 'check_violation';
    end if;
    new.updated_at := now();
    return new;
end
$$;

create trigger actions_guard before insert or update on app.actions
    for each row execute function app.actions_guard();
"""

DWH_TABLES = """
-- Колонки как в CSV. Строки набора не обязаны быть уникальными, поэтому ключ — свой.
create table dwh.production (
    row_id bigint generated always as identity primary key,
    date date not null,
    shift smallint not null,
    site text not null,
    tonnes integer not null
);
create index production_site_date on dwh.production (site, date);

create table dwh.downtime (
    row_id bigint generated always as identity primary key,
    equipment_id text not null,
    site text not null,
    "start" timestamp not null,
    "end" timestamp not null,
    reason text not null
);
create index downtime_site_start on dwh.downtime (site, "start");

create table dwh.equipment (
    row_id bigint generated always as identity primary key,
    equipment_id text not null,
    type text not null,
    model text not null,
    site text not null,
    motor_hours integer not null,
    last_ppr_motor_hours integer not null
);
create index equipment_id on dwh.equipment (equipment_id);
"""

KB_TABLES = """
create table kb.documents (
    doc_id text primary key
);

create table kb.chunks (
    chunk_id text primary key,
    doc_id text not null references kb.documents on delete cascade,
    ordinal integer not null,
    section_no integer not null,
    part_no integer not null,
    title text not null,
    doc_date date not null,
    status text not null,
    acl text[] not null,
    supersedes text,
    superseded_by text,
    heading text not null,
    body text not null,
    terms text[] not null default '{}',
    tsv tsvector generated always as (
        to_tsvector(
            'russian'::regconfig,
            replace(lower(title || ' ' || heading || ' ' || body), 'ё', 'е')
        )
    ) stored,
    unique (doc_id, ordinal)
);
create index chunks_tsv on kb.chunks using gin (tsv);
create index chunks_terms on kb.chunks using gin (terms);
create index chunks_acl on kb.chunks using gin (acl);
"""

# Политики без списка ролей: действуют на всех, кроме владельца таблиц. Участки и группы
# база берёт из таблиц полномочий сама — сервис сообщает только идентификатор пользователя.
# Нет привязки или пользователя нет в app.users — ни одной строки.
POLICIES = """
alter table dwh.production enable row level security;
alter table dwh.downtime enable row level security;
alter table dwh.equipment enable row level security;
alter table kb.chunks enable row level security;
alter table kb.documents enable row level security;
alter table app.sessions enable row level security;
alter table app.turns enable row level security;
alter table app.messages enable row level security;

create policy production_by_grant on dwh.production for select using (
    exists (
        select 1 from app.kpi_grants g
        where g.user_id = app.current_user_id()
          and g.table_name = 'production' and g.site = production.site
    )
);
create policy downtime_by_grant on dwh.downtime for select using (
    exists (
        select 1 from app.kpi_grants g
        where g.user_id = app.current_user_id()
          and g.table_name = 'downtime' and g.site = downtime.site
    )
);
create policy equipment_by_grant on dwh.equipment for select using (
    exists (
        select 1 from app.kpi_grants g
        where g.user_id = app.current_user_id()
          and g.table_name = 'equipment' and g.site = equipment.site
    )
);

create policy chunks_by_group on kb.chunks for select using (
    exists (select 1 from app.users u where u.user_id = app.current_user_id())
    and acl && (
        array['all'] || array(
            select g.group_name from app.user_groups g where g.user_id = app.current_user_id()
        )
    )
);

create policy sessions_by_owner on app.sessions
    using (user_id = app.current_user_id())
    with check (user_id = app.current_user_id());
create policy turns_by_owner on app.turns
    using (user_id = app.current_user_id())
    with check (user_id = app.current_user_id());
create policy messages_by_owner on app.messages
    using (user_id = app.current_user_id())
    with check (user_id = app.current_user_id());
"""

# Три выборки мимо политик. Каждая возвращает одно значение без содержимого, выполняется
# с правами владельца и с фиксированным путём поиска схем; право вызова у всех отозвано
# и выдаётся только роли сервиса.
BYPASS_FUNCTIONS = """
create function kb.document_exists(p_doc_id text) returns boolean
language sql stable security definer
set search_path = pg_catalog, pg_temp
as $$ select exists (select 1 from kb.documents d where d.doc_id = p_doc_id) $$;

-- Идентификатор сравнивается как есть. Если в наборе он встречается несколько раз,
-- берётся первая строка по порядку файла.
create function dwh.equipment_site(p_equipment_id text) returns text
language sql stable security definer
set search_path = pg_catalog, pg_temp
as $$
    select e.site from dwh.equipment e
    where e.equipment_id = p_equipment_id
    order by e.row_id limit 1
$$;

-- Один бит: лучший по рангу действующий фрагмент среди всех документов закрыт для
-- текущего пользователя. Слова с цифрами в сравнении не участвуют, совпадение одного
-- слова признаком не считается: по функции нельзя подбирать содержимое закрытого документа.
-- Без привязки и для пользователя, которого нет в app.users, ответ всегда «нет» — как
-- и политики, функция ничего не сообщает тому, кого приложение не знает.
create function kb.best_match_is_closed(p_query text) returns boolean
language sql stable security definer
set search_path = pg_catalog, pg_temp
as $$
    with words as (
        select tsvector_to_array(
            to_tsvector(
                'russian'::regconfig,
                regexp_replace(
                    replace(lower(coalesce(p_query, '')), 'ё', 'е'), '\\S*[0-9]\\S*', ' ', 'g'
                )
            )
        ) as lexemes
    ),
    query as (
        select
            words.lexemes,
            (select string_agg(quote_literal(l), ' | ') from unnest(words.lexemes) l)::tsquery as q
        from words
    ),
    best as (
        select
            c.acl,
            (
                select count(*) from unnest(query.lexemes) l
                where c.tsv @@ quote_literal(l)::tsquery
            ) as matched
        from kb.chunks c, query
        where query.q is not null and c.status = 'active' and c.tsv @@ query.q
        order by ts_rank(c.tsv, query.q) desc, c.chunk_id
        limit 1
    )
    select coalesce(
        (
            select
                exists (select 1 from app.users u where u.user_id = app.current_user_id())
                and best.matched >= 2
                and not best.acl && (
                    array['all'] || array(
                        select g.group_name from app.user_groups g
                        where g.user_id = app.current_user_id()
                    )
                )
            from best
        ),
        false
    )
$$;

revoke all on function kb.document_exists(text) from public;
revoke all on function dwh.equipment_site(text) from public;
revoke all on function kb.best_match_is_closed(text) from public;
"""


def upgrade() -> None:
    op.execute("create schema app")
    op.execute("create schema dwh")
    op.execute("create schema kb")
    for block in (
        CURRENT_USER,
        APP_TABLES,
        ACTIONS_GUARD,
        DWH_TABLES,
        KB_TABLES,
        POLICIES,
        BYPASS_FUNCTIONS,
    ):
        op.execute(block)


def downgrade() -> None:
    op.execute("drop schema kb cascade")
    op.execute("drop schema dwh cascade")
    op.execute("drop schema app cascade")
