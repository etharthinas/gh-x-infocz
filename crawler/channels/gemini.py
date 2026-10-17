"""Gemini (gemini.google.com).

비로그인으로만 측정한다 (Flash-Lite 고정). 로그인하면 Gemini 앱 활동·과거 대화·개인 맥락이 답변에 섞일 수 있고,
Google 세션 쿠키는 다른 기기로 옮기면 곧 무효화되어 서버에서 로그인을 유지할 수도 없다.
로그인 상태로 감지되면 PersonalizationError로 중단한다. 답변 스트림은 WebChannel(processSession)이라
읽기 어렵고, 인용 URL도 네트워크·<a>에 없다. 대신 답변 안 출처 칩(source-inline-chip의 button)의
jslog 속성에 `BardVeMetadataKey:<base64 JSON>`이 있고, 그 JSON에 칩이 가리키는 URL들이 들어 있다.
칩을 클릭하지 않고 DOM만으로 인용을 순서대로 얻는다. 하위 검색어(fanout)는 UI에 노출되지 않는다.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page

from ..base import BaseCrawler, PersonalizationError
from ..models import CapturedResponse, Citation, Extraction, Query

_INPUT = "rich-textarea div.ql-editor[contenteditable=true], div.ql-editor[contenteditable=true]"
_TURN = "model-response"
_BODY = ".markdown-main-panel"
_CHIP = "source-inline-chip button[jslog]"
_MODE = "[data-test-id=bard-mode-menu-button]"
_VE_KEY = re.compile(r"BardVeMetadataKey:([A-Za-z0-9+/_=-]+)")
_URL = re.compile(r"https?://[^\s\"'\\<>]+")
# 칩 aria-label: "jn-silveryoung.tistory.com의 인용에 대한 …" / "S2W Inc. 및 지디넷코리아의 인용에 대한 …"
_CHIP_LABEL = re.compile(r"^(.*?)(?:의 인용에 대한| citation| source)", re.IGNORECASE)
# 파비콘·로그인·검색 링크 등 Google 자체 UI
_IGNORE_HOSTS = re.compile(r"(^|\.)(gstatic\.com|googleusercontent\.com)$|^(www\.google\.com|gemini\.google\.com|accounts\.google\.com|policies\.google\.com)$")
# 출처가 둘 이상인 칩 ("A 및 B의 인용…") — jslog에는 첫 URL만 있어 클릭해서 팝업 링크를 읽는다
_MULTI_LABEL = re.compile(r" 및 | and |, ")


def clean_url(url: str) -> str:
    """텍스트 하이라이트 조각(#:~:text=…) 제거."""
    parts = urlsplit(url)
    if parts.fragment.startswith(":~:"):
        parts = parts._replace(fragment="")
    return urlunsplit(parts)


def decode_chip(jslog: str) -> list[str]:
    """jslog의 BardVeMetadataKey(base64 JSON)에서 URL을 등장 순서대로."""
    m = _VE_KEY.search(jslog or "")
    if not m:
        return []
    raw = m.group(1)
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_" if "-" in raw or "_" in raw else None)
        text = decoded.decode("utf-8", errors="replace")
    except ValueError:
        return []
    try:
        strings = [s for s in _iter_strings(json.loads(text)) if s.startswith(("http://", "https://"))]
    except json.JSONDecodeError:
        strings = _URL.findall(text)
    return [u for u in dict.fromkeys(strings) if not _IGNORE_HOSTS.search(urlsplit(u).hostname or "")]


def _iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)


