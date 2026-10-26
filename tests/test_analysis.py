"""분석 계층의 외부 호출 없는 점검. 실행: python tests/test_analysis.py (assert가 실패하면 비정상 종료)."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis import label  # noqa: E402
from analysis.fetch import extract_features  # noqa: E402
from analysis.metrics import bigrams, query_overlap  # noqa: E402

HTML = """<html lang="ko-KR"><head><title>온톨로지 구축 가이드</title>
<meta property="article:modified_time" content="2026-03-02T10:00:00+09:00">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Article","datePublished":"2025-12-01T09:00:00+09:00"},
 {"@type":["FAQPage","WebPage"]}]}
</script></head><body>
<h1>온톨로지 구축</h1><h2>비용</h2>
<p>도입 비용은 30% 줄고 구축 기간은 3배 빨라졌다고 한다. 담당자는 "현장 데이터를 먼저 정리하는 것이 핵심이다"라고 말했다.</p>
<img src="a.png" alt="구조도"><img src="b.png">
<table><tr><td>x</td></tr></table>
<iframe src="https://www.youtube.com/embed/abc"></iframe>
<a href="https://other.example.com/x">외부</a><a href="https://page.example.com/y">내부</a>
<blockquote>인용문 한 줄</blockquote></body></html>"""


def test_extract_features():
    f = extract_features(HTML, "https://page.example.com/post")
    assert f["lang"] == "ko"
    assert f["schema_types"] == ["Article", "FAQPage", "WebPage"], f["schema_types"]
    assert f["published_at"].year == 2025 and f["modified_at"].month == 3
    assert f["h_count"] == 2 and f["img_count"] == 2 and f["alt_ratio"] == 0.5
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
    sql = (ROOT / "db" / "analysis.sql").read_text(encoding="utf-8")
    for name, values in [("page_form", label.PAGE_FORMS), ("persona", label.PERSONAS), ("tone", label.TONES),
                         ("publisher_kind", label.PUBLISHER_KINDS), ("answer_form", label.ANSWER_FORMS),
                         ("stance", label.STANCES)]:
        line = re.search(rf"{name}\s+text check \({name} in \((.*?)\)\)", sql, re.S)
        assert line, name
        assert set(re.findall(r"'(\w+)'", line.group(1))) == set(values), name
    assert "'stats', 'case', 'source'" in sql
    schema_enums = label.PAGE_SCHEMA["properties"]["page_form"]["enum"]
    assert schema_enums == label.PAGE_FORMS


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
