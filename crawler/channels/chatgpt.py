"""ChatGPT (chatgpt.com).

로그인 없이도 질의할 수 있지만, 비로그인과 로그인은 프런트엔드가 다르다.

    비로그인  /unauth-mweb/conversation/updates (NDJSON). 답변은 DIL(생성형 UI)로 오고,
              인용은 componentResults의 Cite 컴포넌트(state.items[].url)에 있다. 하위 검색어는 노출되지 않는다.
    로그인    /backend-api/(f/)conversation (SSE). metadata의 search_queries·search_model_queries가 fanout,
              content_references(grouped_webpages)가 인용. 답변 안 인용 칩은 <a>로 렌더링된다.

두 경우 모두 화면(DOM)에서 답변 텍스트·링크를 먼저 읽고, 네트워크 원문에서 인용·fanout·모델을 보탠다.
로그인 상태에서는 메모리·맞춤 설정이 섞이지 않도록 임시 채팅(temporary-chat)으로 묻는다.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Page

from ..base import BaseCrawler
from ..models import CapturedResponse, Citation, Extraction, Query

_INPUT = "#prompt-textarea:visible, textarea[name=prompt]:visible"
# 로그인: data-message-author-role / 비로그인: li[data-message-role]
_TURN = "[data-message-author-role=assistant], li[data-message-role=assistant]"
_STOP = "[data-testid=stop-button], button[aria-label*='중지'], button[aria-label*='Stop']"
# 쿠키 동의, '로그아웃 유지' 등 입력을 가리는 팝업
_DISMISS = ("모두 허용", "Accept all", "로그아웃 유지", "Stay logged out")
_SR_HEADER = re.compile(r"^\s*(ChatGPT의 말|ChatGPT said)\s*:?\s*\n")


def clean_url(url: str) -> str:
    """ChatGPT가 붙이는 utm_source=chatgpt.com 제거 (같은 출처의 중복 제거용)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not (k == "utm_source" and v == "chatgpt.com")]
    return urlunsplit(parts._replace(query=urlencode(query)))


class ChatGPTCrawler(BaseCrawler):
    engine = "chatgpt"
    start_url = "https://chatgpt.com/"
    requires_login = False
    capture_patterns = (
        r"chatgpt\.com/unauth-mweb/conversation/updates",
        r"chatgpt\.com/backend-(api|anon)/(f/)?conversation(\?|$)",
    )
    answer_timeout_s = 240.0

    def open_fresh(self, page: Page, query: Query) -> None:
        # 임시 채팅은 로그인 상태에서만 의미가 있다 (비로그인은 원래 기록이 없음)
        url = self.start_url + ("?temporary-chat=true" if self.is_logged_in(page) else "")
        page.goto(url, wait_until="domcontentloaded")
        page.locator(_INPUT).first.wait_for(state="visible", timeout=30_000)
        self._dismiss_overlays(page)

    def is_logged_in(self, page: Page) -> bool:
        return any(c["name"].startswith("__Secure-next-auth.session-token") for c in page.context.cookies("https://chatgpt.com"))

    def submit(self, page: Page, query: Query) -> None:
        self.type_into(page, page.locator(_INPUT).first, query.text)

    def wait_until_done(self, page: Page) -> None:
        turn = page.locator(_TURN).last

        def done() -> bool:
            if page.locator("li[data-message-role=assistant][data-message-complete]").count():
                return True
            return not page.locator(_STOP).count()

        self.wait_for_stable(page, turn, quiet_s=4.0, done=done)
        page.wait_for_timeout(1500)  # 인용 칩·스트림 마지막 이벤트 반영

    def extract(self, page: Page, captured: list[CapturedResponse]) -> Extraction:
        turn = page.locator(_TURN).last
        body = turn.locator(".markdown").first if turn.locator(".markdown").count() else turn
        text = _SR_HEADER.sub("", body.inner_text()).strip()

        events = [e for c in captured if c.status == 200 for e in _json_lines(c.body)]
        net = _parse_events(events)

        citations: dict[str, Citation] = {}
        for c in [*self.links_in(body), *net["citations"]]:
            url = clean_url(c.url)
            parts = urlsplit(url)
            # 자사 UI 링크, 엔티티 카드의 지도 링크는 인용이 아니다
            if parts.hostname in ("chatgpt.com", "openai.com", "images.openai.com") or (
                    parts.hostname in ("www.google.com", "maps.google.com") and parts.path.startswith("/maps")):
                continue
            existing = citations.get(url)
            if existing is None:
                citations[url] = Citation(url=url, title=c.title, snippet=c.snippet)
            elif not existing.snippet and c.snippet:
                existing.snippet = c.snippet

        model = turn.get_attribute("data-message-model-slug") or net["model"]
        return Extraction(
            response_text=text,
            answer_present=bool(text),
            response_html=turn.evaluate("e => e.outerHTML"),
            model=model,
            citations=list(citations.values()),
            fanout_queries=net["fanouts"],
            extra={"frontend": "unauth-mweb" if "/unauth-mweb/" in " ".join(c.url for c in captured) else "backend-api",
                   "conversation_url": page.url, "markdown": net["markdown"]},
        )

    def extra_settings(self, page: Page) -> dict[str, Any]:
        logged_in = self.is_logged_in(page)
        return {"logged_in": logged_in, "temporary_chat": logged_in}

    def _dismiss_overlays(self, page: Page) -> None:
        for label in _DISMISS:
            button = page.get_by_role("button", name=label, exact=True)
            if button.count() and button.first.is_visible():
                button.first.click()
                page.wait_for_timeout(500)


