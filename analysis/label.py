"""LLM 라벨링: 질문(persona), 인용 페이지(page_labels), 답변(response_labels · 언급 감정 · 서술 정확도).

허용 값은 db/analysis.sql의 check 제약과 같아야 한다 (tests/test_analysis.py가 대조한다).
같은 (대상, label_version)은 다시 라벨링하지 않으므로, 프롬프트나 값 목록을 바꾸면 해당 *_VERSION을 올린다.
"""

from __future__ import annotations

from . import DEFAULT_BATCH, ROOT, connect, llm

PAGE_VERSION = "v1"
RESPONSE_VERSION = "v2"  # v2: 서술 정확도 판정 기준을 좁히고 accuracy_pred를 저장

PAGE_FORMS = ["news", "blog_opinion", "reference", "tech_doc", "how_to", "faq", "product_page", "comparison_list",
              "review", "case_study", "interview", "report_whitepaper", "forum_qa", "press_release", "academic",
              "company_intro", "other"]
INDUSTRIES = ["semiconductor", "energy_plant", "finance", "manufacturing", "defense", "general"]  # 실제 DB enum 기준
PERSONAS = ["executive", "practitioner", "business"]
TONES = ["neutral_info", "promotional", "expert_analysis", "opinion"]
EVIDENCE = ["stats", "case", "source"]
PUBLISHER_KINDS = ["brand", "media", "government", "academic", "community", "other"]
ANSWER_FORMS = ["list", "comparison", "explanation", "recommendation", "how_to", "other"]
SENTIMENTS = ["positive", "neutral", "negative"]
STANCES = ["recommended", "neutral", "not_recommended", "mentioned_only"]
ACCURACY = ["correct", "partial", "wrong", "confused_infosys", "not_applicable"]  # not_applicable은 저장하지 않는다
AUTO_ACCURACY = {"confused_infosys"}  # responses.accuracy에 자동 반영하는 판정. 나머지는 사람이 검토한 뒤 반영한다


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR = {"type": "string"}

_PERSONA_RUBRIC = """- executive: 경영진·임원. 투자 대비 효과, 전략, 위험, 도입 결정을 묻는다.
- practitioner: 실무 담당자(엔지니어, DX·보안·데이터 담당자). 구현 방식, 아키텍처, 도구 선택, 기술 세부를 묻는다.
- business: 현업 부서 사용자(생산, 운영, 재무, 영업 등). 자기 업무의 문제를 어떻게 풀지 묻는다."""

# ---------- 질문 ----------
QUERY_SYSTEM = f"""검색 질문을 입력한 사람이 어떤 독자인지 분류한다. 다음 중 하나만 고른다.
{_PERSONA_RUBRIC}
질문 문장에 드러난 관점(무엇을 알고 싶어 하는가)을 기준으로 판단한다."""
QUERY_SCHEMA = _obj({"persona": _enum(PERSONAS)})

# ---------- 페이지 ----------
PAGE_SYSTEM = f"""AI 검색 엔진이 인용한 웹 페이지를 분류한다. 제목과 본문 앞부분만 주어진다. 각 필드의 정의는 다음과 같다.

page_form (글의 형태, 하나):
news 뉴스 기사 / blog_opinion 개인·기업 블로그, 칼럼 / reference 백과·용어 설명 / tech_doc 제품·기술 문서, 매뉴얼 /
how_to 단계별 안내 / faq 질문과 답변 모음 / product_page 제품·서비스 소개 및 사양 / comparison_list 비교, 순위, 추천 목록 /
review 사용 후기, 평가 / case_study 고객 사례, 도입 사례 / interview 인터뷰 / report_whitepaper 보고서, 백서, 시장 분석 /
forum_qa 포럼, 커뮤니티 질의응답 / press_release 보도자료 / academic 논문, 학술 자료 / company_intro 회사 소개 / other

topic: 페이지가 다루는 핵심 주제 1~3개. 짧은 명사구로 쓴다 (예: 온톨로지, Graph-RAG, 내부자 위협 탐지, 비용과 ROI).
industry: 대상 산업. semiconductor 반도체·디스플레이 / energy_plant 에너지·발전·플랜트·정유화학 / finance 금융 /
manufacturing 공장·설비·생산, 스마트팩토리, 조선·가전·자동차 부품 / defense 방산·국방, 무기체계 / general 특정 산업 없음.
persona: 주로 읽을 독자.
{_PERSONA_RUBRIC}
tone: neutral_info 중립적 정보 전달 / promotional 자사 제품·서비스 홍보 / expert_analysis 전문가의 해설과 분석 / opinion 개인 의견.
evidence: 본문에 있는 근거의 종류를 모두 고른다 (없으면 빈 배열). stats 구체적 수치 / case 구체적 고객·도입 사례 / source 출처나 근거 자료 명시.
publisher_kind: 발행 주체. brand 제품·서비스를 파는 기업의 자사 사이트 / media 언론, 전문 매체, 리뷰·비교 사이트 /
government 정부·공공기관 / academic 학술기관·논문 / community 개인 블로그, 포럼, SNS, 사용자 생성 콘텐츠 / other.
도메인과 본문의 말투를 함께 보고 판단한다."""
PAGE_SCHEMA = _obj({
    "page_form": _enum(PAGE_FORMS), "topic": _arr(_STR), "industry": _enum(INDUSTRIES), "persona": _enum(PERSONAS),
    "tone": _enum(TONES), "evidence": _arr(_enum(EVIDENCE)), "publisher_kind": _enum(PUBLISHER_KINDS),
})

