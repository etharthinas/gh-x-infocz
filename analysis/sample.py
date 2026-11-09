"""고정 시드 표본으로 분석 파이프라인 전체를 돌리고 요약을 출력한다.

표본: 분석 대상 엔진마다 인포시즈 도메인이 인용된 답변 n개 + 인용되지 않은 답변 n개.
순서는 md5(시드 || 응답 id)로 정하므로 같은 시드·같은 회차면 늘 같은 응답이 뽑힌다.
ponytail: 회차에 응답이 추가되면 표본이 바뀔 수 있다. 회차가 계속 늘어나면 뽑은 id를 테이블에 저장한다.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from . import DEFAULT_BATCH, ROOT, connect, fetch, jev, label, metrics, rules

SEED = "sample-2026-11"
ENGINES = ["chatgpt", "gemini", "claude", "naver_ai_briefing", "google_aio"]  # analysis.v_citation_analysis와 같은 목록

SUMMARY = {
    "responses": """
        select engine, cited, count(*) as n,
               count(*) filter (where infocz_mentioned) as mentioned,
               string_agg(distinct answer_form, ',') as answer_forms,
               count(*) filter (where overall_sentiment = 'positive') as pos,
               count(*) filter (where overall_sentiment = 'negative') as neg,
               count(*) filter (where answer_dominance = 'dominant') as dominant,
               count(*) filter (where answer_dominance = 'neutral') as neutral,
               count(*) filter (where answer_dominance = 'recessive') as recessive,
               count(*) filter (where dominance_labeled and answer_dominance is null) as no_competitor,
               string_agg(accuracy_pred, ',') as accuracy_pred
        from (select r.id, r.engine, r.infocz_mentioned, s.cited, l.answer_form, l.overall_sentiment, l.accuracy_pred,
                     d.answer_dominance, d.response_id is not null as dominance_labeled
              from responses r join sample s on s.id = r.id
              left join lateral (select * from response_labels x where x.response_id = r.id
                                 order by x.labeled_at desc limit 1) l on true
              left join analysis.response_label d on d.response_id = r.id) t
        group by engine, cited order by engine, cited desc""",
    "citations": """
        select a.engine, s.cited, count(*) as citations, count(a.page_id) as with_page,
               count(a.markup) as with_text, round(avg(a.query_match)::numeric, 2) as query_match,
               count(*) filter (where a.self_authored) as self_pages,
               count(*) filter (where a.lang_match = 'match') as lang_match
        from analysis.v_citation_analysis a join sample s on s.id = a.response_id
        group by a.engine, s.cited order by a.engine, s.cited desc""",
}
# 축별 분포 (표본 전체의 인용 기준)
DIST_AXES = ["format", "genre", "topic", "page_industry", "page_persona", "tone", "evidence", "entity_focus",
             "brand_role", "authority", "passage_type", "publisher", "visual", "markup", "freshness", "page_language",
             "lang_match"]


def response_ids(conn, *, batch: str = DEFAULT_BATCH, seed: str = SEED, per_group: int = 5) -> list[int]:
    return [r[0] for r in conn.execute(
        """
        select id from (
            select r.id, row_number() over (partition by r.engine, cited order by md5(%s || r.id::text)) as n
            from (select r.*, exists (select 1 from citations c where c.response_id = r.id and c.is_infocz) as cited
                  from responses r
                  where r.batch = %s and r.response_text is not null and r.engine = any(%s)) r
        ) x where n <= %s order by id
        """, (seed, batch, ENGINES, per_group)).fetchall()]


def summarize(conn, ids: list[int]) -> None:
    conn.execute(
        "create temp table sample as select r.id, exists (select 1 from citations c where c.response_id = r.id "
        "and c.is_infocz) as cited from responses r where r.id = any(%s)", (ids,))
    for name, sql in SUMMARY.items():
        cur = conn.execute(sql)
        print(f"\n[{name}]\n" + "\t".join(d.name for d in cur.description))
        for row in cur.fetchall():
            print("\t".join("" if v is None else str(v) for v in row))
    for axis in DIST_AXES:
        rows = conn.execute(
            f"select coalesce({axis}::text, '(null)'), count(*) from analysis.v_citation_analysis a "
            f"join sample s on s.id = a.response_id group by 1 order by 2 desc").fetchall()
        print(f"\n[{axis}] " + ", ".join(f"{v} {n}" for v, n in rows))


# 대시보드용 내보내기: 답변 1행 · 인용 페이지 1행 · 인용 1행. 모두 summarize()의 temp 테이블 sample을 쓴다
EXPORTS = {
    "responses": """
        select r.id as response_id, r.engine, s.cited, q.query_text, q.origin as query_origin,
               q.language as query_language, coalesce(r.infocz_mentioned, false) as infocz_mentioned,
               l.answer_form, l.overall_sentiment, l.accuracy_pred,
               array_to_string(l.topic, ', ') as topic, array_to_string(l.keywords, ', ') as keywords,
               case when d.response_id is null then null else coalesce(d.answer_dominance, 'no_competitor') end
                   as answer_dominance,
               d.rationale as dominance_rationale,
               (select count(*) from citations c where c.response_id = r.id) as citations,
               (select string_agg(e.name || ' (' || e.role || ') ' || coalesce(m.stance, '-') || ' / '
                                  || coalesce(m.sentiment, '-'), '; ' order by m.rank nulls last, e.name)
                from response_mentions m join entities e on e.id = m.entity_id where m.response_id = r.id) as mentions,
               r.response_text
        from responses r join sample s on s.id = r.id join queries q on q.id = r.query_id
        left join lateral (select * from response_labels x where x.response_id = r.id
                           order by x.labeled_at desc limit 1) l on true
        left join analysis.response_label d on d.response_id = r.id
        order by r.engine, s.cited desc, r.id""",
    "pages": """
        select a.page_id, max(a.url) as url, max(a.domain) as domain, max(p.title) as title, max(p.http_status) as http_status,
               max(a.format) as format, max(a.genre) as genre, max(a.topic) as topic,
               max(array_to_string(a.topic_keywords, ', ')) as topic_keywords,
               max(a.page_industry) as industry, max(a.page_persona) as persona, max(a.tone) as tone,
               max(a.evidence) as evidence, max(a.entity_focus) as entity_focus, bool_or(a.self_authored) as self_authored,
               max(a.publisher) as publisher, max(a.brand_role) as brand_role, max(a.authority) as authority,
               max(a.visual) as visual, max(a.markup) as markup, max(a.page_language) as language,
               max(a.freshness) as freshness, max(a.word_count) as word_count,
               count(*) as citations, string_agg(distinct a.engine, ', ') as engines,
               string_agg(distinct a.response_id::text, ', ') as response_ids
        from analysis.v_citation_analysis a join sample s on s.id = a.response_id
        left join pages p on p.id = a.page_id
        where a.page_id is not null group by a.page_id order by count(*) desc, a.page_id""",
    "citations": """
        select a.citation_id, a.response_id, a.engine, s.cited, a.position, a.page_id, a.url, a.domain,
               a.brand_role, round(a.query_match::numeric, 3) as query_match, a.passage_type, a.lang_match
        from analysis.v_citation_analysis a join sample s on s.id = a.response_id
        order by a.response_id, a.position nulls last, a.citation_id""",
    # 답변마다 인식된 회사: LLM 추출(analysis.response_vendor, 답변별 최신 버전)과 기존 정규식(response_mentions)을 맞댄다.
    # status  both 둘 다 · new 미등록 vendor(기존 방식이 못 잡음) · llm_only 등록됐지만 정규식이 놓침 ·
    #         unregistered_other 미등록 customer/other · regex_only 정규식만 · question 질문에 나온 엔티티(분석 제외)
    # terms   본문 하이라이트용 표기 (LLM 표기 + 등록 이름·별칭)
    "vendors": """
        with latest as (select distinct on (response_id) * from analysis.response_vendor
                        order by response_id, labeled_at desc),
        llm as (select v.response_id, x.ord, x.v ->> 'name' as name, x.v ->> 'kind' as kind,
                       (x.v ->> 'entity_id')::bigint as entity_id,
                       array(select jsonb_array_elements_text(x.v -> 'spellings')) as terms
                from latest v join sample s on s.id = v.response_id
                cross join lateral jsonb_array_elements(v.vendors) with ordinality x(v, ord)),
        rx as (select m.response_id, m.entity_id, m.rank, e.name || e.aliases as terms,
                      exists (select 1 from responses r join query_entities qe on qe.query_id = r.query_id
                              where r.id = m.response_id and qe.entity_id = m.entity_id) as asked
               from response_mentions m join sample s on s.id = m.response_id join entities e on e.id = m.entity_id)
        select coalesce(l.response_id, r.response_id) as response_id, l.ord, coalesce(l.name, e.name) as name, l.kind,
               coalesce(l.entity_id, r.entity_id) as entity_id, e.name as entity_name, e.role::text as entity_role,
               r.rank as regex_rank,
               case when r.asked then 'question'
                    when l.response_id is null then 'regex_only'
                    when l.entity_id is null then case when l.kind = 'vendor' then 'new' else 'unregistered_other' end
                    when r.response_id is null then 'llm_only'
                    else 'both' end as status,
               coalesce(l.terms, '{}') || coalesce(r.terms, '{}') as terms
        from llm l full join rx r on r.response_id = l.response_id and r.entity_id = l.entity_id
        left join entities e on e.id = coalesce(l.entity_id, r.entity_id)
        order by 1, l.ord nulls last, r.rank nulls last, 3""",
}


def export(conn, out: Path) -> None:
    """summarize()를 먼저 불러야 한다 (temp 테이블 sample)."""
    out.mkdir(parents=True, exist_ok=True)
    data = {}
    for name, sql in EXPORTS.items():
        cur = conn.execute(sql)
        cols = [d.name for d in cur.description]
        data[name] = [{c: float(v) if isinstance(v, Decimal) else v for c, v in zip(cols, row)} for row in cur.fetchall()]
        (out / f"{name}.json").write_text(json.dumps(data[name], ensure_ascii=False, default=str), encoding="utf-8")
        print(f"sample: {len(data[name])} rows → {out / name}.json")
    # dashboard.html(직접 만든 페이지)은 데이터를 한 줄로 품고 있으므로 그 줄만 바꾼다
    page = out / "dashboard.html"
    if page.exists():
        line = "<script>const DATA = " + json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/") + ";</script>"
        html = re.sub(r"^<script>const DATA = .*;</script>$", lambda _: line, page.read_text(encoding="utf-8"), count=1,
                      flags=re.M)
        page.write_text(html, encoding="utf-8")
        print(f"sample: {page} 데이터 갱신")


def run(*, batch: str = DEFAULT_BATCH, seed: str = SEED, per_group: int = 5) -> None:
    conn = connect(autocommit=True)
    ids = response_ids(conn, batch=batch, seed=seed, per_group=per_group)
    print(f"sample: seed={seed}, {len(ids)} responses: {ids}")
    fetch.run(batch=batch, response_ids=ids)
    metrics.run(batch=batch, response_ids=ids)
    label.label_responses(batch=batch, response_ids=ids)
    jev.run(batch=batch, response_ids=ids)
    label.label_topic_keywords(response_ids=ids)
    rules.run(batch=batch, response_ids=ids)
    summarize(conn, ids)
    export(conn, ROOT / "data" / "analysis" / "sample")
    conn.close()