def _json_lines(body: str) -> list[Any]:
    """SSE(data: …)·NDJSON 공용. JSON이 아닌 줄은 건너뜀."""
    events = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _parse_events(events: list[Any]) -> dict[str, Any]:
    citations: list[Citation] = []
    fanouts: list[str] = []
    model: str | None = None
    markdown: str | None = None
    cite_components: dict[str, list[dict]] = {}  # 비로그인 DIL: 같은 컴포넌트가 스트림 중 여러 번 갱신됨 → 마지막 값

    def add_queries(value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("queries", [])
        for q in value if isinstance(value, list) else []:
            q = q.get("q") or q.get("query") if isinstance(q, dict) else q
            if isinstance(q, str) and q.strip():
                fanouts.append(q.strip())

    for event in events:
        if isinstance(event, dict) and isinstance(dil := event.get("dil"), dict) and dil.get("fallbackMarkdown"):
            markdown = dil["fallbackMarkdown"]
        for d in BaseCrawler.walk_json(event):
            # ---- 로그인(backend-api): metadata 또는 JSON-patch {"p": ".../search_queries", "v": ...}
            path = d.get("p") if isinstance(d.get("p"), str) else ""
            if path.endswith(("/search_queries", "/search_model_queries")):
                add_queries(d.get("v"))
            if "search_queries" in d:
                add_queries(d["search_queries"])
            if "search_model_queries" in d:
                add_queries(d["search_model_queries"])
            if d.get("type") == "search_model_queries":
                add_queries(d)
            if isinstance(d.get("model_slug"), str):
                model = d["model_slug"]
            if str(d.get("type", "")).startswith("grouped_webpages"):
                for item in d.get("items") or []:
                    if isinstance(item, dict) and item.get("url"):
                        citations.append(Citation(url=item["url"], title=item.get("title"),
                                                  snippet=item.get("snippet")))
            # ---- 비로그인(unauth-mweb): DIL componentResults
            if isinstance(results := d.get("componentResults"), dict):
                for cid, comp in results.items():
                    if isinstance(comp, dict) and comp.get("componentName") == "Cite":
                        items = (comp.get("state") or {}).get("items") or []
                        cite_components[cid] = items  # dict는 처음 등장 순서를 유지

    for items in cite_components.values():
        for item in items:
            if item.get("url"):
                title = item.get("title") or item.get("source_label")
                citations.append(Citation(url=item["url"], title=title, snippet=item.get("snippet")))

    return {"citations": citations, "fanouts": list(dict.fromkeys(fanouts)), "model": model, "markdown": markdown}
