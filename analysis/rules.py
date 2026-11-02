"""규칙으로 채우는 분석 축 (db/axis_definition.md).

페이지: Self Authored · Language · Visual → analysis.page_label
응답:   Answer Dominance → analysis.response_label
자동:   Markup · Freshness · Length · Lang Match → analysis.refresh_auto()

규칙이라 매번 전체를 다시 계산한다. labeled_by = 'manual' 행은 덮어쓰지 않는다.
Answer Dominance의 감성·입장 신호는 response_mentions.sentiment·stance를 쓰므로 label-responses를 먼저 돌린다.
"""

from __future__ import annotations

import re

from crawler.entities import _term_pattern

from . import DEFAULT_BATCH, connect
from .fetch import _guess_lang

LANGUAGES = ["ko", "en", "ja", "zh", "other"]
VISUALS = ["video", "table", "image", "text_only"]  # 정의서 값 7개 중 규칙으로 판정하는 것. diagram·infographic·screenshot은 이미지를 봐야 한다
DOMINANCE = ["dominant", "neutral", "recessive"]
_STANCE = {"recommended": 1, "neutral": 0, "mentioned_only": 0, "not_recommended": -1}
_SENTIMENT = {"positive": 1, "neutral": 0, "negative": -1}
_LIST_ITEM = re.compile(r"\s*(?:#+\s*)?(\d+)[.)]\s")


# ---------- 페이지 ----------
def visual(has_video: bool | None, table_count: int | None, img_count: int | None) -> str | None:
    if img_count is None:  # HTML을 받지 못한 페이지
        return None
    # ponytail: 정의서는 '가장 비중 큰' 요소를 고르라고 하지만, 개수 비중은 보지 않고 동률 순서(video → table → image)만 쓴다.
    # img_count는 본문 이미지만 센 값이다 (fetch.extract_features).
    if has_video:
        return "video"
    if table_count:
        return "table"
    return "image" if img_count else "text_only"


def language(lang: str | None, text: str | None) -> str | None:
    code = (lang or "")[:2].lower() or (_guess_lang(text) if text else None)
    return code if code is None or code in LANGUAGES else "other"


def is_self(domain: str | None, self_domains: list[str]) -> bool | None:
    # 도메인으로만 판정한다. 외부 플랫폼(네이버 블로그 등)에는 자사 공식 계정이 없다.
    if not domain:
        return None
    return any(domain == d or domain.endswith("." + d) for d in self_domains)