class GeminiCrawler(BaseCrawler):
    engine = "gemini"
    start_url = "https://gemini.google.com/app"
    requires_login = False
    answer_timeout_s = 240.0

    def open_fresh(self, page: Page, query: Query) -> None:
        page.goto(self.start_url, wait_until="domcontentloaded")
        page.locator(_INPUT).first.wait_for(state="visible", timeout=30_000)

    def is_logged_in(self, page: Page) -> bool:
        # 쿠키만으로는 판단할 수 없다 (옮겨 온 Google 쿠키는 남아 있어도 무효). 화면의 '로그인' 링크 유무로 본다
        return page.locator("a[href*='accounts.google.com/ServiceLogin']").count() == 0

    def check_personalization(self, page: Page) -> dict[str, Any]:
        if self.is_logged_in(page):
            raise PersonalizationError(
                "gemini: 로그인 상태 — 계정 맞춤화가 섞일 수 있어 비로그인으로만 측정한다. "
                "data/profiles/gemini 를 지우고 다시 실행")
        return {"logged_in": False}

    def submit(self, page: Page, query: Query) -> None:
        self.type_into(page, page.locator(_INPUT).first, query.text)

    def wait_until_done(self, page: Page) -> None:
        turn = page.locator(_TURN).last
        body = turn.locator(_BODY)

        def done() -> bool:
            return body.count() > 0 and body.first.get_attribute("aria-busy") == "false" \
                and turn.locator("[data-test-id=regenerate-button], [data-test-id=copy-button]").count() > 0

        self.wait_for_stable(page, body, quiet_s=4.0, done=done)
        page.wait_for_timeout(1500)  # 출처 칩 렌더링

    def extract(self, page: Page, captured: list[CapturedResponse]) -> Extraction:
        turn = page.locator(_TURN).last
        body = turn.locator(_BODY).first
        # 출처 칩의 라벨('비링커' 등)이 문장 사이에 끼지 않도록 칩을 잠시 숨기고 읽는다
        text = ""
        if turn.locator(_BODY).count():
            body.evaluate("e => e.querySelectorAll('source-inline-chip, source-footnote').forEach(c => c.hidden = true)")
            text = body.inner_text().strip()
            body.evaluate("e => e.querySelectorAll('source-inline-chip, source-footnote').forEach(c => c.hidden = false)")

        chips = turn.locator(_CHIP).evaluate_all(
            "els => els.map(b => ({jslog: b.getAttribute('jslog') || '', label: b.getAttribute('aria-label') || ''}))")
        citations: dict[str, Citation] = {}
        for i, chip in enumerate(chips):
            m = _CHIP_LABEL.match(chip["label"])
            label = m.group(1).strip() if m else None
            multi = bool(_MULTI_LABEL.search(label or ""))
            # 출처가 여러 개인 칩의 라벨은 'A 및 B'라 한 URL의 제목으로 쓰지 않는다
            found = [Citation(url=u, title=None if multi else label) for u in decode_chip(chip["jslog"])]
            if multi:
                found += self._chip_popup_citations(page, turn.locator(_CHIP).nth(i))
            for c in found:
                url = clean_url(c.url)
                existing = citations.setdefault(url, Citation(url=url, title=c.title))
                existing.title = existing.title or c.title
        # 본문 안 일반 링크·출처 캐러셀 링크
        for c in self.links_in(turn):
            url = clean_url(c.url)
            if not _IGNORE_HOSTS.search(urlsplit(url).hostname or ""):
                citations.setdefault(url, Citation(url=url, title=c.title))

        return Extraction(
            response_text=text,
            answer_present=bool(text),
            response_html=turn.evaluate("e => e.outerHTML"),
            model=self._mode(page),
            citations=list(citations.values()),
            extra={"conversation_url": page.url, "source_chips": len(chips)},
        )

    def extra_settings(self, page: Page) -> dict[str, Any]:
        return {"logged_in": self.is_logged_in(page), "mode": self._mode(page)}

    def _chip_popup_citations(self, page: Page, chip) -> list[Citation]:
        """칩을 눌러 뜨는 출처 팝업([role=dialog])의 링크. 실패해도 jslog에서 얻은 첫 URL은 남는다."""
        dialog = page.locator("[role=dialog]")
        try:
            chip.scroll_into_view_if_needed()
            chip.click(timeout=5_000)
            page.wait_for_timeout(1_200)
            found = self.links_in(dialog.last) if dialog.count() else []
        except Exception:
            found = []
        finally:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        for c in found:  # 팝업 링크 텍스트는 '도메인\n제목\n발췌' 형태
            lines = [line.strip() for line in (c.title or "").splitlines() if line.strip()]
            c.title = lines[1] if len(lines) > 1 else (lines[0] if lines else None)
        return [c for c in found if not _IGNORE_HOSTS.search(urlsplit(c.url).hostname or "")]

    @staticmethod
    def _mode(page: Page) -> str | None:
        mode = page.locator(_MODE)
        return mode.first.inner_text().strip() or None if mode.count() else None
