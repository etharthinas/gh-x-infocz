"""Google AI 개요 (AI Overviews, google.com/search).

비로그인으로만 측정한다 (Gemini와 같은 이유: Google 세션은 옮기면 무효화되고 검색 기록 맞춤화를 확인할 수 없다).
일반 검색 결과 페이지에 AI 개요가 뜨는지가 측정 대상이라, 쿼리를 그대로 검색하고 AI 개요가 없으면
answer_present=False로 기록한다 (노출률 분모). 같은 쿼리도 실행마다 뜨기도 하고 안 뜨기도 한다.

- AI 개요 영역: #m-x-content 안의 [data-scope-id=turn]. 답변이 끝나면 data-complete=true.
  자식은 [제목('AI 개요'/'AI Overview'), 본문, 면책 문구, …] 순서다.
- 본문 안 출처 칩(span[data-animation-atomic] 안 button[data-icl-uuid])과 오른쪽 출처 패널(ul)은 텍스트에서 뺀다.
- 링크는 모두 google.com/goto?url=… 또는 /url?…&url=… 형태의 불투명 리디렉트다. 페이지 쿠키로 요청해
  302 Location에서 실제 URL을 얻는다 (토큰은 배치마다 달라서 HTML 주석의 하이드레이션 데이터와 짝짓기 어렵다).
- 하위 검색어(fanout)·모델명은 노출되지 않는다.
"""

from __future__ import annotations

import logging
import random
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page

from ..base import BaseCrawler, CrawlerError, PersonalizationError
from ..models import CapturedResponse, Citation, Extraction, Query

log = logging.getLogger(__name__)

_ROOT = "#m-x-content"
_TURN = "#m-x-content [data-scope-id=turn]"
_HEADINGS = ("AI 개요", "AI Overview")
_SEARCH_BOX = "textarea[name=q], input[name=q]"
_RESULTS = "#rso, #search"  # 일반 검색 결과 영역
_CAPTCHA_TEXTS = ("비정상적인 트래픽", "unusual traffic", "로봇이 아닙니다", "not a robot")
_REDIRECT = ("/goto", "/url")
_TRAILING_BUTTONS = ("", "모두 표시", "Show all")
_IGNORE_HOSTS = ("google.com", "google.co.kr", "gstatic.com", "googleusercontent.com", "youtube.com/redirect")
# AI 개요가 늦게 붙는 경우가 있어 검색 결과가 뜬 뒤 이만큼은 기다린다
_APPEAR_TIMEOUT_MS = 15_000

# 본문 텍스트를 읽을 때 잠깐 숨기는 요소: 출처 칩, 오른쪽 출처 패널
_HIDE_JS = """(turn, hide) => {
    const marked = turn.querySelectorAll('[data-geo-hidden]');
    if (!hide) { marked.forEach(e => { e.style.display = e.dataset.geoHidden; delete e.dataset.geoHidden; }); return; }
    const targets = [...turn.querySelectorAll('[data-icl-uuid]')].map(b => b.closest('[data-animation-atomic]') || b);
    const cards = [...turn.querySelectorAll('a[aria-label]')]
        .filter(a => /새 탭에서 열립니다|opens in (a )?new tab/i.test(a.getAttribute('aria-label')));
    let panel = cards[0];
    while (panel && panel !== turn && !cards.every(c => panel.contains(c))) panel = panel.parentElement;
    if (panel && panel !== turn) targets.push(panel);
    for (const e of targets) { e.dataset.geoHidden = e.style.display; e.style.display = 'none'; }
}"""
# 접힌 본문의 '더보기' 버튼 (답변 옵션 메뉴 '이 대답에 대한 옵션 더보기'는 aria-label이 있어 제외)
_EXPAND_JS = """turn => {
    const b = [...turn.querySelectorAll('[role=button][aria-expanded=false]')]
        .find(e => !e.getAttribute('aria-label') && /^(더보기|자세히 보기|show more)$/i.test(e.innerText.trim()));
    if (b) { b.click(); return true; }
    return false;
}"""