def label_pages(conn) -> None:
    self_domains = [d.lower().removeprefix("www.") for (d,) in conn.execute(
        "select unnest(domains) from entities where role = 'self'").fetchall()]
    rows = conn.execute(
        "select id, domain, lang, left(text, 2000), has_video, table_count, img_count from pages").fetchall()
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into analysis.page_label (page_id, self_authored, language, visual, labeled_by)
            values (%s, %s, %s, %s, 'rule')
            on conflict (page_id) do update
                set self_authored = excluded.self_authored, language = excluded.language, visual = excluded.visual
                where analysis.page_label.labeled_by <> 'manual'
                  and (analysis.page_label.self_authored, analysis.page_label.language, analysis.page_label.visual)
                      is distinct from (excluded.self_authored, excluded.language, excluded.visual)
            """,  # labeled_by는 바꾸지 않는다. llm이 채운 행에 규칙 축만 덧쓴다
            [(pid, is_self(domain, self_domains), language(lang, text), visual(video, tables, imgs))
             for pid, domain, lang, text, video, tables, imgs in rows])
    print(f"rules: {len(rows)} pages (self_authored, language, visual)")


# ---------- 응답 ----------
def _sign(a, b) -> int:
    return (a > b) - (a < b)


def find_mention(text: str, terms: list[str], tone: int | None) -> dict | None:
    """답변 본문에서 엔티티(이름·별칭)의 첫 등장 위치·목록 번호·언급 줄 수를 구한다. 본문에 없으면 None."""
    patterns = [_term_pattern(t) for t in terms if t]  # 크롤러가 언급을 감지할 때와 같은 매칭 규칙
    offsets = [m.start() for p in patterns if (m := p.search(text))]
    if not offsets:
        return None
    offset = min(offsets)
    start = text.rfind("\n", 0, offset) + 1
    item = _LIST_ITEM.match(text, start)
    return {
        "name": terms[0], "offset": offset, "line": text.count("\n", 0, offset),
        "list_rank": int(item[1]) if item and item.end() <= offset else None,
        # ponytail: 언급 분량을 글자 수 대신 '엔티티가 나오는 줄 수'로 센다. 줄바꿈 없는 긴 답변이면 문장 단위로 바꾼다.
        "volume": sum(1 for line in text.splitlines() if any(p.search(line) for p in patterns)),
        "tone": tone,
    }


def dominance(self_m: dict | None, competitors: list[dict]) -> tuple[str | None, str]:
    """정의서 24번의 판정 순서를 따른다. 반환: (answer_dominance, 판정 근거)."""
    if not competitors:
        return None, "no competitor"
    if self_m is None:
        return "recessive", "competitor only"
    c = max(competitors, key=lambda m: (m["volume"], -m["offset"]))  # 가장 두드러진 경쟁사: 많이 언급, 동률이면 먼저 등장
    s = self_m
    signals = {
        "order": 0 if s["line"] == c["line"] else _sign(c["offset"], s["offset"]),
        "list_rank": _sign(c["list_rank"], s["list_rank"]) if s["list_rank"] and c["list_rank"] else 0,
        "volume": _sign(s["volume"], c["volume"]),
        "tone": _sign(s["tone"], c["tone"]) if s["tone"] is not None and c["tone"] is not None else 0,
    }
    total = sum(signals.values())
    value = "dominant" if total >= 2 else "recessive" if total <= -2 else "neutral"
    return value, f"vs {c['name']}: " + " ".join(f"{k}={v:+d}" for k, v in signals.items())


def label_dominance(conn, batch: str, response_ids: list[int] | None = None) -> None:
    texts = dict(conn.execute(
        "select id, response_text from responses where batch = %s and response_text is not null "
        "and (%s::bigint[] is null or id = any(%s))", (batch, response_ids, response_ids)).fetchall())
    mentions: dict[int, list[tuple]] = {rid: [] for rid in texts}
    for rid, role, name, aliases, sentiment, stance in conn.execute(
            """
            select m.response_id, e.role::text, e.name, e.aliases, m.sentiment, m.stance
            from response_mentions m join entities e on e.id = m.entity_id join responses r on r.id = m.response_id
            where r.batch = %s and e.role in ('self', 'competitor')
            """, (batch,)).fetchall():
        if rid in mentions:
            mentions[rid].append((role, name, aliases, sentiment, stance))

    rows = []
    for rid, text in texts.items():
        self_m, competitors = None, []
        for role, name, aliases, sentiment, stance in mentions[rid]:
            tone = None if sentiment is None and stance is None else _STANCE.get(stance, 0) + _SENTIMENT.get(sentiment, 0)
            m = find_mention(text, [name, *aliases], tone)
            if m is None:
                continue
            if role == "self":
                self_m = m
            else:
                competitors.append(m)
        rows.append((rid, *dominance(self_m, competitors)))

    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into analysis.response_label (response_id, answer_dominance, labeled_by, rationale)
            values (%s, %s, 'rule', %s)
            on conflict (response_id) do update
                set answer_dominance = excluded.answer_dominance, rationale = excluded.rationale,
                    labeled_by = 'rule', labeled_at = now()
                where (analysis.response_label.answer_dominance, analysis.response_label.rationale)
                      is distinct from (excluded.answer_dominance, excluded.rationale)
            """, rows)  # manual 행은 analysis.protect_manual 트리거가 건너뛴다
    counts = {v: sum(1 for r in rows if r[1] == v) for v in [*DOMINANCE, None]}
    print(f"rules: {len(rows)} responses (answer_dominance {counts})")


def run(*, batch: str = DEFAULT_BATCH, response_ids: list[int] | None = None) -> None:
    conn = connect(autocommit=True)
    conn.execute("select analysis.refresh_auto()")
    print("rules: auto axes refreshed (markup, freshness, length, lang_match)")
    label_pages(conn)  # 페이지 규칙은 가벼워서 표본 실행에서도 전체를 다시 계산한다
    label_dominance(conn, batch, response_ids)
    conn.close()
