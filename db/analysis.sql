-- GEO 분석용 분류 축 스키마 (`analysis`). public 테이블은 읽기만 하고, 분류 축 값은 모두 여기에 둔다. 멱등.
-- 선행: db/schema.sql, db/crawl_ext.sql (pages, page_labels, response_labels, citations.page_id·query_overlap)
--
-- 단위별 테이블
--   페이지  page_label(분류) · page_auto(자동: Markup·Freshness·Length) · format_genre_map(Format → Genre)
--   도메인  domain_label(Brand Role 대체값 · Authority)
--   인용    citation_label(Passage Type · Query Match 대체값) · citation_auto(Lang Match)
--   응답    response_label(Answer Dominance)
-- 규칙
--   · 기존에 값이 있는 축(Language, Publisher, Query Match, Brand Role, Engine, Answer Form, Overall Sentiment)은 복사하지 않고
--     뷰에서 조인한다. 기존 값이 비었을 때만 analysis 쪽 값을 쓴다 (coalesce(기존, analysis)).
--   · labeled_by = 'manual' 행은 rule/llm 의 UPDATE(upsert 포함)가 조용히 건너뛴다. 행 단위 보호.
--   · 재계산: select analysis.refresh_auto();   -- 자동 축(Markup·Freshness·Length·Lang Match)을 원본에서 다시 계산

create schema if not exists analysis;

-- ---------- Format → Genre ----------
-- Format 19개의 사전이자 Genre 파생 근거. Genre는 page_label에 저장하지 않고 이 표를 조인한다.
create table if not exists analysis.format_genre_map (
    format text primary key,
    genre  text not null check (genre in
               ('descriptive', 'instructional', 'persuasive', 'narrative', 'opinion', 'discussion', 'spoken', 'other'))
);

insert into analysis.format_genre_map (format, genre) values
    ('reference', 'descriptive'), ('tech_doc', 'descriptive'), ('academic', 'descriptive'), ('report_whitepaper', 'descriptive'),
    ('how_to', 'instructional'),
    ('product_page', 'persuasive'), ('company_intro', 'persuasive'), ('buyer_info', 'persuasive'),
    ('careers', 'persuasive'), ('partner', 'persuasive'),
    ('news', 'narrative'), ('press_release', 'narrative'), ('case_study', 'narrative'),
    ('blog_opinion', 'opinion'), ('comparison_list', 'opinion'),
    ('forum_qa', 'discussion'), ('faq', 'discussion'),
    ('interview', 'spoken'),
    ('other', 'other')
on conflict (format) do update set genre = excluded.genre;

-- ---------- 페이지 ----------
-- 분류 축은 기존 page_labels 값을 가져오지 않고 이 기준(분류 정의·값)으로 새로 라벨링해 저장한다.
-- language · publisher 는 pages.lang / page_labels.publisher_kind 가 비었을 때만 쓰는 대체값.
create table if not exists analysis.page_label (
    page_id        bigint primary key references public.pages (id) on delete cascade,
    format         text references analysis.format_genre_map (format),
    topic          text check (topic in ('ontology', 'graphrag', 'hallucination', 'ai_security', 'data_security',
                       'insider_threat', 'ot_security', 'digital_twin', 'on_prem_ai', 'ai_agent', 'regulation',
                       'ax_consulting', 'cost_roi', 'gov_support', 'palantir_alt', 'doc_ai', 'brand', 'other')),
    topic_keywords text[] not null default '{}',     -- Topic과 함께 쓰는 한글 키워드
    industry       text check (industry in ('general', 'manufacturing', 'defense', 'finance', 'energy_plant',
                       'semiconductor', 'public_sector', 'healthcare', 'telecom', 'automotive', 'retail_logistics',
                       'construction_shipbuilding')),
    persona        text check (persona in ('executive', 'practitioner', 'business')),
    tone           text check (tone in ('neutral_info', 'expert_analysis', 'promotional', 'opinion')),
    evidence       text check (evidence in ('stats', 'case', 'quote', 'source', 'authority', 'none')),
    visual         text check (visual in ('text_only', 'image', 'table', 'diagram', 'infographic', 'video', 'screenshot')),
    entity_focus   text check (entity_focus in ('self', 'competitor', 'ecosystem', 'customer', 'none')),
    self_authored  boolean,
    publisher      text check (publisher in ('media', 'brand', 'community', 'government', 'academic', 'other')),
    language       text check (language in ('ko', 'en', 'ja', 'zh', 'other')),
    label_version  text not null default 'v1',
    label_model    text,                              -- rule 이면 null 가능, llm 이면 모델명
    labeled_by     text not null check (labeled_by in ('rule', 'llm', 'manual')),
    labeled_at     timestamptz not null default now()
);
create index if not exists page_label_format_idx on analysis.page_label (format);