class GoogleAIOCrawler(BaseCrawler):
    engine = "google_aio"
    start_url = "https://www.google.com/"
    requires_login = False
    answer_timeout_s = 90.0
    # 검색은 /sorry 차단이 빠르다. 결과 URL 직행·짧은 간격은 서버 IP뿐 아니라 가정용 IP도 바로 막혔다 (2026-10)
    delay_between_runs_s = (60.0, 120.0)

    def open_fresh(self, page: Page, query: Query) -> None:
        # 결과 URL로 바로 가면 자동화로 보여 /sorry가 빨리 뜬다. 사람처럼 홈에서 검색창에 입력한다
        page.goto(f"https://www.google.com/?hl={self.language}", wait_until="domcontentloaded")
        page.locator(_SEARCH_BOX).first.wait_for(state="visible", timeout=30_000)
        page.wait_for_timeout(random.randint(1500, 4000))

    def blocked_reason(self, page: Page) -> str | None:
        if reason := super().blocked_reason(page):
            return reason
        # 실제 Chrome에서는 /sorry로 가지 않고 /search 주소 그대로 캡차 페이지가 뜬다
        if page.locator("#captcha-form, form[action*='sorry']").count() or any(
                page.get_by_text(t, exact=False).count() for t in _CAPTCHA_TEXTS):
            return "google_captcha"
        return None

    def is_logged_in(self, page: Page) -> bool:
        return page.locator("a[href*='accounts.google.com/ServiceLogin']").count() == 0

    def check_personalization(self, page: Page) -> dict[str, Any]:
        if self.is_logged_in(page):
            raise PersonalizationError(
                "google_aio: 로그인 상태 — 검색 기록 맞춤화가 섞일 수 있어 비로그인으로만 측정한다. "
                "data/profiles/google_aio 를 지우고 다시 실행")
        return {"logged_in": False}

    def submit(self, page: Page, query: Query) -> None:
        box = page.locator(_SEARCH_BOX).first
        box.click()
        page.keyboard.type(query.text, delay=random.randint(60, 140))  # 한 글자씩 (insert_text는 붙여넣기처럼 보인다)
        page.wait_for_timeout(random.randint(500, 1500))
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded")
        self._raise_if_blocked(page)

    def wait_until_done(self, page: Page) -> None:
        heading = page.locator(f"{_ROOT} [role=heading]").filter(has_text=_HEADINGS[0]).or_(
            page.locator(f"{_ROOT} [role=heading]").filter(has_text=_HEADINGS[1]))
        try:
            heading.first.wait_for(state="attached", timeout=_APPEAR_TIMEOUT_MS)
        except Exception:
            # AI 개요 없음 → extract에서 answer_present=False. 단 검색 결과가 실제로 있을 때만 (캡차·빈 화면 제외)
            self._raise_if_blocked(page)
            if not page.locator(_RESULTS).count():
                raise CrawlerError("google_aio: 검색 결과 화면이 아님 — 'AI 개요 없음'으로 기록하지 않음")
            return
        turn = page.locator(_TURN)
        self.wait_for_stable(page, turn, quiet_s=3.0, done=lambda: turn.first.get_attribute("data-complete") == "true")
        if turn.first.evaluate(_EXPAND_JS):
            page.wait_for_timeout(1500)
            self.wait_for_stable(page, turn, quiet_s=2.0)

    def extract(self, page: Page, captured: list[CapturedResponse]) -> Extraction:
        turn = page.locator(_TURN)
        if not turn.count() or not any(h in turn.first.inner_text()[:40] for h in _HEADINGS):
            return Extraction(response_text=None, answer_present=False, extra={"aio": False})
        turn = turn.first
        body = turn.locator(":scope > div").nth(1)  # [제목, 본문, 면책 문구, …]
        if not body.count():
            raise CrawlerError("google_aio: AI 개요 본문을 찾지 못함")
        turn.evaluate(_HIDE_JS, True)
        try:
            text = body.inner_text().strip()
        finally:
            turn.evaluate(_HIDE_JS, False)
        lines = text.splitlines()
        while lines and lines[-1].strip() in _TRAILING_BUTTONS:  # 출처 패널의 '모두 표시' 버튼
            lines.pop()
        text = "\n".join(lines).strip()

        resolved: dict[str, str | None] = {}
        citations: dict[str, Citation] = {}
        for link in self.links_in(turn):
            url = self._resolve(page, link.url, resolved)
            if not url or any(h in (urlsplit(url).hostname or "") for h in _IGNORE_HOSTS):
                continue
            title = _clean_title(link.title)
            existing = citations.setdefault(url, Citation(url=url, title=title))
            # 본문 링크 제목은 회사명, 출처 카드 제목은 문서 제목 — 카드 쪽이 더 정확하다
            if link.title and ("새 탭에서" in link.title or "new tab" in link.title.lower()):
                existing.title = title

        return Extraction(
            response_text=text or None,
            answer_present=bool(text),
            response_html=turn.evaluate("e => e.outerHTML"),
            citations=list(citations.values()),
            extra={"aio": True, "source_chips": turn.locator("[data-icl-uuid]").count()},
        )

    def extra_settings(self, page: Page) -> dict[str, Any]:
        return {"logged_in": self.is_logged_in(page), "hl": self.language}

    # ---------- 내부 ----------
    def _resolve(self, page: Page, url: str, cache: dict[str, str | None]) -> str | None:
        """google.com/goto·/url 리디렉트 → 실제 URL. 일반 URL은 그대로."""
        parts = urlsplit(url)
        if not (parts.hostname or "").startswith("www.google.") or parts.path not in _REDIRECT:
            return url
        if url in cache:
            return cache[url]
        target = None
        q = parse_qs(parts.query).get("q") or parse_qs(parts.query).get("url")
        if q and q[0].startswith(("http://", "https://")):  # 예전 형식은 쿼리에 URL이 그대로 있다
            target = q[0]
        else:
            try:
                r = page.request.get(url, max_redirects=0, timeout=15_000)
                target = r.headers.get("location")
            except Exception as e:
                log.warning("google_aio: 리디렉트 해석 실패 %s: %r", url[:80], e)
        cache[url] = target
        return target


def _clean_title(title: str | None) -> str | None:
    if not title:
        return None
    for suffix in (". 새 탭에서 열립니다.", ". Opens in new tab.", ", 동영상, 외부 사이트에서 열림"):
        title = title.replace(suffix, "")
    return title.strip() or None
