-- 인포시즈 GEO 측정 DB 스키마
-- 흐름: queries(seed) → responses(엔진별 실행) → response_fanouts(AI가 쓴 하위 검색어) / citations(인용 URL) / response_mentions(언급 엔티티)
-- 축 정의는 AXES.md, CONTEXT.md §5 참고. 코드 ↔ 한국어 라벨은 axis_labels 테이블.

-- ---------- 축 (enum) ----------
create type market          as enum ('us_security', 'kr_ax', 'kr_security');
create type industry        as enum ('semiconductor', 'energy_plant', 'finance', 'manufacturing_defense', 'general');
create type awareness_stage as enum ('problem', 'tech_name', 'tech_detail', 'vendor');
create type anchor_type     as enum ('general', 'competitor', 'ecosystem', 'customer');
create type constraint_type as enum ('none', 'deployment', 'scale', 'regulation', 'integration');
create type query_origin    as enum ('seed', 'fanout', 'control');
create type entity_role     as enum ('self', 'competitor', 'ecosystem', 'customer');

create table axis_labels (
    axis        text not null,          -- market | industry | awareness_stage | anchor_type | constraint_type
    code        text not null,
    label_ko    text not null,
    label_en    text not null,
    description text,
    primary key (axis, code)
);

-- ---------- 엔티티 레지스트리 ----------
-- 쿼리의 고유명사 앵커, 응답 내 언급, 인용 도메인 소유자를 한 곳에서 관리
create table entities (
    id       bigint generated always as identity primary key,
    name     text not null unique,
    aliases  text[] not null default '{}',     -- 표기 흔들림 매칭용 (AXIOM/액시엄 등)
    role     entity_role not null,
    markets  market[] not null default '{}',
    domains  text[] not null default '{}',     -- 인용 URL 도메인 매칭용
    notes    text
);

-- ---------- 쿼리 ----------
-- seed: 축 설계로 만든 측정 쿼리 / control: 브랜드 직접 질의(엔티티 정확도 통제 세트) / fanout: AI가 실제로 날린 하위 검색어
create table queries (
    id               bigint generated always as identity primary key,
    query_text       text not null,
    language         text not null check (language in ('ko', 'en')),
    origin           query_origin not null default 'seed',
    market           market,
    industry         industry,
    awareness_stage  awareness_stage,
    anchor_type      anchor_type,
    constraint_type  constraint_type,
    constraint_tags  constraint_type[] not null default '{}',  -- 대표값 외 추가 조건
    use_case         text,                                      -- 시장×산업 하위 유스케이스 (P&ID, IP 유출 등)
    template         text,                                      -- 엔티티 변형 쿼리의 원형 ({c} 자리에 경쟁사). 같은 template = 같은 슬롯
    persona          text check (persona in ('executive', 'practitioner', 'business')),
    parent_query_id  bigint references queries (id) on delete cascade,  -- fanout → 원래 seed 쿼리
    is_active        boolean not null default true,              -- false = 측정에서 제외 (MECE 리뷰로 비활성화한 슬롯 등)
    review_note      text,                                      -- 비활성화·수정 사유
    notes            text,
    created_at       timestamptz not null default now(),
    unique nulls not distinct (query_text, parent_query_id),
    constraint seed_has_axes check (
        origin <> 'seed' or (market is not null and industry is not null and awareness_stage is not null
                             and anchor_type is not null and constraint_type is not null)
    ),
    constraint fanout_has_parent check (origin <> 'fanout' or parent_query_id is not null)
);
create index on queries (origin);
create index on queries (market, industry);
create index on queries (parent_query_id);

create table query_entities (
    query_id  bigint not null references queries (id) on delete cascade,
    entity_id bigint not null references entities (id) on delete restrict,
    primary key (query_id, entity_id)
);

-- ---------- 엔진 · 응답 ----------
create table engines (
    code  text primary key,
    name  text not null,
    notes text
);

create table responses (
    id               bigint generated always as identity primary key,
    query_id         bigint not null references queries (id) on delete cascade,
    engine           text not null references engines (code),
    model            text,                       -- 예: gpt-5, sonar-pro
    batch            text,                       -- 측정 회차 라벨, 예: baseline-2026-10
    run_no           int not null default 1,     -- 같은 회차 내 반복 번호 (3~5회)
    settings         jsonb not null default '{}',-- 웹검색 on/off, locale, 로그인 여부 등
    executed_at      timestamptz not null default now(),
    response_text    text,
    raw              jsonb,                      -- API 원본 응답
    infocz_mentioned boolean,
    infocz_rank      int,                        -- 추천 목록 내 순위 (없으면 null)
    accuracy         text check (accuracy in ('correct', 'partial', 'wrong', 'confused_infosys')),
    notes            text,
    created_at       timestamptz not null default now(),
    unique nulls not distinct (batch, query_id, engine, model, run_no)
);
create index on responses (query_id);
create index on responses (engine, batch);

-- AI가 답변 중 사용한 하위 검색어 (query fan-out). 하위 검색어 자체는 queries(origin='fanout')에 저장
create table response_fanouts (
    response_id bigint not null references responses (id) on delete cascade,
    query_id    bigint not null references queries (id) on delete cascade,
    position    int,
    primary key (response_id, query_id)
);

create table citations (
    id              bigint generated always as identity primary key,
    response_id     bigint not null references responses (id) on delete cascade,
    url             text not null,
    domain          text generated always as (
                        lower(substring(url from '^[A-Za-z][A-Za-z0-9+.-]*://(?:www\.)?([^/:?#]+)'))
                    ) stored,
    is_infocz       boolean generated always as (
                        coalesce(lower(substring(url from '^[A-Za-z][A-Za-z0-9+.-]*://(?:www\.)?([^/:?#]+)'))
                                 ~ '(^|\.)infocz\.(co\.kr|ai)$', false)
                    ) stored,
    title           text,
    position        int,                         -- 답변 내 인용 순서
    snippet         text,                        -- 인용된 문장/구간
    fanout_query_id bigint references queries (id) on delete set null,  -- 이 인용을 가져온 하위 검색어 (알 수 있을 때)
    entity_id       bigint references entities (id) on delete set null, -- 도메인 소유 엔티티
    unique (response_id, url)
);
create index on citations (domain);

-- 응답에 언급된 엔티티 (인포시즈 · 경쟁사) 와 순위
create table response_mentions (
    response_id bigint not null references responses (id) on delete cascade,
    entity_id   bigint not null references entities (id) on delete restrict,
    rank        int,
    context     text,
    primary key (response_id, entity_id)
);

-- Supabase: public 스키마는 REST API로 노출되므로 RLS 활성화 (정책 없음 = anon 접근 차단, postgres/service_role은 우회)
alter table axis_labels       enable row level security;
alter table entities          enable row level security;
alter table queries           enable row level security;
alter table query_entities    enable row level security;
alter table engines           enable row level security;
alter table responses         enable row level security;
alter table response_fanouts  enable row level security;
alter table citations         enable row level security;
alter table response_mentions enable row level security;
