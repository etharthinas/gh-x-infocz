"""키워드 사전: LLM이 뽑은 키워드를 대표 키워드로 묶는다 (analysis.keyword · analysis.keyword_alias, db/analysis.sql).

원래 표기는 그대로 둔다 (response_labels.keywords · page_label.topic_keywords). 사전은 표기 → 대표 키워드 대응만 기록하고,
어느 표기가 더 많이 인용되는지는 analysis.v_keyword_variant_citations 에서 본다.

register()가 키워드를 사전에 넣는다.
  1. 표면 키(analysis.keyword_key: NFC · 소문자 · 공백/구두점 제거)가 별칭에 있으면 → 그 대표 키워드
  2. 없으면 임베딩으로 가까운 대표 키워드 후보(코사인 거리 ≤ CANDIDATE_DISTANCE, 최대 TOP_K개)를 찾고
  3. Haiku가 후보 중 '같은 용어'(번역 · 표기 · 줄임말 · 어순 차이)를 고르면 → 그 별칭으로 붙임
  4. 후보가 없거나 Haiku가 고르지 않으면 → 새 대표 키워드
임베딩 거리만으로 정하지 않는 이유: bge-m3에서는 더 좁은 용어(팔란티어 온톨로지 → 온톨로지 0.19)나
관련만 있는 용어(미탐률 → 오탐률 0.22)가 진짜 변이(팔란티어 ↔ Palantir 0.275)보다 가깝다. 임계값 하나로는 가를 수 없다.

라벨링(label.py)이 저장할 때마다 호출하고, 기존 라벨은 `python -m analysis normalize-keywords`로 채운다.
임베딩은 로컬 bge-m3(다국어, 1024차원). sentence-transformers 필요 (requirements-embed.txt, 모델 약 2.2GB).
"""

from __future__ import annotations

import os
from typing import Iterable

from . import llm

MODEL_NAME = os.environ.get("KEYWORD_MODEL", "BAAI/bge-m3")
CANDIDATE_DISTANCE = float(os.environ.get("KEYWORD_CANDIDATE_DISTANCE", 0.35))  # 이보다 먼 대표 키워드는 후보로도 안 본다
TOP_K = 5
# low는 좁은 개념을 자주 합쳤다(네이버클라우드 → NHN 클라우드, LLM analysis → LLM). medium은 그런 오병합을 모두 거르고,
# 대신 일부 진짜 변이를 놓친다(환각 현상 ≠ 환각). 잘못 합치면 다른 개념의 인용이 섞이므로 놓치는 쪽을 택했다.
JUDGE_EFFORT = "medium"