# ---------- 답변 ----------
def _infocz_facts() -> str:
    text = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    return text.split("## 인포시즈", 1)[1].split("## 핵심 진단", 1)[0].strip()


RESPONSE_SYSTEM = f"""AI 검색 엔진의 답변을 분류한다. 질문, 답변, 답변에서 감지된 엔티티 목록(번호: 이름 (역할))이 주어진다.

answer_form: list 항목 나열 / comparison 대상 간 비교 / explanation 개념·방법 설명 / recommendation 특정 회사·제품 추천 /
how_to 단계별 절차 / other.
topic: 답변의 핵심 주제 1~3개 (짧은 명사구).
keywords: 답변을 대표하는 핵심 용어 5~8개. 답변에 나온 표기 그대로 쓴다.
overall_sentiment: 답변 전체가 다루는 주제와 제시된 대안들에 대한 어조. positive / neutral / negative.
entities: 주어진 엔티티마다 한 항목. id는 목록의 번호를 그대로 쓴다.
  sentiment: 답변이 그 엔티티를 묘사하는 어조. positive 장점 강조 / neutral 사실 나열 / negative 단점·한계 지적.
  stance: recommended 추천하거나 적합하다고 제시 / neutral 비교 대상으로 설명 / not_recommended 비추천 또는 제외 권고 /
  mentioned_only 이름만 언급.
accuracy: 인포시즈에 대한 서술이 아래 확인된 사실과 맞는지 판정한다. 인포시즈가 언급되지 않았으면 not_applicable.
  correct 모두 아래 사실과 일치 / partial 아래 사실과 모순은 없지만 아래에 없는 내용(예: 공식 파트너 관계, 시장 순위,
  고객 수, 점유율)이 섞임 / wrong 아래 사실과 직접 모순됨(예: 다른 사업 영역, 다른 제품, 틀린 출시 시기) /
  confused_infosys 인도 Infosys와 혼동하거나 같은 회사로 서술.
  아래에 없는 내용은 아무리 단정적으로 쓰였더라도 틀렸다고 판단하지 말고 partial로 판정한다. 확인할 수 없기 때문이다.

[확인된 인포시즈 사실]
{_infocz_facts()}"""
RESPONSE_SCHEMA = _obj({
    "answer_form": _enum(ANSWER_FORMS), "topic": _arr(_STR), "keywords": _arr(_STR),
    "overall_sentiment": _enum(SENTIMENTS),
    "entities": _arr(_obj({"id": {"type": "integer"}, "sentiment": _enum(SENTIMENTS), "stance": _enum(STANCES)})),
    "accuracy": _enum(ACCURACY),
})
ANSWER_CHARS = 9000   # 수집된 답변의 최대 길이(약 7,900자)보다 크므로 잘리지 않는다
PAGE_CHARS = 1500


def label_queries(*, limit: int | None = None, use_batch_api: bool = False) -> None:
    conn = connect(autocommit=True)
    rows = conn.execute(
        "select id, query_text from queries where origin in ('seed', 'control') and is_active and persona is null "
        "order by id limit %s", (limit,)).fetchall()

    def save(results: dict[str, dict]) -> None:
        with conn.cursor() as cur:
            cur.executemany("update queries set persona = %s where id = %s and persona is null",
                            [(r["persona"], int(k)) for k, r in results.items()])

    print(f"label-queries: {len(rows)} queries")
    llm.run({str(i): text for i, text in rows}, system=QUERY_SYSTEM, schema=QUERY_SCHEMA, save=save,
            max_tokens=500, use_batch_api=use_batch_api)


