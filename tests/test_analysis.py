"""분석 계층의 외부 호출 없는 점검. 실행: python tests/test_analysis.py (assert가 실패하면 비정상 종료)."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis import jev, label, rules  # noqa: E402
from analysis.fetch import extract_features  # noqa: E402
from analysis.metrics import bigrams, query_overlap  # noqa: E402

HTML = """<html lang="ko-KR"><head><title>온톨로지 구축 가이드</title>
<meta property="article:modified_time" content="2026-03-02T10:00:00+09:00">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Article","datePublished":"2025-12-01T09:00:00+09:00"},
 {"@type":["FAQPage","WebPage"]}]}
</script></head><body>
<header><img src="/logo.png" alt="로고"></header><article>
<h1>온톨로지 구축</h1><h2>비용</h2>
<p>도입 비용은 30% 줄고 구축 기간은 3배 빨라졌다고 한다. 담당자는 "현장 데이터를 먼저 정리하는 것이 핵심이다"라고 말했다.
""" + "온톨로지는 현장 데이터의 관계를 정의하는 작업이다. " * 20 + """</p>
<img src="https://page.example.com/a.png" alt="구조도">
<p>""" + "구축 단계마다 검증 절차를 둔다. " * 20 + """</p>
<img src="https://page.example.com/b.png">
<table><tr><td>x</td></tr></table></article>
<iframe src="https://www.youtube.com/embed/abc"></iframe>
<a href="https://other.example.com/x">외부</a><a href="https://page.example.com/y">내부</a>
<blockquote>인용문 한 줄</blockquote></body></html>"""


def test_extract_features():
    f = extract_features(HTML, "https://page.example.com/post")
    assert f["lang"] == "ko"
    assert f["schema_types"] == ["Article", "FAQPage", "WebPage"], f["schema_types"]
    assert f["published_at"].year == 2025 and f["modified_at"].month == 3
    assert f["h_count"] == 2 and f["img_count"] == 2 and f["alt_ratio"] == 0.5, (f["img_count"], f["alt_ratio"])  # 헤더 로고는 세지 않는다
    assert f["table_count"] == 1 and f["has_video"] is True
    assert f["outlink_count"] == 1, f["outlink_count"]  # 같은 도메인 링크는 세지 않는다
    assert f["stat_count"] >= 2 and f["quote_count"] >= 2, (f["stat_count"], f["quote_count"])
    assert extract_features("", "https://x.example.com") == {}


def test_overlap():
    assert query_overlap("", "a", "b") is None
    assert query_overlap("온톨로지 구축 비용", "온톨로지 구축 비용 안내", None) == 1.0
    assert query_overlap("온톨로지 구축 비용", "전혀 다른 내용", "무관한 본문") < 0.3
    assert 0 < query_overlap("graph database cost", "Graph database pricing", "") < 1
    assert bigrams("a") == set()


def test_label_values_match_sql():
    sql = (ROOT / "db" / "crawl_ext.sql").read_text(encoding="utf-8")
    for name, values in [("page_form", label.PAGE_FORMS), ("persona", label.PERSONAS), ("tone", label.TONES),
                         ("publisher_kind", label.PUBLISHER_KINDS), ("answer_form", label.ANSWER_FORMS),
                         ("stance", label.STANCES)]:
        line = re.search(rf"{name}\s+text check \({name} in \((.*?)\)\)", sql, re.S)
        assert line, name
        assert set(re.findall(r"'(\w+)'", line.group(1))) == set(values), name
    assert "'stats', 'case', 'source'" in sql
    schema_enums = label.PAGE_SCHEMA["properties"]["page_form"]["enum"]
    assert schema_enums == label.PAGE_FORMS


def test_rule_values_match_sql():
    sql = (ROOT / "db" / "analysis.sql").read_text(encoding="utf-8")

    def values(name):
        line = re.search(rf"{name}\s+text check \({name} in \((.*?)\)\)", sql, re.S)
        assert line, name
        return set(re.findall(r"'(\w+)'", line.group(1)))

    assert set(rules.VISUALS) <= values("visual")
    assert set(rules.LANGUAGES) == values("language")
    assert set(rules.DOMINANCE) == values("answer_dominance")

    formats = {v for tier in jev.FORMAT_TIERS for v in tier} | {"other"}
    assert formats == set(re.findall(r"\('(\w+)', '\w+'\)", sql.split("insert into analysis.format_genre_map")[1]
                                     .split("on conflict")[0]))
    assert set(jev.TOPICS) | {"brand"} == values("topic")
    for name, options in [("industry", jev.INDUSTRIES), ("persona", jev.PERSONAS), ("tone", jev.TONES),
                          ("evidence", [*jev.EVIDENCE, "none"]), ("publisher", jev.PUBLISHERS),
                          ("passage_type", [*jev.PASSAGES, "other"]), ("authority", jev.AUTHORITY)]:
        assert set(options) == values(name), name
    assert len(jev.AUTHORITY) == len(jev.AUTHORITY_LEVELS)
    for name, q in [("entity_focus", jev.page_questions("a", "b")["entity_focus"]),
                    ("brand_role", jev.domain_questions("a", "b")["brand_role"])]:
        assert {jev.as_role(o) for o in q.criteria} == values(name), name
        assert "self" not in q.criteria  # Jev가 '페이지 자신의 회사'로 읽지 않도록 infocz로 묻는다


def test_fix_infoseeds():
    assert jev.fix_infoseeds("Infoseeds launches GOS", "InfoSeeds said...") == ["InfoCZ launches GOS", "InfoCZ said..."]
    assert jev.fix_infoseeds("Info Seeds title", "graph security AXIOM") == ["InfoCZ title", "graph security AXIOM"]
    assert jev.fix_infoseeds("Infoseeds Technologies, Pune", "IT services") == ["Infoseeds Technologies, Pune", "IT services"]
    assert jev.fix_infoseeds("Infoseeds", "gos is lowercase") == ["Infoseeds", "gos is lowercase"]  # GOS는 대문자만
    assert jev.fix_infoseeds(None, "no mention") == [None, "no mention"]


def test_jev_pick():
    tiers = [["press_release", "faq"], ["how_to"], ["news"]]
    assert jev.pick({"news": 0.95, "press_release": 0.8}, tiers, "other") == "press_release"  # 앞 묶음 우선
    assert jev.pick({"press_release": 0.75, "faq": 0.9}, tiers, "other") == "faq"  # 같은 묶음은 확률 순
    assert jev.pick({"how_to": 0.6, "news": 0.65}, tiers, "other") == "other"  # YES 미만은 버린다


def test_page_rules():
    assert rules.visual(None, None, None) is None
    assert rules.visual(True, 2, 5) == "video" and rules.visual(False, 1, 5) == "table"
    assert rules.visual(False, 0, 3) == "image" and rules.visual(False, 0, 0) == "text_only"
    assert rules.language("ko", None) == "ko" and rules.language("zh-cn", None) == "zh"
    assert rules.language("de", None) == "other" and rules.language(None, None) is None
    assert rules.language(None, "온톨로지 구축 가이드") == "ko"
    self_domains = ["infocz.ai", "infocz.co.kr"]
    assert rules.is_self("blog.infocz.ai", self_domains) and not rules.is_self("notinfocz.ai", self_domains)
    assert rules.is_self(None, self_domains) is None


def test_dominance():
    text = ("추천 업체는 다음과 같습니다.\n1. **인포시즈** - 온톨로지 전문. 인포시즈는 폐쇄망 구축 경험이 많습니다.\n"
            "2. Palantir - 글로벌 플랫폼.\n인포시즈는 국내 레퍼런스가 많습니다.")
    s = rules.find_mention(text, ["인포시즈", "Infocz"], tone=2)
    c = rules.find_mention(text, ["팔란티어", "Palantir"], tone=0)
    assert s["list_rank"] == 1 and c["list_rank"] == 2 and s["volume"] == 2 and c["volume"] == 1
    assert rules.find_mention(text, ["SAP"], None) is None
    assert rules.find_mention("Palantir 3. 이후", ["Palantir"], None)["list_rank"] is None  # 엔티티 뒤의 번호는 무시

    assert rules.dominance(s, []) == (None, "no competitor")
    assert rules.dominance(None, [c]) == ("recessive", "competitor only")
    value, why = rules.dominance(s, [c])
    assert value == "dominant", why  # 순서·순위·분량·감성 모두 +1
    assert rules.dominance(c, [s])[0] == "recessive"
    same_line = rules.find_mention("인포시즈와 Palantir를 비교하면", ["인포시즈"], None)
    rival = rules.find_mention("인포시즈와 Palantir를 비교하면", ["Palantir"], None)
    assert rules.dominance(same_line, [rival]) == ("neutral", "vs Palantir: order=+0 list_rank=+0 volume=+0 tone=+0")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