MATCH_SYSTEM = """키워드 사전을 정리한다. 새 키워드 하나와 이미 사전에 있는 후보 키워드 목록(번호)이 주어진다.
새 키워드와 '같은 용어'인 후보의 번호를 고른다. 없으면 0. 먼저 reason에 한 문장으로 근거를 쓴다.

같은 용어: 가리키는 대상이 같고 표기만 다르다. 다음은 모두 같은 용어다.
  - 번역·음차: 팔란티어 = Palantir, 바이브컴퍼니 = Vibe Company, 내부자 위협 = insider threat
  - 괄호 병기·약어 병기: 고스(GOS) = GOS, 총소유비용(TCO) = TCO, 루시아(LUXIA) = 루시아
  - 철자·띄어쓰기·대소문자: 시멘틱 레이어 = 시맨틱 레이어, 그래프 RAG = GraphRAG
  - 줄임말·동의어 표기: 온프렘 = 온프레미스, Graph DB = 그래프 데이터베이스, 접근 제어 = 접근 통제
  - 회사명 접두어 유무(같은 제품): Apache Iceberg = Iceberg, Palantir Foundry = 팔란티어 Foundry
  - 옛 이름·새 이름: Azure Sentinel = Microsoft Sentinel
  - 어순·단복수: 에이전트 AI = AI 에이전트, knowledge graphs = Knowledge Graph
다른 용어: 한정어가 붙어 범위가 좁아지거나 다른 것을 가리키면 다른 용어다.
  - 분야·산업·대상 한정: 보안 지식 그래프 ≠ 지식그래프, 제조 지식그래프 ≠ 지식그래프, 반도체 MES ≠ MES,
    개인정보 국외이전 ≠ 데이터 국외 이전
  - 활동·제품·구성요소를 덧붙임: 수율 관리 ≠ 수율, 이상탐지 모델 ≠ 이상 탐지, AI 보안 솔루션 ≠ AI 보안,
    보안 관제센터 ≠ 보안관제, Data Intelligence Platform ≠ 데이터 인텔리전스
  - 파생·별칭 개념: K-팔란티어(한국형 팔란티어 기업) ≠ 팔란티어, 팔란티어 온톨로지 ≠ 온톨로지
  - 관련 있지만 다른 개념: 스마트 제조 ≠ 스마트팩토리, 미탐률 ≠ 오탐률, 로그 분석 ≠ 감사 로그
판단 기준: 두 표기를 서로 바꿔 써도 뜻이 그대로면 같은 용어, 한쪽에만 있는 말이 범위를 바꾸면 다른 용어."""
MATCH_SCHEMA = {"type": "object", "properties": {"reason": {"type": "string"}, "match": {"type": "integer"}},
                "required": ["reason", "match"], "additionalProperties": False}

_model = None
_warned = False


