"""ChatGPT (chatgpt.com).

로그인 없이도 질의할 수 있지만, 비로그인과 로그인은 프런트엔드가 다르다.

    비로그인  /unauth-mweb/conversation/updates (NDJSON). 답변은 DIL(생성형 UI)로 오고,
              인용은 componentResults의 Cite 컴포넌트(state.items[].url)에 있다. 하위 검색어는 노출되지 않는다.
    로그인    /backend-api/(f/)conversation (SSE). metadata의 search_queries·search_model_queries가 fanout,
              content_references(grouped_webpages)가 인용. 답변 안 인용 칩은 <a>로 렌더링된다.

두 경우 모두 화면(DOM)에서 답변 텍스트·링크를 먼저 읽고, 네트워크 원문에서 인용·fanout·모델을 보탠다.
로그인 상태에서는 임시 채팅(temporary-chat)으로 묻는다. 임시 채팅도 메모리·맞춤 지침을 참조할 수 있으므로
세션 시작 때 계정 설정(check_personalization)이 모두 꺼져 있는지 확인하고, 켜져 있으면 중단한다.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Page

from ..base import BaseCrawler, PersonalizationError
from ..models import CapturedResponse, Citation, Extraction, Query

_INPUT = "#prompt-textarea:visible, div.ProseMirror[contenteditable=true]:visible, textarea[name=prompt]:visible"
# 로그인: data-message-author-role / 비로그인: li[data-message-role]
_TURN = "[data-message-author-role=assistant], li[data-message-role=assistant]"
_STOP = "[data-testid=stop-button], button[aria-label*='중지'], button[aria-label*='Stop']"
# 쿠키 동의, '로그아웃 유지', 기능 안내(메모리 업데이트 등) 같은 입력을 가리는 팝업.
# '설정'처럼 설정을 바꾸는 버튼은 누르지 않고 닫기/확인만 누른다
_DISMISS_ANYWHERE = ("모두 허용", "Accept all", "로그아웃 유지", "Stay logged out")  # 쿠키 배너는 dialog가 아니다
_DISMISS_IN_DIALOG = ("확인", "계속", "OK", "Continue", "Got it", "알겠습니다", "닫기", "Close")

# 답변을 사용자별로 바꾸는 설정 (/backend-api/settings/user). True면 측정 중단
_PERSONAL_FLAGS = {"sunshine": "저장된 메모리 참조", "moonshine": "채팅 기록 참조"}
_CUSTOM_INSTRUCTION_FIELDS = ("about_user_message", "about_model_message", "name_user_message", "role_user_message",
                              "traits_model_message", "other_user_message", "personality_type_selection")
_PERSONALIZATION_JS = """async () => {
    const s = await (await fetch('/api/auth/session')).json();
    const h = {Authorization: 'Bearer ' + s.accessToken};
    const get = async u => { const r = await fetch(u, {headers: h}); return r.ok ? r.json() : {error: r.status}; };
    return {
        settings: (await get('/backend-api/settings/user')).settings || {},
        instructions: await get('/backend-api/user_system_messages'),
        memories: await get('/backend-api/memories?include_memory_entries=false'),
    };
}"""
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
        # 안내 팝업(메모리 업데이트, 임시 채팅 소개 등)은 렌더링 뒤 늦게, 여러 개 연달아 뜬다 → 입력창이 보일 때까지 닫기
        box = page.locator(_INPUT).first
        for _ in range(20):
            self._dismiss_overlays(page)
            if box.is_visible() and not page.locator("[role=dialog]:visible").count():
                return
        box.wait_for(state="visible", timeout=5_000)

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

    def check_personalization(self, page: Page) -> dict[str, Any]:
        if not self.is_logged_in(page):
            return {"logged_in": False}
        data = page.evaluate(_PERSONALIZATION_JS)
        flags = {k: data["settings"].get(k) for k in _PERSONAL_FLAGS}
        instructions = {k: v for k, v in data["instructions"].items() if k in _CUSTOM_INSTRUCTION_FIELDS and v}
        memory_tokens = data["memories"].get("memory_num_tokens")
        problems = [f"{label}({k})" for k, label in _PERSONAL_FLAGS.items() if flags[k]]
        if instructions:
            problems.append(f"맞춤 지침({', '.join(instructions)})")
        if memory_tokens:
            problems.append(f"저장된 메모리 {memory_tokens} 토큰")
        if "error" in data["instructions"] or "error" in data["memories"]:
            problems.append(f"설정 조회 실패({data['instructions'].get('error')}, {data['memories'].get('error')})")
        if problems:
            raise PersonalizationError(
                "chatgpt: 답변 맞춤화 설정이 켜져 있음 — " + ", ".join(problems)
                + ". 설정 > 개인 맞춤 설정에서 메모리·채팅 기록 참조를 끄고 맞춤 지침·메모리를 비운 뒤 다시 실행")
        return {"flags": flags, "custom_instructions": False, "memory_tokens": memory_tokens or 0}

    def _dismiss_overlays(self, page: Page) -> None:
        page.wait_for_timeout(1500)
        for _ in range(3):
            clicked = False
            candidates = [page.get_by_role("button", name=label, exact=True) for label in _DISMISS_ANYWHERE]
            candidates += [page.locator("[role=dialog]").get_by_role("button", name=label, exact=True)
                           for label in _DISMISS_IN_DIALOG]
            for button in candidates:
                if button.count() and button.first.is_visible():
                    button.first.click()
                    page.wait_for_timeout(700)
                    clicked = True
                    break
            if not clicked:
                if page.locator("[role=dialog]:visible").count():
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)
                return


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
