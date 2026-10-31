-- Схема контура «AI-чат мобильного оператора».
-- Применяется целиком при подготовке стенда (make migrate); обратных миграций нет.

create extension if not exists vector;

-- ── Демо-пользователи контура (субъекты JWT) ─────────────────────────────
create table if not exists users(
    login   text primary key,
    sub     text unique not null,
    name    text not null,
    role    text not null,
    scopes  text[] not null
);

-- ── Домен оператора: состояние моков CRM / Billing / Tickets ─────────────
create table if not exists tariffs(
    name        text primary key,
    price_rub   int not null,
    data_gb     int not null,
    description text not null,
    available   boolean not null default true
);

create table if not exists subscribers(
    sub         text primary key,
    login       text unique not null references users(login),
    name        text not null,
    phone       text not null,
    email       text,
    tariff      text not null references tariffs(name),
    balance_rub numeric(10,2) not null default 0
);

create table if not exists subscriber_services(
    sub     text not null references subscribers(sub),
    service text not null,
    enabled boolean not null default true,
    price_rub int not null default 0,
    primary key (sub, service)
);

create table if not exists billing_events(
    id         bigserial primary key,
    sub        text not null,
    month      text not null,
    label      text not null,
    amount_rub numeric(10,2) not null,
    created_at timestamptz not null default now()
);

create table if not exists tariff_change_log(
    id              bigserial primary key,
    sub             text not null,
    before          text,
    after           text not null,
    idempotency_key text,
    created_at      timestamptz not null default now()
);

create table if not exists tickets(
    id         text primary key,
    sub        text not null,
    subject    text not null,
    note       text not null,
    status     text not null default 'open',
    created_at timestamptz not null default now()
);

-- ── База знаний: чанки + векторы (снапшот считается заранее) ─────────────
create table if not exists kb_chunks(
    id        bigserial primary key,
    source    text not null,
    title     text not null,
    body      text not null,
    embedding vector(768)
);

-- ── Контур: сессии, карточки подтверждений, идемпотентность ──────────────
create table if not exists sessions(
    session_id text primary key,
    login      text not null,
    sub        text not null,
    thread     jsonb not null default '[]',
    created_at timestamptz not null default now()
);

create table if not exists pending_cards(
    pending_id text primary key,
    session_id text not null,
    sub        text not null,
    action     text not null,
    details    jsonb not null,
    card       jsonb not null,
    state      text not null default 'pending',
    created_at timestamptz not null default now(),
    expires_at timestamptz not null
);

create table if not exists idempotency(
    key        text primary key,
    sub        text not null,
    action     text not null,
    result     jsonb,
    created_at timestamptz not null default now()
);

-- ── Журналы: аудит, LLM-вызовы, tool-вызовы, решения политики ────────────
create table if not exists audit_log(
    id              bigserial primary key,
    ts              timestamptz not null default now(),
    trace_id        text,
    session_id      text,
    who             text not null,
    what            text not null,
    outcome         text not null,
    args            jsonb,
    before          jsonb,
    after           jsonb,
    idempotency_key text,
    detail          text
);

create table if not exists llm_calls(
    id            bigserial primary key,
    ts            timestamptz not null default now(),
    trace_id      text,
    agent         text,
    model         text,
    input_tokens  int,
    output_tokens int,
    latency_ms    int,
    -- фактический провайдер модели (primary/secondary) и путь переключения
    provider      text,
    cost_rub      numeric(12,6),
    fallbacks     jsonb
);

-- миграция для баз, созданных до бонусного шага (идемпотентно)
alter table llm_calls add column if not exists provider  text;
alter table llm_calls add column if not exists cost_rub  numeric(12,6);
alter table llm_calls add column if not exists fallbacks jsonb;

create table if not exists tool_calls(
    id         bigserial primary key,
    ts         timestamptz not null default now(),
    trace_id   text,
    tool       text not null,
    args       jsonb,
    outcome    text,
    latency_ms int
);

create table if not exists policy_events(
    id         bigserial primary key,
    ts         timestamptz not null default now(),
    session_id text,
    sub        text,
    action     text not null,
    decision   text not null,
    reason     text
);
