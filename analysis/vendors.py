"""답변에 나온 업체 추출 (analysis.response_vendor) — 등록되지 않은 경쟁사·새 시장 참여자를 찾는다.

crawler.entities.EntityMatcher(등록된 이름·별칭 정규식)는 entities에 있는 업체만 잡는다. 여기서는 Haiku가 답변에서
업체를 직접 뽑고, 뽑힌 이름을 EntityMatcher에 넣어 entity_id를 붙인다. kind = 'vendor'인데 entity_id가 null이면 미등록 후보.
같은 답변에서 정규식에는 잡혔는데 여기 없는 엔티티는 정규식 오탐 후보다 (analysis.v_response_vendor_check).
"""

from __future__ import annotations

import json
import re
import unicodedata

from crawler.entities import Entity, EntityMatcher

from . import DEFAULT_BATCH, connect, llm
from .label import ANSWER_CHARS, VENDOR_KINDS as KINDS, VENDOR_RULES, VENDORS_FIELD, _arr, _enum, _obj

# v2: 회사 단위로 묶고(제품은 회사 아래로) 답변 속 표기를 모두 받아 엔티티 매칭에 쓴다
# v3: 질문에 나온 회사는 제외 (답변이 새로 꺼낸 업체만), 본문 대조(verify)로 지어낸 이름 제거
VERSION = "v3"  # 전체 실행은 label.label_responses가 답변 라벨과 한 호출로 뽑는다 (규칙 문구는 같아 버전 유지)

SYSTEM = "AI 검색 엔진의 답변에서 회사 이름을 뽑는다. 질문과 답변이 주어진다.\n\n" + VENDOR_RULES
SCHEMA = _obj({"vendors": VENDORS_FIELD})


def matcher(conn) -> EntityMatcher:
    return EntityMatcher([Entity(i, n, tuple(a), str(r), tuple(d)) for i, n, a, r, d in conn.execute(
        "select id, name, aliases, role, domains from entities").fetchall()])


def entity_id(m: EntityMatcher, names: list[str]) -> int | None:
    """회사 이름과 답변 속 표기들 중 처음으로 등록 별칭에 걸리는 엔티티."""
    # ponytail: 표기 안의 별칭 부분 일치를 쓴다. 'KT DS' → KT 처럼 상위 회사로 붙을 수 있다. 오매칭이 보이면 전체 일치로 좁힌다
    for n in names:
        if found := m.mentions(n):
            return found[0].entity.id
    return None


def _norm(s: str) -> str:
    """대조용 표기: NFC · 소문자 · 공백/구두점 제거 (analysis.keyword_key와 같은 규칙)."""
    return re.sub(r"[\s\-_·・.,/]+", "", unicodedata.normalize("NFC", s)).casefold()


def verify(vendors: list[dict], question: str, answer: str, m: EntityMatcher,
           question_entities: set[int]) -> tuple[list[dict], int, int]:
    """LLM이 뽑은 업체를 본문과 대조한다. (남은 업체, 지어낸 수, 질문 회사 수)

    - 표기(name·spellings) 중 답변 본문에 실제로 있는 것만 남기고, 하나도 없으면 지어낸 이름으로 보고 버린다.
    - 표기가 질문에 있거나, 매칭된 엔티티가 질문의 엔티티(query_entities)면 질문에 나온 회사로 보고 버린다.
    """
    # ponytail: 부분 문자열 대조라 아주 짧은 표기('AI')는 아무 데나 걸린다. 그런 표기가 문제 되면 최소 길이를 둔다
    q, a = _norm(question), _norm(answer)
    out, made_up, asked = [], 0, 0
    for v in vendors:
        names = [s for s in dict.fromkeys([v["name"], *v["spellings"]]) if _norm(s)]
        seen = [s for s in names if _norm(s) in a]
        if not seen:
            made_up += 1
            continue
        eid = entity_id(m, seen)
        if eid in question_entities or any(_norm(s) in q for s in names):
            asked += 1
            continue
        out.append({**v, "spellings": seen, "entity_id": eid})
    return out, made_up, asked


def run(*, batch: str = DEFAULT_BATCH, limit: int | None = None, response_ids: list[int] | None = None,
        version: str = VERSION, use_batch_api: bool = False) -> None:
    conn = connect(autocommit=True)
    rows = conn.execute(
        """
        select r.id, q.query_text, r.response_text from responses r join queries q on q.id = r.query_id
        where r.batch = %s and r.response_text is not null and (%s::bigint[] is null or r.id = any(%s))
          and not exists (select 1 from analysis.response_vendor v where v.response_id = r.id and v.label_version = %s)
        order by r.id limit %s
        """, (batch, response_ids, response_ids, version, limit)).fetchall()
    m = matcher(conn)
    text = {i: (q, t) for i, q, t in rows}
    asked_entities: dict[int, set[int]] = {i: set() for i in text}
    for rid, eid in conn.execute(
            "select r.id, qe.entity_id from responses r join query_entities qe on qe.query_id = r.query_id "
            "where r.id = any(%s)", (list(text),)).fetchall():
        asked_entities[rid].add(eid)
    dropped = {"made_up": 0, "asked": 0}

    def save(results: dict[str, dict]) -> None:
        params = []
        for k, r in results.items():
            rid = int(k)
            kept, made_up, asked = verify(r["vendors"], *text[rid], m, asked_entities[rid])
            dropped["made_up"] += made_up
            dropped["asked"] += asked
            params.append((rid, version, llm.MODEL, json.dumps(kept, ensure_ascii=False)))
        with conn.cursor() as cur:
            cur.executemany(
                "insert into analysis.response_vendor (response_id, label_version, label_model, vendors) "
                "values (%s, %s, %s, %s) on conflict do nothing", params)

    print(f"discover-vendors: {len(rows)} responses")
    llm.run({str(i): f"[질문] {q}\n[답변]\n{t[:ANSWER_CHARS]}" for i, q, t in rows}, system=SYSTEM, schema=SCHEMA,
            save=save, max_tokens=3000, use_batch_api=use_batch_api)
    print(f"discover-vendors: 본문에 없어 버림 {dropped['made_up']}, 질문에 나온 회사라 버림 {dropped['asked']}")
    conn.close()


if __name__ == "__main__":
    m = EntityMatcher([Entity(1, "솔트룩스", ("Saltlux",), "competitor", ()), Entity(2, "KT", (), "competitor", ())])
    assert entity_id(m, ["솔트룩스"]) == 1 and entity_id(m, ["솔트", "Saltlux"]) == 1  # 표기 중 하나만 걸려도 된다
    assert entity_id(m, ["메타넷디엘"]) is None and entity_id(m, ["kt"]) is None  # 짧은 대문자 약어는 대소문자 구분
    vs = [{"name": "솔트룩스", "spellings": ["솔트룩스"], "kind": "vendor"},           # 본문에 없음 → 지어냄
          {"name": "Amazon Web Services", "spellings": ["Guard Duty", "Macie"], "kind": "vendor"},  # 표기 중 일부만 본문에
          {"name": "이에이트", "spellings": ["이에이트"], "kind": "vendor"},             # 질문에 나옴
          {"name": "KT", "spellings": ["KT"], "kind": "vendor"}]                     # 질문 엔티티(id 2)
    kept, made_up, asked = verify(vs, "이에이트 말고 추천해줘", "이에이트 외에 GuardDuty 와 KT 가 있다", m, {2})
    assert (made_up, asked) == (1, 2) and kept == [{**vs[1], "spellings": ["Guard Duty"], "entity_id": None}]
    print("ok")