-- 자동 축: 크롤링 컬럼에서 계산해 값으로 저장 (analysis.refresh_auto)
create table if not exists analysis.page_auto (
    page_id     bigint primary key references public.pages (id) on delete cascade,
    markup      text check (markup in ('none', 'basic', 'rich')),     -- null = 본문 없음(pages.text null)
    freshness   text not null check (freshness in ('under_3mo', '3_6mo', '6_12mo', 'over_12mo', 'unknown')),
    word_count  int,                                                   -- Length
    computed_at timestamptz not null default now()
);

-- ---------- 도메인 ----------
-- brand_role 은 entities(role, domains)로 구할 수 없는 도메인의 대체값, authority 는 기존에 저장처가 없다.
create table if not exists analysis.domain_label (
    domain     text primary key check (domain = lower(domain)),
    brand_role text check (brand_role in ('self', 'competitor', 'ecosystem', 'customer', 'none')),
    authority  text check (authority in ('high', 'mid', 'low')),
    labeled_by text not null check (labeled_by in ('rule', 'llm', 'manual')),
    label_model text,
    labeled_at timestamptz not null default now()
);

-- ---------- 인용 ----------
-- query_match 는 citations.query_overlap 이 비었을 때만 쓰는 대체값
create table if not exists analysis.citation_label (
    citation_id  bigint primary key references public.citations (id) on delete cascade,
    passage_type text check (passage_type in ('definition', 'stats', 'table', 'list', 'quote', 'other')),
    query_match  real check (query_match between 0 and 1),
    label_model  text,
    labeled_by   text not null check (labeled_by in ('rule', 'llm', 'manual')),
    labeled_at   timestamptz not null default now()
);

create table if not exists analysis.citation_auto (
    citation_id bigint primary key references public.citations (id) on delete cascade,
    lang_match  text check (lang_match in ('match', 'mismatch')),      -- null = 페이지 언어를 알 수 없음
    computed_at timestamptz not null default now()
);

-- ---------- 응답 ----------
-- 행이 있고 answer_dominance 가 null 이면 "라벨링 결과 경쟁사가 답변에 없음", 행이 없으면 "아직 라벨링 안 함".
-- 판정 근거: 등장 순서 · 목록 순위 · 언급 분량 · 감성/입장. 경쟁사만 있고 인포시즈가 없으면 recessive.
create table if not exists analysis.response_label (
    response_id      bigint primary key references public.responses (id) on delete cascade,
    answer_dominance text check (answer_dominance in ('dominant', 'neutral', 'recessive')),
    labeled_by       text not null check (labeled_by in ('rule', 'llm', 'manual')),
    label_model      text,
    rationale        text,
    labeled_at       timestamptz not null default now()
);

alter table analysis.format_genre_map enable row level security;
alter table analysis.page_label       enable row level security;
alter table analysis.page_auto        enable row level security;
alter table analysis.domain_label     enable row level security;
alter table analysis.citation_label   enable row level security;
alter table analysis.citation_auto    enable row level security;
alter table analysis.response_label   enable row level security;

-- ---------- 수동 라벨 보호 ----------
-- 기존 행이 manual 이고 새 값이 manual 이 아니면 그 행의 UPDATE 를 건너뛴다 (on conflict do update 일괄 재실행이 중단되지 않게 에러 대신 skip).
create or replace function analysis.protect_manual() returns trigger language plpgsql as $$
begin
    if old.labeled_by = 'manual' and new.labeled_by <> 'manual' then
        return null;
    end if;
    return new;
end $$;

drop trigger if exists protect_manual on analysis.page_label;
create trigger protect_manual before update on analysis.page_label
    for each row execute function analysis.protect_manual();
drop trigger if exists protect_manual on analysis.domain_label;
create trigger protect_manual before update on analysis.domain_label
    for each row execute function analysis.protect_manual();
