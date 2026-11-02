"""Jev(TypeSafe System One) 분류: analysis 스키마의 페이지 · 도메인 · 인용 축 (db/axis_definition.md).

질문 문구 · 선택지 · 임계값은 모두 이 파일에 둔다. 지시문은 정의서를 영어로 옮긴 것이다
(Jev는 영어 정확도가 가장 높다). state(페이지 본문 등)는 원문 그대로 보낸다.
우선순위가 있는 축(Format · Evidence · Passage Type)은 값마다 Noul로 따로 묻고, 우선순위는 코드에서 적용한다.
Topic Keywords는 자유 텍스트라 Jev로 만들 수 없어서 여기서 다루지 않는다.
"""

from __future__ import annotations

import asyncio
import re

from dotenv import dotenv_values
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeError

from crawler.entities import _term_pattern

from . import DEFAULT_BATCH, ROOT, connect
from .label import PAGE_CHARS

MODEL = "jev-1.13.0"  # 버전 고정: jev-latest는 릴리스마다 답이 달라져 label_version과 맞지 않는다
LABEL_VERSION = "v3"  # v2: 선택지 이름 self → infocz, Format 차선 판정 추가 / v3: Infoseeds 오역 교정
TAG = f"{MODEL}@{LABEL_VERSION}"  # label_version 열이 없는 domain_label · citation_label의 label_model 값
YES = 0.7             # Noul 판정 기준 (TypeSafe 권장 출발점)
MAYBE = 0.3           # Format에서 YES인 값이 없을 때 차선으로 받아들이는 하한
CONCURRENCY = 16      # 분당 1,200 요청 제한보다 충분히 낮다
# Jev는 선택지 이름을 글자 그대로 읽어서 'self'를 '그 페이지 자신의 회사'로 해석한다. 묻을 때는 infocz로 쓰고 저장할 때 self로 바꾼다
SELF_OPTION = "infocz"

# ---------- 페이지 ----------
# 우선순위 순서대로 묶은 Format. 앞 묶음에서 YES 이상인 값이 있으면 그 묶음에서 확률이 가장 높은 값을 고른다.
FORMAT_TIERS = [
    {"press_release": "a press release issued by the company or institution itself",
     "careers": "a job posting or careers page",
     "faq": "a list of frequently asked questions written by the site operator",
     "forum_qa": "a community forum or Q&A thread where users post questions and others answer",
     "academic": "an academic paper or thesis with abstract, references, and author affiliations"},
    {"how_to": "a step-by-step guide the reader follows to do something",
     "comparison_list": "a comparison, ranking, or recommendation list of several products or vendors (TOP N, A vs B, alternatives)",
     "case_study": "a case study of a specific customer or project: problem, solution, and results",
     "interview": "an interview, talk, or lecture transcript centered on what a speaker says"},
    {"reference": "an encyclopedia, wiki, or glossary entry defining a concept, neutral, with nothing to sell",
     "tech_doc": "official technical documentation or a manual for a product, API, or architecture, with code, settings, or specs",
     "report_whitepaper": "a market or industry report, whitepaper, or research report with survey results and statistics",
     "product_page": "a page introducing or selling a product, solution, or service, centered on features, pricing, or a contact/demo button",
     "company_intro": "a company About page: vision, history, organization",
     "buyer_info": "information for buyers evaluating adoption: adoption process, cost structure, selection criteria",
     "partner": "a partner, alliance, or reseller program page",
     "news": "a news article in which a third-party media outlet reports an event, with a reporter or date"},
    {"blog_opinion": "a blog post or column written from the author's own point of view"},
]
TOPICS = {
    "ontology": "ontology or knowledge graph modeling",
    "graphrag": "GraphRAG, RAG, or graph-based retrieval",
    "hallucination": "LLM hallucination and how to reduce it",
    "ai_security": "AI or LLM security, such as prompt injection or model security",
    "data_security": "data security, leak prevention, or access control",
    "insider_threat": "insider threats or internal information leaks",
    "ot_security": "OT or industrial control system (ICS) security",
    "digital_twin": "digital twins",
    "on_prem_ai": "on-premise, air-gapped, or small LLM (sLLM) deployment",
    "ai_agent": "AI agents or work automation",
    "regulation": "AI regulation, law, or compliance",
    "ax_consulting": "AX or DX transformation, adoption strategy, or consulting",
    "cost_roi": "cost, ROI, or TCO",
    "gov_support": "government support programs, vouchers, or subsidies",
    "palantir_alt": "Palantir and alternative or similar platforms",
    "doc_ai": "document AI, OCR, or document automation",
    "other": "none of the topics above",
}
INDUSTRIES = {
    "general": "no specific industry, or several industries listed together",
    "manufacturing": "manufacturing: factories, production, MES",
    "defense": "defense or military",
    "finance": "banking, insurance, or securities",
    "energy_plant": "energy, power generation, or plants",
    "semiconductor": "semiconductors",
    "public_sector": "public sector or local government",
    "healthcare": "healthcare or bio",
    "telecom": "telecommunications",
    "automotive": "automotive or mobility",
    "retail_logistics": "retail or logistics",
    "construction_shipbuilding": "construction or shipbuilding",
}
PERSONAS = {
    "executive": "executives and decision makers: strategy, cost, and risk, written as a summary",
    "practitioner": "practitioners and engineers: implementation, configuration, code, many technical terms",
    "business": "business, planning, or purchasing staff: features, benefits, adoption review, shallow technical depth",
}
TONES = {
    "neutral_info": "neutral information: objective, almost no promotion or subjective wording",
    "expert_analysis": "expert analysis: interpretation, evaluation, and implications based on evidence",
    "promotional": "promotional: stresses the publisher's own superiority, superlatives, calls to action",
    "opinion": "opinion: the author's subjective view, first person, argument-centered",
}
EVIDENCE = {  # 우선순위 순서
    "stats": "The page supports its claims with specific numbers, ratios, or survey results.",
    "case": "The page supports its claims with a specific customer or project case.",
    "quote": "The page quotes an expert or customer, naming the speaker.",
    "source": "The page cites sources such as papers, reports, or links.",
    "authority": "The page relies on certifications, awards, institutional affiliation, or credentials as evidence.",
}
PUBLISHERS = {
    "media": "a news outlet or media company",
    "brand": "an official channel of a company or product",
    "community": "a community, personal blog, forum, or social media: user-generated",
    "government": "a government or public institution",
    "academic": "a university, research institute, or journal",
    "other": "none of the above",
}

