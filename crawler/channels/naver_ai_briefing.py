"""네이버 AI 브리핑 (search.naver.com, 구 Cue:).

비로그인으로 측정한다. 통합검색 결과 맨 위에 AI 브리핑이 뜨는지가 측정 대상이라, 쿼리를 그대로 검색하고
브리핑이 없으면 answer_present=False로 기록한다 (노출률 분모).

- 브리핑 영역: .fds-aib-expandable-container (상위 .api_subject_bx). 처음엔 접혀 있어 '펼쳐서 더보기'를 누른다.
- 구성: 머리글(.fds-aib-header) → 상단 출처 카드(.fds-multimedia-container) → 마크다운 본문(.fds-markdown-*)
  → 출처 목록('출처 N건 전체보기', a.fds-aib-color-bg-99) → 면책 문구.
- 본문 문장 끝 출처 칩(.fds-overlay-chip, 예: 'ustracloud.com +1')은 텍스트에서 뺀다.
- 인용은 출처 목록 → 상단 카드 → 그 밖의 외부 링크 순서로, 실제 URL이 <a href>에 그대로 있다.
- 하위 검색어(fanout)·모델명은 노출되지 않는다.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from playwright.sync_api import Page

from ..base import BaseCrawler, PersonalizationError
from ..models import CapturedResponse, Citation, Extraction, Query

_CONTAINER = ".fds-aib-expandable-container"
_HEADER = ".fds-aib-header"
_SOURCE_LIST = "a.fds-aib-color-bg-99[href]"
_EXPAND_TEXTS = ("펼쳐서 더보기",)
_SOURCE_ALL_TEXTS = ("전체보기",)
_IGNORE_HOSTS = ("search.naver.com", "help.naver.com", "nid.naver.com")
_APPEAR_TIMEOUT_MS = 10_000

# 최상위 마크다운 블록만 (중첩된 li·td 중복 제외), 칩을 숨긴 상태에서 읽는다
_TEXT_JS = """root => {
    const chips = [...root.querySelectorAll('.fds-overlay-chip')];
    chips.forEach(c => { c.dataset.geoDisplay = c.style.display; c.style.display = 'none'; });
    const sel = '.fds-markdown-p, .fds-markdown-h, .fds-markdown-ul, .fds-markdown-ol, .fds-markdown-table-container, .fds-markdown-blockquote, .fds-markdown-pre';
    const blocks = [...root.querySelectorAll(sel)].filter(e => !e.parentElement.closest(sel));
    const text = blocks.map(e => e.innerText.trim()).filter(Boolean).join('\\n');
    chips.forEach(c => { c.style.display = c.dataset.geoDisplay; delete c.dataset.geoDisplay; });
    return text;
}"""


class NaverAIBriefingCrawler(BaseCrawler):
    engine = "naver_ai_briefing"
    start_url = "https://search.naver.com/"
    requires_login = False
    answer_timeout_s = 90.0
    delay_between_runs_s = (10.0, 25.0)

    def open_fresh(self, page: Page, query: Query) -> None:
        page.goto(f"https://search.naver.com/search.naver?where=nexearch&query={quote(query.text)}",
                  wait_until="domcontentloaded")

    def is_logged_in(self, page: Page) -> bool:
        # 페이지에는 로그인·로그아웃 링크가 템플릿으로 둘 다 있어 세션 쿠키로 본다
        return any(c["name"] in ("NID_AUT", "NID_SES") for c in page.context.cookies("https://naver.com"))

    def blocked_reason(self, page: Page) -> str | None:
        if reason := super().blocked_reason(page):
            return reason
        if "nid.naver.com" in page.url or page.get_by_text("자동입력 방지", exact=False).count():
            return "naver_captcha"
        return None

    def check_personalization(self, page: Page) -> dict[str, Any]:
        if self.is_logged_in(page):
            raise PersonalizationError(
                "naver_ai_briefing: 로그인 상태 — 비로그인으로만 측정한다. data/profiles/naver_ai_briefing 를 지우고 다시 실행")
        return {"logged_in": False}

    def submit(self, page: Page, query: Query) -> None:
        page.locator("#nx_query, input[name=query]").first.wait_for(state="attached", timeout=30_000)

    def wait_until_done(self, page: Page) -> None:
        container = page.locator(_CONTAINER)
        try:
            page.locator(_HEADER).first.wait_for(state="attached", timeout=_APPEAR_TIMEOUT_MS)
            container.first.wait_for(state="attached", timeout=_APPEAR_TIMEOUT_MS)
        except Exception:
            return  # AI 브리핑 없음 → extract에서 answer_present=False
        self.wait_for_stable(page, container, quiet_s=2.5)
        for label in _EXPAND_TEXTS:
            btn = container.first.get_by_text(label, exact=False)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(1500)
        for label in _SOURCE_ALL_TEXTS:
            btn = container.first.get_by_text(label, exact=False)
            if btn.count() and btn.first.is_visible():
                try:
                    btn.first.click(timeout=5_000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass  # 출처가 이미 다 보이면 버튼이 없다
        self.wait_for_stable(page, container, quiet_s=2.0)

    def extract(self, page: Page, captured: list[CapturedResponse]) -> Extraction:
        container = page.locator(_CONTAINER)
        if not container.count():
            return Extraction(response_text=None, answer_present=False, extra={"briefing": False})
        root = container.first
        text = root.evaluate(_TEXT_JS).strip()

        citations: dict[str, Citation] = {}
        sources = root.locator(_SOURCE_LIST).evaluate_all(
            "els => els.map(a => ({url: a.href, lines: a.innerText.split('\\n').map(s => s.trim()).filter(Boolean)}))")
        for s in sources:  # '도메인/매체\n제목\n새 창 열림'
            lines = [x for x in s["lines"] if x != "새 창 열림"]
            title = lines[1] if len(lines) > 1 else (lines[0] if lines else None)
            citations.setdefault(s["url"], Citation(url=s["url"], title=title))
        for c in self.links_in(root):
            if any(h in (urlsplit(c.url).hostname or "") for h in _IGNORE_HOSTS):
                continue
            title = (c.title or "").split("\n")[0].strip() or None
            citations.setdefault(c.url, Citation(url=c.url, title=title))

        return Extraction(
            response_text=text or None,
            answer_present=bool(text),
            response_html=root.evaluate("e => (e.closest('.api_subject_bx') || e).outerHTML"),
            citations=list(citations.values()),
            extra={"briefing": bool(text), "source_chips": root.locator(".fds-overlay-chip").count(),
                   "source_list": len(sources)},
        )

    def extra_settings(self, page: Page) -> dict[str, Any]:
        return {"logged_in": self.is_logged_in(page)}
