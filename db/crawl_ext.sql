-- 분석 계층: 인용 페이지 수집 · 라벨 · 집계 뷰. 멱등이므로 이미 데이터가 있는 DB에 그대로 적용한다.
-- 흐름: citations → pages(URL 수집 + 규칙 기반 특징) → page_labels(LLM 분류)
--       responses → response_labels(LLM 분류) / response_mentions.sentiment·stance
-- 출처 유형(own_brand / competitor_brand / true_third_party)은 열로 저장하지 않고 v_citations에서 entities.role로 계산한다.

-- ---------- 인용 페이지 ----------
create table if not exists pages (
    id             bigint generated always as identity primary key,
    url            text not null unique,
    domain         text generated always as (
                       lower(substring(url from '^[A-Za-z][A-Za-z0-9+.-]*://(?:www\.)?([^/:?#]+)'))
                   ) stored,
    fetched_at     timestamptz not null default now(),
    http_status    int,                 -- null = 연결 실패 (fetch_error 참고)
    fetch_error    text,
    content_type   text,
    lang           text,
    title          text,
    text           text,                -- 본문 앞부분만 보관 (analysis.fetch.TEXT_KEEP)
    published_at   timestamptz,
    modified_at    timestamptz,
    -- 규칙 기반 특징 (구조+기술 · 증거 수준 · 시각 요소 축)
    schema_types   text[] not null default '{}',   -- JSON-LD @type (페이지가 선언한 타입)
    h_count        int,
    img_count      int,
    alt_ratio      real,                -- ALT가 있는 이미지 비율, 이미지가 없으면 null
    table_count    int,
    has_video      boolean,
    stat_count     int,                 -- 수치 표현 개수
    quote_count    int,                 -- 따옴표 인용 + blockquote 개수
    outlink_count  int,                 -- 다른 도메인으로 나가는 링크
    word_count     int
);

alter table citations add column if not exists page_id bigint references pages (id) on delete set null;
alter table citations add column if not exists query_overlap real;   -- 질문 바이그램 중 (제목+본문 앞부분)에 있는 비율
create index if not exists citations_page_id_idx on citations (page_id);

-- ---------- 라벨 ----------
-- label_version을 올리면 이전 결과를 지우지 않고 다시 라벨링할 수 있다. 조회는 가장 최근 라벨을 쓴다.
create table if not exists page_labels (
    page_id        bigint not null references pages (id) on delete cascade,
    label_version  text not null,
    label_model    text not null,
    labeled_at     timestamptz not null default now(),
    page_form      text check (page_form in (
                       'news', 'blog_opinion', 'reference', 'tech_doc', 'how_to', 'faq', 'product_page',
                       'comparison_list', 'review', 'case_study', 'interview', 'report_whitepaper',
                       'forum_qa', 'press_release', 'academic', 'company_intro', 'other')),
    topic          text[] not null default '{}',
    industry       industry,            -- queries.industry와 같은 enum: 질문과 페이지의 산업 일치 여부를 조인으로 본다
    persona        text check (persona in ('executive', 'practitioner', 'business')),
    tone           text check (tone in ('neutral_info', 'promotional', 'expert_analysis', 'opinion')),
    evidence       text[] not null default '{}' check (evidence <@ array['stats', 'case', 'source']::text[]),
    publisher_kind text check (publisher_kind in ('brand', 'media', 'government', 'academic', 'community', 'other')),
    primary key (page_id, label_version)
);

create table if not exists response_labels (
    response_id       bigint not null references responses (id) on delete cascade,
    label_version     text not null,
    label_model       text not null,
    labeled_at        timestamptz not null default now(),
    answer_form       text check (answer_form in ('list', 'comparison', 'explanation', 'recommendation', 'how_to', 'other')),
    topic             text[] not null default '{}',
    keywords          text[] not null default '{}',
    overall_sentiment text check (overall_sentiment in ('positive', 'neutral', 'negative')),
    primary key (response_id, label_version)
);

-- 인포시즈 서술 정확도의 모델 판정(미검토). responses.accuracy에는 사람이 확인한 값(과 자동 반영하는 confused_infosys)만 둔다.
alter table response_labels add column if not exists accuracy_pred text
    check (accuracy_pred in ('correct', 'partial', 'wrong', 'confused_infosys'));

alter table response_mentions add column if not exists sentiment text check (sentiment in ('positive', 'neutral', 'negative'));
alter table response_mentions add column if not exists stance text check (stance in ('recommended', 'neutral', 'not_recommended', 'mentioned_only'));

alter table pages          enable row level security;
alter table page_labels    enable row level security;
alter table response_labels enable row level security;

-- ---------- 뷰 ----------
create or replace view v_citations as
select c.id as citation_id, c.response_id, c.position, c.url, c.domain, c.page_id, c.query_overlap,
       r.engine, r.batch, r.run_no, r.query_id, r.executed_at,
       q.language as query_language, q.market, q.industry as query_industry, q.awareness_stage,
       q.anchor_type, q.constraint_type, q.persona as query_persona,
       case e.role when 'self' then 'own_brand' when 'competitor' then 'competitor_brand'
                   else 'true_third_party' end as source_type,
       p.http_status, p.lang as page_lang, p.published_at, p.modified_at, p.schema_types,
       p.h_count, p.img_count, p.alt_ratio, p.table_count, p.has_video,
       p.stat_count, p.quote_count, p.outlink_count, p.word_count,
       extract(epoch from r.executed_at - p.published_at) / 86400 as page_age_days,
       l.page_form, l.topic as page_topic, l.industry as page_industry, l.persona as page_persona,
       l.tone, l.evidence, l.publisher_kind
from citations c
join responses r on r.id = c.response_id
join queries q on q.id = r.query_id
left join entities e on e.id = c.entity_id
left join pages p on p.id = c.page_id
left join lateral (
    select * from page_labels pl where pl.page_id = p.id order by pl.labeled_at desc limit 1
) l on true;

-- 엔진 × 출처 유형 비중
create or replace view v_engine_source_share as
select engine, batch, source_type, count(*) as n,
       round(count(*) * 1.0 / sum(count(*)) over (partition by engine, batch), 4) as share
from v_citations group by engine, batch, source_type;

-- 엔진 × 출처 유형별 평균 인용 순서 (낮을수록 답변 앞쪽)
create or replace view v_engine_citation_rank as
select engine, batch, source_type, count(*) as n, round(avg(position), 2) as avg_position
from v_citations group by engine, batch, source_type;

-- 엔진별 도메인 집중도: 상위 도메인부터 누적 인용 비중
create or replace view v_domain_concentration as
select engine, batch, domain, n,
       rank() over w as domain_rank,
       round(sum(n) over (partition by engine, batch order by n desc, domain
                          rows between unbounded preceding and current row)
             * 1.0 / sum(n) over (partition by engine, batch), 4) as cum_share
from (select engine, batch, domain, count(*) as n from v_citations group by engine, batch, domain) d
window w as (partition by engine, batch order by n desc);
