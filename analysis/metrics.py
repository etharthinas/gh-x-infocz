"""질문-페이지 정합성(query_overlap) 계산. Moore & Dunne (2026)에서 인용 횟수를 가장 크게 설명한 변수다."""

from __future__ import annotations

import re

from . import DEFAULT_BATCH, connect

CHUNK = 500


def bigrams(s: str) -> set[str]:
    s = re.sub(r"\W+", "", s.lower())
    return {s[i : i + 2] for i in range(len(s) - 1)}


def query_overlap(query: str, title: str | None, text: str | None) -> float | None:
    """질문의 문자 바이그램 중 (제목 + 본문 앞부분)에 나타나는 비율.

    ponytail: 형태소 분석 없이 문자 바이그램을 쓴다. 한국어 조사 때문에 점수가 조금 부풀 수 있다.
    정밀도가 부족하면 kiwipiepy 명사 추출로 바꾼다. Jaccard 대신 질문 기준 비율을 쓰는 이유는,
    질문(수십 자)과 본문(수천 자)의 길이 차이가 커서 Jaccard는 거의 항상 0에 가깝기 때문이다.
    """
    q = bigrams(query)
    if not q:
        return None
    return len(q & bigrams(f"{title or ''} {text or ''}")) / len(q)


def run(*, batch: str = DEFAULT_BATCH, response_ids: list[int] | None = None) -> None:
    conn = connect(autocommit=True)
    last_id, done = 0, 0
    while True:
        rows = conn.execute(
            """
            select p.id, p.title, p.text, array_agg(c.id), array_agg(q.query_text)
            from pages p
            join citations c on c.page_id = p.id
            join responses r on r.id = c.response_id
            join queries q on q.id = r.query_id
            where p.id > %s and p.text is not null and c.query_overlap is null and r.batch = %s
              and (%s::bigint[] is null or r.id = any(%s))
            group by p.id order by p.id limit %s
            """,
            (last_id, batch, response_ids, response_ids, CHUNK),
        ).fetchall()
        if not rows:
            break
        updates = [
            (query_overlap(qt, title, text), cid)
            for _, title, text, cids, qts in rows
            for cid, qt in zip(cids, qts)
        ]
        with conn.cursor() as cur:
            cur.executemany("update citations set query_overlap = %s where id = %s", updates)
        last_id, done = rows[-1][0], done + len(updates)
        print(f"overlap: {done} citations updated (page id <= {last_id})")
    conn.close()
