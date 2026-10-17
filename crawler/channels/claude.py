"""Claude (claude.ai). 로그인 필수.

화면 없는 서버에서는 PC에서 scripts/export_cookies.py로 쿠키를 내보내 scripts/import_cookies.sh로 넣는다.
claude.ai는 headless 브라우저에 Cloudflare 403을 주므로 xvfb-run으로 헤드풀 실행한다.

- 시크릿 채팅(/new?incognito)으로 묻는다: 기록·메모리에 남지 않는다. 세션 시작 때 계정의 맞춤화 설정
  (개인 선호 지침, 메모리 생성·과거 대화 검색, 저장된 메모리)이 모두 꺼져 있는지 확인하고, 켜져 있으면 중단한다.
- 답변 스트림(ConversationService/StreamTimeline)은 바이너리 protobuf라, 답변이 끝난 뒤 페이지 안에서
  대화 JSON(GET chat_conversations/<id>?tree=True&rendering_mode=messages&render_all_tools=true)을 받아 읽는다.
      model                                  모델 (예: claude-opus-5-5)
      content[type=tool_use name=web_search] input.query → fanout
      content[type=tool_result].content[]    검색 결과 (knowledge: url, title) → 직전 검색어와 연결
      content[type=text].citations[]         답변에 실제로 단 인용
- 웹 검색은 계정 설정(enabled_web_search)을 따른다. settings.web_search에 남긴다.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Page

from ..base import BaseCrawler, CrawlerError, PersonalizationError
from ..models import CapturedResponse, Citation, Extraction, Query

_INPUT = ("div[contenteditable=true][data-testid=chat-input]:visible, div.ProseMirror[contenteditable=true]:visible, "
          "[contenteditable=true]:visible")
_TURN = "[data-testid=assistant-message]"
_MODEL = "[data-testid=model-selector-dropdown]"
_SEARCH_TOOLS = ("web_search", "brave_search", "search")
_CONVERSATION_ID = re.compile(r"/chat/([0-9a-f-]{36})")

_FETCH_JS = "async u => { const r = await fetch(u); return [r.status, r.ok ? await r.json() : null]; }"


class ClaudeCrawler(BaseCrawler):
    engine = "claude"
    start_url = "https://claude.ai/new?incognito"
    requires_login = True
    answer_timeout_s = 300.0
    delay_between_runs_s = (15.0, 30.0)  # 사용량 제한이 빡빡하다

    def open_fresh(self, page: Page, query: Query) -> None:
        page.goto(self.start_url, wait_until="domcontentloaded")
        if "/login" not in page.url:
            page.locator(_INPUT).first.wait_for(state="visible", timeout=30_000)

    def is_logged_in(self, page: Page) -> bool:
        has_session = any(c["name"] == "sessionKey" for c in page.context.cookies("https://claude.ai"))
        return has_session and "/login" not in page.url

    def blocked_reason(self, page: Page) -> str | None:
        if reason := super().blocked_reason(page):
            return reason
        # 사용량 한도 배너 — 계속 보내면 실패만 쌓인다
        if page.get_by_text("usage limit", exact=False).count() or page.get_by_text("사용 한도", exact=False).count():
            return "claude_usage_limit"
        return None

    def check_personalization(self, page: Page) -> dict[str, Any]:
        org = self._org(page)
        account = self._get(page, "/api/account")
        profile = self._get(page, "/api/account_profile")
        memory = self._get(page, f"/api/organizations/{org}/memory")
        memory_settings = self._get(page, f"/api/organizations/{org}/memory/settings")
        account_settings = account.get("settings") or {}

        # saffron = 대화에서 메모리 생성, saffron_search = 과거 대화 검색·참조, melange = 새 메모리 시스템
        flags = {k: bool(memory_settings.get(k) or account_settings.get(k))
                 for k in ("enabled_saffron", "enabled_saffron_search", "enabled_melange")}
        problems = [k for k, on in flags.items() if on]
        if (profile.get("conversation_preferences") or "").strip():
            problems.append("개인 선호 지침(conversation_preferences)")
        if (memory.get("memory") or "").strip():
            problems.append("저장된 메모리")
        if problems:
            raise PersonalizationError(
                "claude: 답변 맞춤화 설정이 켜져 있음 — " + ", ".join(problems)
                + ". 설정 > 일반(개인 선호)·기능(메모리, 과거 대화 검색)에서 끄고 비운 뒤 다시 실행")
        self._web_search = account_settings.get("enabled_web_search")
        return {"flags": flags, "conversation_preferences": False, "memory": False}

    def submit(self, page: Page, query: Query) -> None:
        self.type_into(page, page.locator(_INPUT).first, query.text)

    def wait_until_done(self, page: Page) -> None:
        turn = page.locator(_TURN).last

        def done() -> bool:
            return turn.get_attribute("data-is-streaming") == "false"

        self.wait_for_stable(page, turn, quiet_s=5.0, done=done)
        page.wait_for_timeout(1500)

    def extract(self, page: Page, captured: list[CapturedResponse]) -> Extraction:
        turn = page.locator(_TURN).last
        m = _CONVERSATION_ID.search(page.url)
        if not m:
            raise CrawlerError(f"claude: 대화 id를 URL에서 찾지 못함 ({page.url})")
        tree = self._get(page, f"/api/organizations/{self._org(page)}/chat_conversations/{m.group(1)}"
                               "?tree=True&rendering_mode=messages&render_all_tools=true")
        net = parse_conversation(tree)

        citations: dict[str, Citation] = {}
        for c in net["citations"]:
            existing = citations.setdefault(c.url, c)
            existing.title = existing.title or c.title
            existing.snippet = existing.snippet or c.snippet
            existing.fanout_query = existing.fanout_query or net["url_query"].get(c.url)

        model_ui = self._model_ui(page)
        return Extraction(
            response_text=net["text"],
            answer_present=bool(net["text"]),
            response_html=turn.evaluate("e => e.outerHTML"),
            model=net["model"] or model_ui,
            citations=list(citations.values()),
            fanout_queries=net["fanouts"],
            extra={"conversation_url": page.url, "conversation": tree, "search_results": net["search_results"]},
        )

    def extra_settings(self, page: Page) -> dict[str, Any]:
        return {"model_ui": self._model_ui(page), "incognito": "incognito" in page.url,
                "web_search": getattr(self, "_web_search", None)}

    # ---------- 내부 ----------
    @staticmethod
    def _org(page: Page) -> str:
        org = next((c["value"] for c in page.context.cookies("https://claude.ai") if c["name"] == "lastActiveOrg"), None)
        if not org:
            raise CrawlerError("claude: lastActiveOrg 쿠키 없음 — 다시 로그인")
        return org

    @staticmethod
    def _get(page: Page, path: str) -> dict[str, Any]:
        status, body = page.evaluate(_FETCH_JS, path)
        if status != 200 or not isinstance(body, dict):
            raise CrawlerError(f"claude: GET {path} → {status}")
        return body

    @staticmethod
    def _model_ui(page: Page) -> str | None:
        model = page.locator(_MODEL)
        return model.first.inner_text().strip() or None if model.count() else None


def parse_conversation(tree: dict[str, Any]) -> dict[str, Any]:
    """대화 JSON의 마지막 assistant 메시지 → 텍스트·fanout·인용·검색 결과."""
    fanouts: list[str] = []
    citations: list[Citation] = []
    search_results: list[dict[str, Any]] = []
    url_query: dict[str, str] = {}
    texts: list[str] = []

    messages = [m for m in tree.get("chat_messages") or [] if m.get("sender") == "assistant"]
    last_query: str | None = None
    last_tool_seen = False
    for block in (messages[-1].get("content") or []) if messages else []:
        btype = block.get("type")
        if btype in ("tool_use", "tool_result"):
            last_tool_seen = True
        if btype == "tool_use" and block.get("name") in _SEARCH_TOOLS:
            if q := (block.get("input") or {}).get("query"):
                fanouts.append(q)
                last_query = q
        elif btype == "tool_result":
            for item in block.get("content") or []:
                if isinstance(item, dict) and item.get("url"):
                    search_results.append({"url": item["url"], "title": item.get("title"), "query": last_query})
                    if last_query:
                        url_query.setdefault(item["url"], last_query)
        elif btype == "text":
            # 검색 사이사이의 진행 멘트("더 찾아볼게요")는 버리고 마지막 도구 호출 뒤의 텍스트만 답변으로 본다
            if last_tool_seen:
                texts, last_tool_seen = [], False
            texts.append(block.get("text", ""))
            for c in block.get("citations") or []:
                if url := c.get("url") or (c.get("metadata") or {}).get("url"):
                    citations.append(Citation(url=url, title=c.get("title"), snippet=c.get("cited_text")))

    return {
        "model": tree.get("model"),
        "text": "".join(texts).strip() or None,
        "fanouts": list(dict.fromkeys(fanouts)),
        "citations": citations,
        "search_results": search_results,
        "url_query": url_query,
    }
