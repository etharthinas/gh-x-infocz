"""Claude (claude.ai). 로그인 필수.

화면 없는 서버에서는 다른 PC 브라우저에서 claude.ai 쿠키(sessionKey 포함)를 JSON으로 내보낸 뒤
`python -m crawler login claude --cookies cookies.json`으로 프로필에 넣는다.

답변 스트림은 POST /api/organizations/<org>/chat_conversations/<id>/completion (SSE, Anthropic 메시지 이벤트).
    message_start.message.model                       모델
    content_block_start(tool_use name=web_search) +
      input_json_delta → {"query": …}                 fanout
    content_block_start(tool_result).content[]        검색 결과 (type=knowledge, url, title) → 직전 검색어와 연결
    content_block_delta(citation_start_delta).citation 답변에 실제로 단 인용
스트림 뒤에 오는 대화 트리 GET(chat_conversations/<id>?tree=…)에도 같은 블록이 완성형으로 들어 있어 함께 읽는다.
웹 검색은 계정 설정(검색 도구 켜기)을 따른다.
"""

from __future__ import annotations

import json
from typing import Any

from playwright.sync_api import Page

from ..base import BaseCrawler
from ..models import CapturedResponse, Citation, Extraction, Query

_INPUT = ("div[contenteditable=true][data-testid=chat-input]:visible, div.ProseMirror[contenteditable=true]:visible, "
          "[contenteditable=true]:visible")
_TURN = "[data-is-streaming]"
_BODY = ".font-claude-response, .font-claude-message"
_MODEL = "[data-testid=model-selector-dropdown]"
_SEARCH_TOOLS = ("web_search", "brave_search", "search")


class ClaudeCrawler(BaseCrawler):
    engine = "claude"
    start_url = "https://claude.ai/new"
    requires_login = True
    capture_patterns = (
        r"claude\.ai/api/organizations/[^/]+/chat_conversations/[^/?]+/(retry_)?completion",
        r"claude\.ai/api/organizations/[^/]+/chat_conversations/[^/?]+\?",
    )
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

    def submit(self, page: Page, query: Query) -> None:
        self.type_into(page, page.locator(_INPUT).first, query.text)

    def wait_until_done(self, page: Page) -> None:
        turn = page.locator(_TURN).last

        def done() -> bool:
            return turn.get_attribute("data-is-streaming") == "false"

        self.wait_for_stable(page, turn, quiet_s=5.0, done=done)
        page.wait_for_timeout(2000)  # 대화 트리 GET·인용 칩 반영

    def extract(self, page: Page, captured: list[CapturedResponse]) -> Extraction:
        turn = page.locator(_TURN).last
        body = turn.locator(_BODY).last if turn.locator(_BODY).count() else turn
        net = _parse_captured(captured)
        # 스트림 텍스트에는 검색 단계 요약('Searched the web' 등)이 섞이지 않는다
        text = (net["text"] or body.inner_text()).strip()

        citations: dict[str, Citation] = {}
        for c in [*net["citations"], *self.links_in(body)]:
            if "claude.ai" in c.url or "anthropic.com" in c.url:
                continue
            existing = citations.setdefault(c.url, c)
            existing.title = existing.title or c.title
            existing.snippet = existing.snippet or c.snippet
            existing.fanout_query = existing.fanout_query or net["url_query"].get(c.url)

        model_ui = page.locator(_MODEL).first.inner_text().strip() if page.locator(_MODEL).count() else None
        return Extraction(
            response_text=text,
            answer_present=bool(text),
            response_html=turn.evaluate("e => e.outerHTML"),
            model=net["model"] or model_ui,
            citations=list(citations.values()),
            fanout_queries=net["fanouts"],
            extra={"conversation_url": page.url, "model_ui": model_ui, "search_results": net["search_results"]},
        )

    def extra_settings(self, page: Page) -> dict[str, Any]:
        return {"model_ui": page.locator(_MODEL).first.inner_text().strip() if page.locator(_MODEL).count() else None}


def _parse_captured(captured: list[CapturedResponse]) -> dict[str, Any]:
    model: str | None = None
    texts: list[str] = []
    fanouts: list[str] = []
    citations: list[Citation] = []
    search_results: list[dict[str, Any]] = []
    url_query: dict[str, str] = {}

    def add_result(item: dict, query: str | None) -> None:
        if item.get("url"):
            search_results.append({"url": item["url"], "title": item.get("title"), "query": query})
            if query:
                url_query.setdefault(item["url"], query)

    def add_citation(c: dict) -> None:
        url = c.get("url") or (c.get("metadata") or {}).get("url")
        if url:
            citations.append(Citation(url=url, title=c.get("title"), snippet=c.get("cited_text") or c.get("text")))

    for cap in captured:
        if cap.status != 200:
            continue
        if "/completion" in cap.url:
            # ---- 스트림: 블록 index별로 조립
            blocks: dict[int, dict[str, Any]] = {}
            last_query: str | None = None
            for ev in BaseCrawler.parse_sse(cap.body):
                if not isinstance(ev, dict):
                    continue
                kind = ev.get("type")
                if kind == "message_start":
                    model = (ev.get("message") or {}).get("model") or model
                elif kind == "content_block_start":
                    block = dict(ev.get("content_block") or {})
                    block["_json"] = ""
                    blocks[ev.get("index", len(blocks))] = block
                    if block.get("type") == "tool_result":
                        for item in block.get("content") or []:
                            if isinstance(item, dict):
                                add_result(item, last_query)
                    for c in block.get("citations") or []:
                        add_citation(c)
                elif kind == "content_block_delta":
                    block = blocks.setdefault(ev.get("index", -1), {"type": "text", "_json": ""})
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        block["text"] = block.get("text", "") + delta.get("text", "")
                    elif dtype == "input_json_delta":
                        block["_json"] += delta.get("partial_json", "")
                    elif dtype in ("citation_start_delta", "citations_delta"):
                        add_citation(delta.get("citation") or {})
                elif kind == "content_block_stop":
                    block = blocks.get(ev.get("index", -1)) or {}
                    if block.get("type") == "tool_use" and block.get("name") in _SEARCH_TOOLS:
                        try:
                            q = (json.loads(block["_json"] or "{}") or block.get("input") or {}).get("query")
                        except json.JSONDecodeError:
                            q = None
                        if q:
                            fanouts.append(q)
                            last_query = q
            texts = [b.get("text", "") for _, b in sorted(blocks.items()) if b.get("type") == "text"] or texts
        else:
            # ---- 대화 트리: 마지막 assistant 메시지의 완성된 content 블록
            try:
                tree = json.loads(cap.body)
            except json.JSONDecodeError:
                continue
            messages = [m for m in tree.get("chat_messages") or [] if m.get("sender") == "assistant"]
            if not messages:
                continue
            msg = messages[-1]
            model = model or tree.get("model")
            last_query = None
            tree_texts = []
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "tool_use" and block.get("name") in _SEARCH_TOOLS:
                    if q := (block.get("input") or {}).get("query"):
                        fanouts.append(q)
                        last_query = q
                elif btype == "tool_result":
                    for item in block.get("content") or []:
                        if isinstance(item, dict):
                            add_result(item, last_query)
                elif btype == "text":
                    tree_texts.append(block.get("text", ""))
                    for c in block.get("citations") or []:
                        add_citation(c)
            texts = texts or tree_texts

    return {
        "model": model,
        "text": "".join(texts).strip() or None,
        "fanouts": list(dict.fromkeys(fanouts)),
        "citations": citations,
        "search_results": search_results,
        "url_query": url_query,
    }