# ---------- 도메인 · 인용 ----------
AUTHORITY = ["low", "mid", "high"]
AUTHORITY_LEVELS = [
    "low: personal blog, new or unknown source, or spam-like site",
    "mid: industry trade media, mid-sized company, or well-known platform",
    "high: government, university, major news outlet, or large company's official domain",
]
PASSAGES = {  # 우선순위 순서
    "table": "The passage is tabular data.",
    "list": "The passage is a bulleted or numbered list of items.",
    "stats": "The passage states specific numbers or ratios.",
    "quote": "The passage quotes a named speaker.",
    "definition": "The passage defines or explains something, in the form 'X is ...'.",
}


# 영문 기사가 인포시즈를 Infoseeds로 잘못 옮긴 사례가 있다. Infoseeds는 인도에 실제로 있는 다른 회사이므로,
# 인포시즈 제품명이 같은 글에 있을 때만 InfoCZ로 바꿔서 보낸다. 답변 라벨링에는 쓰지 않는다(오역 자체가 정확도 판정 대상이다).
_INFOSEEDS = re.compile(r"(?i)(?<![a-z])info\s?seeds(?![a-z])")
_PRODUCTS = [_term_pattern(t) for t in ("GOS", "AXIOM", "Axiom", "액시엄", "DELTAflow", "DeltaFlow", "델타플로우")]


def fix_infoseeds(*parts: str | None) -> list[str | None]:
    whole = "\n".join(p for p in parts if p)
    if not (_INFOSEEDS.search(whole) and any(p.search(whole) for p in _PRODUCTS)):
        return list(parts)
    return [_INFOSEEDS.sub("InfoCZ", p) if p else p for p in parts]


def pick(probs: dict[str, float], tiers: list[list[str]], fallback: str) -> str:
    """앞 묶음부터 YES 이상인 값을 찾고, 그 묶음 안에서는 확률이 가장 높은 값을 고른다. 없으면 fallback."""
    for tier in tiers:
        hits = [v for v in tier if probs.get(v, 0) >= YES]
        if hits:
            return max(hits, key=probs.get)
    return fallback


def as_role(option: str) -> str:
    return "self" if option == SELF_OPTION else option


def _choice(instructions: str, options: dict[str, str]) -> Choice:
    return Choice(instructions=instructions, criteria=options)


def _roles(conn) -> tuple[str, str]:
    """state 해석에 필요한 자사·경쟁사 이름 (entities 기준)."""
    self_names = ", ".join(n for row in conn.execute(
        "select name, aliases from entities where role = 'self'").fetchall() for n in [row[0], *row[1]])
    competitors = ", ".join(n for (n,) in conn.execute(
        "select name from entities where role = 'competitor' order by id").fetchall())
    return self_names, competitors