def available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def _encode(texts: list[str]):
    """정규화된 벡터 (numpy, 행 = 텍스트). 코사인 거리 = 1 - 내적 = pgvector <=>."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model.encode(texts, normalize_embeddings=True, batch_size=64)


def _lit(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def clean(keywords: Iterable[str]) -> list[str]:
    """앞뒤 공백 제거, 빈 값 제거, 순서 유지 중복 제거."""
    out: list[str] = []
    for k in keywords:
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def match_prompt(raw: str, candidates: list[str]) -> str:
    return f"새 키워드: {raw}\n후보:\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, 1))


def register(conn, keywords: Iterable[str], *, candidate_distance: float = CANDIDATE_DISTANCE) -> dict[str, int]:
    """키워드를 사전에 넣고 {표기: 대표 키워드 id}를 돌려준다. conn은 autocommit 연결.

    한 번에 들어온 새 키워드끼리도 후보가 된다(앞에 온 것이 먼저 대표 키워드가 됨). LLM 판정은 병렬로 받고,
    판정이 실패한 키워드는 등록하지 않는다 (다음 호출이나 normalize-keywords에서 다시 시도).
    """
    raws = clean(keywords)
    if not raws:
        return {}
    keyed = [(raw, key) for raw, key in conn.execute(
        "select k, analysis.keyword_key(k) from unnest(%s::text[]) with ordinality t(k, o) order by o", (raws,))
        .fetchall() if key]
    known = dict(conn.execute("select alias_key, keyword_id from analysis.keyword_alias where alias_key = any(%s)",
                              ([key for _, key in keyed],)).fetchall())
    todo: dict[str, str] = {}  # key → 처음 본 표기 (들어온 순서)
    for raw, key in keyed:
        if key not in known:
            todo.setdefault(key, raw)
    if todo:
        known.update(_add(conn, list(todo.items()), candidate_distance))
    return {raw: known[key] for raw, key in keyed if key in known}


def _add(conn, new: list[tuple[str, str]], candidate_distance: float) -> dict[str, int]:
    surf = [raw for _, raw in new]
    vecs = _encode(surf)
    lits = [_lit(v) for v in vecs]
    # 후보: ('db', keyword_id, 표기, 거리) 또는 ('peer', 이번 묶음 안의 앞 순번, 표기, 거리)
    near: dict[int, list[tuple]] = {i: [] for i in range(len(new))}
    for o, kid, text, d in conn.execute(  # 한 번의 왕복으로 전체 후보 조회 (풀러가 멀어서 키워드마다 물으면 느리다)
            """
            select v.o, k.id, k.canonical, k.d
            from unnest(%s::text[]) with ordinality v(vec, o)
            cross join lateral (
                select id, canonical, embedding <=> v.vec::extensions.vector as d from analysis.keyword
                where model = %s order by d limit %s) k
            where k.d <= %s
            """, (lits, MODEL_NAME, TOP_K, candidate_distance)).fetchall():
        near[o - 1].append(("db", kid, text, float(d)))
    cands: list[list[tuple]] = []
    for i in range(len(new)):
        c = near[i]
        if i:
            d = 1 - vecs[:i] @ vecs[i]
            c += [("peer", j, surf[j], float(d[j])) for j in range(i) if d[j] <= candidate_distance]
        cands.append(sorted(c, key=lambda x: x[3])[:TOP_K])

    items = {str(i): match_prompt(surf[i], [x[2] for x in c]) for i, c in enumerate(cands) if c}
    verdicts: dict[str, dict] = {}
    if items:
        llm.run(items, system=MATCH_SYSTEM, schema=MATCH_SCHEMA, save=verdicts.update, max_tokens=4000,
                effort=JUDGE_EFFORT)

    placed: dict[int, int] = {}  # 순번 → keyword_id
    out: dict[str, int] = {}
    for i, (key, raw) in enumerate(new):
        if str(i) in items and str(i) not in verdicts:
            continue  # 판정 실패 → 이번엔 건너뜀
        m = verdicts.get(str(i), {}).get("match", 0)
        kid, dist = None, None
        if 1 <= m <= len(cands[i]):
            kind, ref, _, dist = cands[i][m - 1]
            kid = ref if kind == "db" else placed.get(ref)
            if kid is None:  # 짝이 된 앞 키워드가 판정 실패로 빠졌다
                continue
        if kid is None:
            kid = conn.execute(
                """
                insert into analysis.keyword (canonical, model, embedding) values (%s, %s, %s::extensions.vector)
                on conflict (model, canonical) do update set canonical = excluded.canonical returning id
                """, (raw, MODEL_NAME, lits[i])).fetchone()[0]
            method, dist = "new", None
        else:
            method = "llm"
        # 다른 프로세스가 같은 키를 먼저 넣었으면 그쪽을 따른다
        row = conn.execute(
            """
            insert into analysis.keyword_alias (alias_key, surface, keyword_id, method, distance)
            values (%s, %s, %s, %s, %s) on conflict (alias_key) do nothing returning keyword_id
            """, (key, raw, kid, method, dist)).fetchone()
        placed[i] = out[key] = row[0] if row else conn.execute(
            "select keyword_id from analysis.keyword_alias where alias_key = %s", (key,)).fetchone()[0]
    return out


def set_alias(surface: str, to: str | None = None) -> None:
    """사람이 정한 대응(method = 'manual', --rebuild에도 남는다). to가 없으면 그 표기를 독립된 대표 키워드로 떼어 낸다."""
    from . import connect

    conn = connect(autocommit=True)
    key = conn.execute("select analysis.keyword_key(%s)", (surface,)).fetchone()[0]
    if to is None:
        kid = conn.execute(
            """
            insert into analysis.keyword (canonical, model, embedding) values (%s, %s, %s::extensions.vector)
            on conflict (model, canonical) do update set canonical = excluded.canonical returning id
            """, (surface, MODEL_NAME, _lit(_encode([surface])[0]))).fetchone()[0]
    else:
        row = conn.execute("select keyword_id from analysis.keyword_alias where alias_key = analysis.keyword_key(%s)",
                           (to,)).fetchone()
        if not row:
            raise SystemExit(f"'{to}'는 사전에 없다")
        kid = row[0]
    conn.execute(
        """
        insert into analysis.keyword_alias (alias_key, surface, keyword_id, method) values (%s, %s, %s, 'manual')
        on conflict (alias_key) do update set keyword_id = excluded.keyword_id, method = 'manual', distance = null
        """, (key, surface, kid))
    # 옮기기 전 대표 키워드에 별칭이 하나도 안 남았으면 지운다 (남겨 두면 이후 키워드의 후보로 잡힌다)
    conn.execute("delete from analysis.keyword k where not exists "
                 "(select 1 from analysis.keyword_alias a where a.keyword_id = k.id)")
    canonical = conn.execute("select canonical from analysis.keyword where id = %s", (kid,)).fetchone()[0]
    print(f"keywords: {surface} → {canonical} (manual)")
    conn.close()


def register_quietly(conn, keywords: Iterable[str]) -> None:
    """라벨링 저장 중에 부른다. 임베딩 패키지가 없으면 한 번 알리고 건너뛴다 (나중에 normalize-keywords로 채움)."""
    global _warned
    if not available():
        if not _warned:
            print("keywords: sentence-transformers 없음 → 사전 등록 건너뜀 (pip install -r requirements-embed.txt 후 "
                  "python -m analysis normalize-keywords)")
            _warned = True
        return
    register(conn, keywords)


def run(*, rebuild: bool = False, candidate_distance: float = CANDIDATE_DISTANCE, chunk: int = 200) -> None:
    """기존 라벨의 키워드를 모두 사전에 넣는다. 자주 쓰인 표기부터 넣어서 그것이 대표 키워드가 되게 한다."""
    from . import connect

    conn = connect(autocommit=True)
    models = [m for (m,) in conn.execute("select distinct model from analysis.keyword").fetchall()]
    if models and models != [MODEL_NAME] and not rebuild:
        raise SystemExit(f"사전이 다른 모델({models})로 만들어졌다. --rebuild 로 다시 만든다.")
    if rebuild:
        conn.execute("delete from analysis.keyword_alias where method <> 'manual'")
        conn.execute("delete from analysis.keyword k where not exists "
                     "(select 1 from analysis.keyword_alias a where a.keyword_id = k.id)")
        print("keywords: 사전 비움 (manual 별칭과 그 대표 키워드는 유지)")
    rows = conn.execute(
        """
        select mode() within group (order by k), count(*) from (
            select unnest(keywords) as k from public.response_labels
            union all
            select unnest(topic_keywords) from analysis.page_label
        ) t
        where analysis.keyword_key(k) is not null
          and not exists (select 1 from analysis.keyword_alias a where a.alias_key = analysis.keyword_key(t.k))
        group by analysis.keyword_key(k) order by count(*) desc, 1  -- 키마다 가장 흔한 표기
        """).fetchall()
    print(f"keywords: 새 표기 {len(rows)}개 등록 (후보 거리 ≤ {candidate_distance}, 판정 {llm.MODEL})")
    for i in range(0, len(rows), chunk):
        register(conn, [k for k, _ in rows[i : i + chunk]], candidate_distance=candidate_distance)
        print(f"keywords: {min(i + chunk, len(rows))}/{len(rows)}")
    n_kw, n_alias, n_llm = conn.execute(
        "select (select count(*) from analysis.keyword), count(*), count(*) filter (where method = 'llm') "
        "from analysis.keyword_alias").fetchone()
    print(f"keywords: 대표 {n_kw}개, 별칭 {n_alias}개 (LLM 판정으로 묶인 것 {n_llm}개)")
    for canonical, uses, aliases in conn.execute(
            "select canonical, uses, aliases from analysis.v_keyword_groups "
            "where cardinality(aliases) > 1 order by uses desc limit 30").fetchall():
        print(f"  {canonical} ({uses}): {', '.join(aliases)}")
    conn.close()