drop trigger if exists protect_manual on analysis.citation_label;
create trigger protect_manual before update on analysis.citation_label
    for each row execute function analysis.protect_manual();
drop trigger if exists protect_manual on analysis.response_label;
create trigger protect_manual before update on analysis.response_label
    for each row execute function analysis.protect_manual();

-- ---------- 자동 축 재계산 ----------
-- 원본(pages, citations, queries)이 바뀐 뒤: select analysis.refresh_auto();
-- Freshness 는 비교 시점을 pages.fetched_at 으로 고정하므로 시간이 지나도 저장값이 바뀌지 않는다(재수집 시에만 바뀜).
create or replace function analysis.refresh_auto() returns void language sql as $$
    insert into analysis.page_auto (page_id, markup, freshness, word_count)
    select p.id,
           case
               when p.text is null then null
               when cardinality(p.schema_types) > 0 and coalesce(p.h_count, 0) >= 3
                    and (coalesce(p.img_count, 0) = 0 or coalesce(p.alt_ratio, 0) >= 0.8) then 'rich'
               when cardinality(p.schema_types) > 0 or coalesce(p.h_count, 0) >= 1 then 'basic'
               else 'none'
           end,
           case
               when coalesce(p.modified_at, p.published_at) is null then 'unknown'
               else case
                   when coalesce(p.fetched_at, now()) - coalesce(p.modified_at, p.published_at) <= interval '90 days'  then 'under_3mo'
                   when coalesce(p.fetched_at, now()) - coalesce(p.modified_at, p.published_at) <= interval '180 days' then '3_6mo'
                   when coalesce(p.fetched_at, now()) - coalesce(p.modified_at, p.published_at) <= interval '365 days' then '6_12mo'
                   else 'over_12mo'
               end
           end,
           p.word_count
    from public.pages p
    on conflict (page_id) do update
        set markup = excluded.markup, freshness = excluded.freshness, word_count = excluded.word_count, computed_at = now()
        where (analysis.page_auto.markup, analysis.page_auto.freshness, analysis.page_auto.word_count)
              is distinct from (excluded.markup, excluded.freshness, excluded.word_count);

    -- Lang Match: 페이지 언어(pages.lang) vs 응답이 속한 쿼리의 언어(queries.language, fanout 쿼리 아님)
    insert into analysis.citation_auto (citation_id, lang_match)
    select c.id,
           case when p.lang is null or q.language is null then null
                when split_part(lower(p.lang), '-', 1) = q.language then 'match'
                else 'mismatch' end
    from public.citations c
    join public.responses r on r.id = c.response_id
    join public.queries q on q.id = r.query_id
    left join public.pages p on p.id = c.page_id
    on conflict (citation_id) do update
        set lang_match = excluded.lang_match, computed_at = now()
        where analysis.citation_auto.lang_match is distinct from excluded.lang_match;
$$;

-- ---------- 기본 분석 뷰 (인용 1행) ----------
-- 분석 대상 엔진 5개만 포함 (perplexity 등 제외). 쿼리 태그는 query_*, 페이지 태그는 page_* 로 구분.
create or replace view analysis.v_citation_analysis as
select c.id as citation_id, c.response_id, c.position, c.url, c.domain, c.page_id,
       r.engine, r.batch, r.run_no, r.query_id, r.executed_at,
       -- 쿼리 태그
       q.market, q.industry as query_industry, q.awareness_stage, q.anchor_type, q.constraint_type,
       q.persona as query_persona, q.language as query_language,
       -- 페이지 축 (분류는 analysis 기준)
       fg.genre, pl.format, pl.topic, pl.topic_keywords, pl.industry as page_industry, pl.persona as page_persona,
       pl.tone, pl.evidence, pa.markup, pl.visual,
       coalesce(p.lang, pl.language) as page_language,
       pa.freshness, pa.word_count,
       pl.entity_focus, pl.self_authored,
       coalesce(old_pl.publisher_kind, pl.publisher) as publisher,
       -- 도메인 축
       coalesce(
           (select e.role::text from public.entities e where e.id = c.entity_id),
           (select e.role::text from public.entities e
             where exists (select 1 from unnest(e.domains) d
                            where c.domain = lower(d) or c.domain like '%.' || lower(d))
             order by e.id limit 1),
           dl.brand_role, 'none') as brand_role,
       dl.authority,
       -- 인용 축
       coalesce(c.query_overlap, cl.query_match) as query_match,
       cl.passage_type, ca.lang_match,
       -- 응답 라벨
       rl.answer_form, rl.overall_sentiment,
       rd.answer_dominance, (rd.response_id is not null) as answer_dominance_labeled