def page_questions(self_names: str, competitors: str) -> dict:
    q = {f"format:{v}": Noul(instructions=f"By its main purpose, this page is {desc}.")
         for tier in FORMAT_TIERS for v, desc in tier.items()}
    q |= {f"evidence:{v}": Noul(instructions=s) for v, s in EVIDENCE.items()}
    q["topic"] = _choice("What is the single main topic of this page? Prefer the topic in the title.", TOPICS)
    q["brand"] = Noul(instructions="The page is mainly about a specific company or brand itself, not a technology topic.")
    q["industry"] = _choice("Which industry does this page explicitly address? For a case study, use the customer's "
                            "industry.", INDUSTRIES)
    q["persona"] = _choice("Who is this page written for, judging by its terminology and depth?", PERSONAS)
    q["tone"] = _choice("What is the overall tone of this page?", TONES)
    q["entity_focus"] = _choice("Which company or party does this page mainly focus on?", {
        SELF_OPTION: f"InfoCZ (also written as {self_names}) or its products",
        "competitor": f"a vendor competing in the same market as InfoCZ, such as {competitors}",
        "ecosystem": "a cloud, partner, technology platform, or industry association",
        "customer": "a customer company that is the subject of an adoption case",
        "none": "no specific company: general information or concept explanation",
    })
    q["publisher"] = _choice("What type of organization published this page? Judge from the domain and the writing.",
                             PUBLISHERS)
    return q


def page_answer(r) -> dict:
    formats = {k.split(":", 1)[1]: a.noul for k, a in r.nouls.items() if k.startswith("format:")}
    evidence = {k.split(":", 1)[1]: a.noul for k, a in r.nouls.items() if k.startswith("evidence:")}
    topic = r.choices["topic"].choice
    if topic == "other" and r.nouls["brand"].noul >= YES:  # 기술 주제와 겹치면 기술 주제를 우선한다
        topic = "brand"
    fmt = pick(formats, [list(t) for t in FORMAT_TIERS], "other")
    if fmt == "other" and max(formats.values()) >= MAYBE:  # 확실히 맞는 형식이 없으면 가장 가까운 형식을 쓴다
        fmt = max(formats, key=formats.get)
    return {
        "format": fmt,
        "evidence": pick(evidence, [[v] for v in EVIDENCE], "none"),
        "topic": topic,
        **{k: r.choices[k].choice for k in ("industry", "persona", "tone", "publisher")},
        "entity_focus": as_role(r.choices["entity_focus"].choice),
        "low_confidence": [k for k, a in r.choices.items() if a.confidence < 0.5],
    }


def domain_questions(self_names: str, competitors: str) -> dict:
    return {
        "authority": Score(instructions="How credible and influential is this website domain?", criteria=AUTHORITY_LEVELS),
        "brand_role": _choice("What role does the owner of this domain play relative to InfoCZ?", {
            SELF_OPTION: f"InfoCZ itself ({self_names})",
            "competitor": f"a vendor in the same market as InfoCZ, such as {competitors}",
            "ecosystem": "a cloud provider, partner, technology platform, or association",
            "customer": "a company that adopted such a solution as a customer",
            "none": "not applicable: media, government, or a general website",
        }),
    }


def passage_questions() -> dict:
    return {v: Noul(instructions=s) for v, s in PASSAGES.items()}


# ---------- 호출 ----------
async def _ask_all(items: dict[str, tuple[object, dict]]) -> dict:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with AsyncTypeSafeClient(api_key=dotenv_values(ROOT / ".env")["JEV_API_KEY"], model=MODEL) as client:
        async def one(key, state, questions):
            async with sem:
                try:
                    return key, await client.system_one(state=state, questions=questions)
                except TypeSafeError as e:  # SDK가 429·529는 재시도한 뒤에 올린다
                    print(f"jev: {key} failed ({type(e).__name__}: {e})")
                    return key, None
        return dict(await asyncio.gather(*(one(k, s, q) for k, (s, q) in items.items())))


def ask(items: dict[str, tuple[object, dict]]) -> dict:
    """items: {키: (state, questions)}. 실패한 항목은 None이다. 다음 실행에서 다시 시도된다."""
    results = asyncio.run(_ask_all(items))
    tokens = sum(r.usage.input_tokens or 0 for r in results.values() if r)
    print(f"jev: {sum(1 for r in results.values() if r)}/{len(items)} ok, {tokens} input tokens")
    return results