def label_pages(*, limit: int | None = None, version: str = PAGE_VERSION, use_batch_api: bool = False) -> None:
    conn = connect(autocommit=True)
    live = {r[0] for r in conn.execute("select unnest(enum_range(null::industry))::text").fetchall()}
    assert live == set(INDUSTRIES), f"industry enum이 DB와 다릅니다: {sorted(live)}"
    rows = conn.execute(
        """
        select p.id, p.url, p.title, left(p.text, %s)
        from pages p
        join (select page_id, count(*) as n from citations group by page_id) c on c.page_id = p.id
        where p.http_status = 200 and length(p.text) > 50
          and not exists (select 1 from page_labels l where l.page_id = p.id and l.label_version = %s)
        order by c.n desc limit %s
        """, (PAGE_CHARS, version, limit)).fetchall()  # 많이 인용된 페이지부터 처리한다
    items = {str(i): f"URL: {url}\n제목: {title}\n본문(앞부분):\n{text}" for i, url, title, text in rows}

    def save(results: dict[str, dict]) -> None:
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into page_labels (page_id, label_version, label_model, page_form, topic, industry, persona,
                                         tone, evidence, publisher_kind)
                values (%s, %s, %s, %s, %s, %s::industry, %s, %s, %s, %s)
                on conflict (page_id, label_version) do nothing
                """,
                [(int(k), version, llm.MODEL, r["page_form"], r["topic"], r["industry"], r["persona"], r["tone"],
                  r["evidence"], r["publisher_kind"]) for k, r in results.items()])

    print(f"label-pages: {len(rows)} pages")
    llm.run(items, system=PAGE_SYSTEM, schema=PAGE_SCHEMA, save=save, max_tokens=1000, use_batch_api=use_batch_api)


def label_responses(*, batch: str = DEFAULT_BATCH, engine: str | None = None, limit: int | None = None,
                    version: str = RESPONSE_VERSION, use_batch_api: bool = False) -> None:
    conn = connect(autocommit=True)
    rows = conn.execute(
        """
        select r.id, q.query_text, r.response_text, coalesce(r.infocz_mentioned, false)
        from responses r join queries q on q.id = r.query_id
        where r.batch = %s and r.response_text is not null and (%s::text is null or r.engine = %s)
          and not exists (select 1 from response_labels l where l.response_id = r.id and l.label_version = %s)
        order by r.id limit %s
        """, (batch, engine, engine, version, limit)).fetchall()
    ids = [r[0] for r in rows]
    mentions: dict[int, dict[int, str]] = {i: {} for i in ids}
    for rid, eid, name, role in conn.execute(
            "select m.response_id, e.id, e.name, e.role from response_mentions m join entities e on e.id = m.entity_id "
            "where m.response_id = any(%s)", (ids,)).fetchall():
        mentions[rid][eid] = f"{eid}: {name} ({role})"

    items = {
        str(rid): (f"[질문] {query}\n[인포시즈 언급 여부] {'예' if mentioned else '아니오'}\n[감지된 엔티티]\n"
                   + ("\n".join(mentions[rid].values()) or "없음") + f"\n[답변]\n{text[:ANSWER_CHARS]}")
        for rid, query, text, mentioned in rows
    }
    mentioned_ids = {rid for rid, _, _, mentioned in rows if mentioned}

    def save(results: dict[str, dict]) -> None:
        with conn.cursor() as cur:
            for key, r in results.items():
                rid = int(key)
                cur.execute(
                    """
                    insert into response_labels (response_id, label_version, label_model, answer_form, topic, keywords,
                                                 overall_sentiment, accuracy_pred)
                    values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict (response_id, label_version) do nothing
                    """, (rid, version, llm.MODEL, r["answer_form"], r["topic"], r["keywords"], r["overall_sentiment"],
                          r["accuracy"] if rid in mentioned_ids and r["accuracy"] != "not_applicable" else None))
                cur.executemany(
                    "update response_mentions set sentiment = %s, stance = %s where response_id = %s and entity_id = %s",
                    [(e["sentiment"], e["stance"], rid, e["id"]) for e in r["entities"] if e["id"] in mentions[rid]])
                if rid in mentioned_ids and r["accuracy"] in AUTO_ACCURACY:
                    cur.execute("update responses set accuracy = %s where id = %s and accuracy is null",
                                (r["accuracy"], rid))

    print(f"label-responses: {len(rows)} responses")
    llm.run(items, system=RESPONSE_SYSTEM, schema=RESPONSE_SCHEMA, save=save, max_tokens=3000,
            use_batch_api=use_batch_api)