from public.citations c
join public.responses r on r.id = c.response_id
join public.queries q on q.id = r.query_id
left join public.pages p on p.id = c.page_id
left join analysis.page_label pl on pl.page_id = c.page_id
left join analysis.format_genre_map fg on fg.format = pl.format
left join analysis.page_auto pa on pa.page_id = c.page_id
left join lateral (
    select x.publisher_kind from public.page_labels x
    where x.page_id = c.page_id order by x.labeled_at desc limit 1
) old_pl on true
left join analysis.domain_label dl on dl.domain = c.domain
left join analysis.citation_label cl on cl.citation_id = c.id
left join analysis.citation_auto ca on ca.citation_id = c.id
left join lateral (
    select x.answer_form, x.overall_sentiment from public.response_labels x
    where x.response_id = c.response_id order by x.labeled_at desc limit 1
) rl on true
left join analysis.response_label rd on rd.response_id = c.response_id
where r.engine in ('chatgpt', 'gemini', 'claude', 'naver_ai_briefing', 'google_aio');

-- ---------- 점검 뷰 ----------
-- 페이지에 연결되지 않은 인용 (분석 대상 엔진만). page_id_by_url 이 있으면 pages 에는 있는데 연결만 안 된 경우.
create or replace view analysis.v_check_unlinked_citations as
select c.id as citation_id, c.response_id, r.engine, r.batch, c.url, c.domain,
       (select p.id from public.pages p where p.url = c.url) as page_id_by_url
from public.citations c
join public.responses r on r.id = c.response_id
where c.page_id is null
  and r.engine in ('chatgpt', 'gemini', 'claude', 'naver_ai_briefing', 'google_aio');

-- 축별 라벨 커버리지. 단위(페이지·도메인·인용·응답)별로 분석 뷰에 등장하는 대상 중 값이 채워진 비율.
-- answer_dominance 는 값이 null 이어도 라벨 행이 있으면 '라벨됨'(경쟁사 없음)으로 센다.
create or replace view analysis.v_check_label_coverage as
with pg as (
    select distinct on (page_id) * from analysis.v_citation_analysis where page_id is not null order by page_id
), dm as (
    select distinct on (domain) * from analysis.v_citation_analysis order by domain
), rs as (
    select distinct on (response_id) * from analysis.v_citation_analysis order by response_id
), cov as (
    select 'page' as unit, v.axis, count(*) as total, count(v.val) as labeled
    from pg cross join lateral (values
        ('genre', pg.genre), ('format', pg.format), ('topic', pg.topic), ('industry', pg.page_industry),
        ('persona', pg.page_persona), ('tone', pg.tone), ('evidence', pg.evidence), ('markup', pg.markup),
        ('visual', pg.visual), ('language', pg.page_language), ('freshness', pg.freshness),
        ('length', pg.word_count::text), ('entity_focus', pg.entity_focus),
        ('self_authored', pg.self_authored::text), ('publisher', pg.publisher)) v(axis, val)
    group by v.axis
    union all
    select 'domain', v.axis, count(*), count(v.val)
    from dm cross join lateral (values ('authority', dm.authority)) v(axis, val)
    group by v.axis
    union all
    select 'citation', v.axis, count(*), count(v.val)
    from analysis.v_citation_analysis a cross join lateral (values
        ('query_match', a.query_match::text), ('passage_type', a.passage_type),
        ('lang_match', a.lang_match)) v(axis, val)
    group by v.axis
    union all
    select 'response', v.axis, count(*), count(v.val)
    from rs cross join lateral (values
        ('engine', rs.engine), ('answer_form', rs.answer_form), ('overall_sentiment', rs.overall_sentiment),
        ('answer_dominance', case when rs.answer_dominance_labeled then coalesce(rs.answer_dominance, 'no_competitor') end)) v(axis, val)
    group by v.axis
)
select unit, axis, total, labeled, round(labeled * 1.0 / nullif(total, 0), 4) as coverage
from cov;