# ---------- 라벨링 ----------
def label_pages(conn, roles, *, response_ids=None, limit=None) -> None:
    rows = conn.execute(
        """
        select p.id, p.url, p.title, left(p.text, %s) from pages p
        where p.http_status = 200 and length(p.text) > 50
          and not exists (select 1 from analysis.page_label l
                          where l.page_id = p.id and l.format is not null and l.label_version = %s)
          and (%s::bigint[] is null
               or exists (select 1 from citations c where c.page_id = p.id and c.response_id = any(%s)))
        order by p.id limit %s
        """, (PAGE_CHARS, LABEL_VERSION, response_ids, response_ids, limit)).fetchall()
    questions = page_questions(*roles)
    states = {}
    for pid, url, title, text in rows:
        title, text = fix_infoseeds(title, text)
        states[str(pid)] = ({"url": url, "title": title, "text": text}, questions)
    results = ask(states)
    answers = {int(k): page_answer(r) for k, r in results.items() if r}
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into analysis.page_label (page_id, format, topic, industry, persona, tone, evidence, entity_focus,
                                             publisher, label_version, label_model, labeled_by)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'llm')
            on conflict (page_id) do update
                set format = excluded.format, topic = excluded.topic, industry = excluded.industry,
                    persona = excluded.persona, tone = excluded.tone, evidence = excluded.evidence,
                    entity_focus = excluded.entity_focus, publisher = excluded.publisher,
                    label_version = excluded.label_version, label_model = excluded.label_model,
                    labeled_by = 'llm', labeled_at = now()
            """,  # 규칙 축(self_authored · language · visual)은 건드리지 않는다. manual 행은 트리거가 건너뛴다
            [(pid, a["format"], a["topic"], a["industry"], a["persona"], a["tone"], a["evidence"], a["entity_focus"],
              a["publisher"], LABEL_VERSION, MODEL) for pid, a in answers.items()])
    low = [k for a in answers.values() for k in a["low_confidence"]]
    print(f"label-axes: {len(answers)} pages, low-confidence choices {dict((k, low.count(k)) for k in set(low))}")


def label_domains(conn, roles, *, batch, response_ids=None, limit=None) -> None:
    rows = conn.execute(
        """
        select c.domain, (array_agg(distinct p.title) filter (where p.title is not null))[1:5]
        from citations c join responses r on r.id = c.response_id left join pages p on p.id = c.page_id
        where r.batch = %s and c.domain is not null and (%s::bigint[] is null or r.id = any(%s))
          and not exists (select 1 from analysis.domain_label d where d.domain = c.domain and d.label_model = %s)
        group by c.domain order by c.domain limit %s
        """, (batch, response_ids, response_ids, TAG, limit)).fetchall()
    questions = domain_questions(*roles)
    results = ask({d: ({"domain": d, "page_titles": titles or []}, questions) for d, titles in rows})
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into analysis.domain_label (domain, brand_role, authority, labeled_by, label_model)
            values (%s, %s, %s, 'llm', %s)
            on conflict (domain) do update
                set brand_role = excluded.brand_role, authority = excluded.authority, labeled_by = 'llm',
                    label_model = excluded.label_model, labeled_at = now()
            """,
            [(d, as_role(r.choices["brand_role"].choice),
              AUTHORITY[max(r.scores["authority"].probabilities, key=r.scores["authority"].probabilities.get)], TAG)
             for d, r in results.items() if r])
    print(f"label-axes: {sum(1 for r in results.values() if r)} domains")


def label_citations(conn, *, batch, response_ids=None, limit=None) -> None:
    rows = conn.execute(
        """
        select c.id, c.snippet from citations c join responses r on r.id = c.response_id
        where r.batch = %s and c.snippet <> '' and (%s::bigint[] is null or r.id = any(%s))
          and not exists (select 1 from analysis.citation_label l where l.citation_id = c.id and l.label_model = %s)
        order by c.id limit %s
        """, (batch, response_ids, response_ids, TAG, limit)).fetchall()  # snippet은 현재 google_aio에만 있다
    results = ask({str(cid): (snippet, passage_questions()) for cid, snippet in rows})
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into analysis.citation_label (citation_id, passage_type, labeled_by, label_model)
            values (%s, %s, 'llm', %s)
            on conflict (citation_id) do update
                set passage_type = excluded.passage_type, labeled_by = 'llm', label_model = excluded.label_model,
                    labeled_at = now()
            """,
            [(int(k), pick({v: a.noul for v, a in r.nouls.items()}, [[v] for v in PASSAGES], "other"), TAG)
             for k, r in results.items() if r])
    print(f"label-axes: {sum(1 for r in results.values() if r)} citations (passage_type)")


def run(*, batch: str = DEFAULT_BATCH, response_ids: list[int] | None = None, limit: int | None = None) -> None:
    conn = connect(autocommit=True)
    roles = _roles(conn)
    label_pages(conn, roles, response_ids=response_ids, limit=limit)
    label_domains(conn, roles, batch=batch, response_ids=response_ids, limit=limit)
    label_citations(conn, batch=batch, response_ids=response_ids, limit=limit)
    conn.close()
