"""UI 기반 GEO 크롤러 공통 뼈대.

API가 아니라 실제 웹 UI(로그인 세션·locale·검색 UI 그대로)로 질의해야 사용자가 보는 답과 같아진다.
채널 크롤러는 BaseCrawler를 상속해 아래 세 가지만 구현한다.

    submit(page, query)          질문 입력·전송
    wait_until_done(page)        답변 생성 완료까지 대기
    extract(page, captured)      화면·네트워크에서 답변 텍스트, 인용, fanout 추출 → Extraction

브라우저 실행, 로그인 프로필 유지, 차단 감지, 증거(스크린샷·HTML·네트워크 원문) 저장,
엔티티 언급·순위 계산, DB 저장(responses / citations / response_fanouts / response_mentions)은 이 클래스가 맡는다.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, Response, sync_playwright

from .entities import EntityMatcher
from .models import CapturedResponse, Citation, Extraction, Query
from .store import ResponseRecord, Store

log = logging.getLogger(__name__)

# 쿼리 언어 → 브라우저 locale. IP 기반 지역은 바꾸지 못하므로 미국 시장 측정은 VPN/프록시 필요 (settings에 기록)
LOCALES: dict[str, dict[str, str]] = {
    "ko": {"locale": "ko-KR", "timezone_id": "Asia/Seoul"},
    "en": {"locale": "en-US", "timezone_id": "America/New_York"},
}

# 공통 봇 차단 신호. 채널별 신호는 blocked_reason()을 확장
_BLOCK_SELECTORS = {
    "cloudflare_challenge": "iframe[src*='challenges.cloudflare.com'], #challenge-form, #cf-challenge-running",
    "recaptcha": "iframe[src*='recaptcha'], iframe[title*='reCAPTCHA']",
    "hcaptcha": "iframe[src*='hcaptcha.com']",
}
_BLOCK_URL_PATTERNS = {"google_sorry": re.compile(r"google\.[^/]+/sorry/")}


def _repair_mojibake(text: str, content_type: str) -> str:
    """charset 없는 응답(SSE 등)은 Chromium이 Latin-1로 읽어 한글이 깨진다 → UTF-8로 되돌림."""
    if "charset" in content_type.lower():
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            return text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return text


class CrawlerError(Exception):
    """한 건 실행 실패. 러너는 기록하고 다음 쿼리로 넘어간다."""


class BlockedError(CrawlerError):
    """캡차·봇 차단. 같은 채널을 계속 두드리면 악화되므로 러너는 해당 채널을 중단한다."""


class NotLoggedInError(CrawlerError):
    """로그인 세션 만료. `python -m crawler login <engine>`으로 다시 로그인."""


class BaseCrawler(ABC):
    # ---------- 채널이 지정하는 값 ----------
    engine: ClassVar[str]  # engines.code
    start_url: ClassVar[str]
    requires_login: ClassVar[bool] = False
    # 이 정규식에 맞는 네트워크 응답 본문을 모아 extract()에 넘긴다 (SSE 스트림, 검색 JSON 등)
    capture_patterns: ClassVar[tuple[str, ...]] = ()
    answer_timeout_s: ClassVar[float] = 180.0
    delay_between_runs_s: ClassVar[tuple[float, float]] = (8.0, 20.0)

    registry: ClassVar[dict[str, type[BaseCrawler]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "engine" in cls.__dict__:
            BaseCrawler.registry[cls.engine] = cls

    def __init__(
        self,
        store: Store | None,
        matcher: EntityMatcher,
        *,
        language: str = "ko",
        headless: bool = False,
        browser_channel: str | None = None,  # 'chrome'이면 설치된 Chrome 사용 (봇 탐지에 유리)
        data_dir: Path = Path("data"),
        max_raw_body_chars: int = 100_000,
    ):
        self.store = store
        self.matcher = matcher
        self.language = language
        self.headless = headless
        self.browser_channel = browser_channel
        self.profile_dir = data_dir / "profiles" / self.engine
        self.artifacts_root = data_dir / "artifacts"
        self.max_raw_body_chars = max_raw_body_chars

        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._pending: list[Response] = []
        self._capture_res = [re.compile(p) for p in self.capture_patterns]

    # ======================================================================
    # 채널이 구현할 훅
    # ======================================================================
    def open_fresh(self, page: Page, query: Query) -> None:
        """이전 대화 맥락이 섞이지 않은 새 화면으로 이동. 기본: start_url."""
        page.goto(self.start_url, wait_until="domcontentloaded")

    def is_logged_in(self, page: Page) -> bool:
        return True

    @abstractmethod
    def submit(self, page: Page, query: Query) -> None: ...

    @abstractmethod
    def wait_until_done(self, page: Page) -> None: ...

    @abstractmethod
    def extract(self, page: Page, captured: list[CapturedResponse]) -> Extraction: ...

    def blocked_reason(self, page: Page) -> str | None:
        for name, pattern in _BLOCK_URL_PATTERNS.items():
            if pattern.search(page.url):
                return name
        for name, selector in _BLOCK_SELECTORS.items():
            if page.locator(selector).count():
                return name
        return None

    def extra_settings(self, page: Page) -> dict[str, Any]:
        """채널별 실행 조건 (모델 선택기 값, 웹검색 토글 등) → responses.settings."""
        return {}

    # ======================================================================
    # 브라우저 수명주기
    # ======================================================================
    def __enter__(self) -> BaseCrawler:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=self.browser_channel,
            headless=self.headless,
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            **LOCALES[self.language],
        )
        self.context.on("response", self._on_response)  # 모든 탭에 적용
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.context:
            self.context.close()
        if self._pw:
            self._pw.stop()

    def login(self, wait: Callable[[], None] = lambda: input("로그인을 마친 뒤 Enter… ")) -> None:
        """헤드풀 브라우저에서 사람이 직접 로그인 → 프로필 디렉터리에 세션이 남는다."""
        assert self.page
        self.page.goto(self.start_url)
        wait()
        if not self.is_logged_in(self.page):
            raise NotLoggedInError(f"{self.engine}: 로그인 상태가 확인되지 않음")

    # ======================================================================
    # 실행 (템플릿 메서드)
    # ======================================================================
    def run(self, query: Query, *, batch: str, run_no: int = 1, skip_existing: bool = True) -> int | None:
        """쿼리 한 건 실행 → 저장된 responses.id. 이미 있으면 None."""
        assert self.page, "with 블록 안에서 실행"
        if skip_existing and self.store and self.store.response_exists(
            batch=batch, query_id=query.id, engine=self.engine, run_no=run_no
        ):
            log.info("skip %s q%s r%s (이미 있음)", self.engine, query.id, run_no)
            return None

        page = self._fresh_page()
        artifact_base = self._artifact_base(batch, query, run_no)
        executed_at = datetime.now(timezone.utc)
        started = time.monotonic()
        try:
            self.open_fresh(page, query)
            self._raise_if_blocked(page)
            if self.requires_login and not self.is_logged_in(page):
                raise NotLoggedInError(f"{self.engine}: 로그인 필요 (python -m crawler login {self.engine})")
            self.submit(page, query)
            self.wait_until_done(page)
            self._raise_if_blocked(page)
            captured = self._collect_captured()
            extraction = self.extract(page, captured)
        except CrawlerError:
            self._save_artifacts(page, artifact_base.with_name(artifact_base.name + "_FAILED"), None, [])
            raise
        except Exception as e:
            self._save_artifacts(page, artifact_base.with_name(artifact_base.name + "_FAILED"), None, [])
            raise CrawlerError(f"{self.engine} q{query.id} r{run_no}: {e!r}") from e

        artifacts = self._save_artifacts(page, artifact_base, extraction, captured)
        record = self._build_record(
            query, batch=batch, run_no=run_no, executed_at=executed_at, page=page,
            extraction=extraction, captured=captured, artifacts=artifacts,
            elapsed_s=round(time.monotonic() - started, 1),
        )
        response_id = self.store.save_response(record) if self.store else None
        log.info("saved %s q%s r%s → response %s (infocz=%s rank=%s, %d citations, %d fanouts)",
                 self.engine, query.id, run_no, response_id, record.infocz_mentioned, record.infocz_rank,
                 len(record.citations), len(record.fanout_queries))
        return response_id

    def pause_between_runs(self) -> None:
        time.sleep(random.uniform(*self.delay_between_runs_s))

    # ======================================================================
    # 채널 구현용 헬퍼
    # ======================================================================
    @staticmethod
    def type_into(page: Page, box: Locator, text: str, *, submit_key: str | None = "Enter") -> None:
        """contenteditable(ProseMirror 등)·textarea 공용 입력. fill()이 안 먹는 리치 에디터 대응."""
        box.wait_for(state="visible")
        box.click()
        page.keyboard.insert_text(text)
        page.wait_for_timeout(random.randint(300, 900))
        if submit_key:
            page.keyboard.press(submit_key)

    def wait_for_stable(
        self,
        page: Page,
        target: Locator,
        *,
        quiet_s: float = 3.0,
        poll_s: float = 0.5,
        timeout_s: float | None = None,
        done: Callable[[], bool] | None = None,
    ) -> str:
        """스트리밍 답변이 quiet_s 동안 변하지 않을 때까지 대기 (done()이 True면 즉시 종료). 최종 텍스트 반환."""
        timeout_s = timeout_s or self.answer_timeout_s
        target.first.wait_for(state="attached", timeout=timeout_s * 1000)
        deadline = time.monotonic() + timeout_s
        last, last_change = None, time.monotonic()
        while time.monotonic() < deadline:
            current = target.first.inner_text() if target.count() else ""
            if current != last:
                last, last_change = current, time.monotonic()
            elif current and (time.monotonic() - last_change >= quiet_s or (done and done())):
                return current
            page.wait_for_timeout(poll_s * 1000)
        raise CrawlerError(f"{self.engine}: 답변 대기 시간 초과 ({timeout_s}s)")

    @staticmethod
    def links_in(scope: Locator) -> list[Citation]:
        """영역 안의 외부 링크를 등장 순서대로 Citation으로 (중복 URL 제거)."""
        items = scope.evaluate_all(
            """els => els.flatMap(root => [...root.querySelectorAll('a[href]')]).map(a => ({
                url: a.href,
                title: (a.getAttribute('aria-label') || a.innerText || a.title || '').trim() || null,
            }))"""
        )
        seen, result = set(), []
        for item in items:
            url = item["url"]
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            result.append(Citation(url=url, title=item["title"]))
        return result

    @staticmethod
    def parse_sse(body: str) -> list[Any]:
        """text/event-stream 본문 → data: 줄의 JSON 목록 (JSON 아닌 줄은 건너뜀)."""
        events = []
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return events

    # ======================================================================
    # 내부
    # ======================================================================
    def _fresh_page(self) -> Page:
        """매 실행 새 탭: 이전 대화 DOM·스트림 리스너가 섞이지 않게."""
        assert self.context and self.page
        old = self.page
        self.page = self.context.new_page()
        old.close()
        self._pending.clear()
        return self.page

    def _on_response(self, response: Response) -> None:
        # 핸들러 안에서 본문을 읽으면 스트림 도중이라 잘리므로 객체만 모아 두고 완료 후 읽는다
        if any(p.search(response.url) for p in self._capture_res):
            self._pending.append(response)

    def _collect_captured(self) -> list[CapturedResponse]:
        captured = []
        for resp in self._pending:
            try:
                body = _repair_mojibake(resp.text(), resp.headers.get("content-type", ""))
            except Exception as e:  # 리다이렉트·취소된 요청 등
                body = f"<unreadable: {e!r}>"
            captured.append(CapturedResponse(resp.url, resp.status, resp.headers.get("content-type", ""), body))
        self._pending.clear()
        return captured

    def _raise_if_blocked(self, page: Page) -> None:
        if reason := self.blocked_reason(page):
            raise BlockedError(f"{self.engine}: 차단 감지 ({reason}) — 브라우저에서 직접 풀거나 잠시 중단")

    def _artifact_base(self, batch: str, query: Query, run_no: int) -> Path:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        directory = self.artifacts_root / batch / self.engine
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"q{query.id}_r{run_no}_{stamp}"

    def _save_artifacts(
        self, page: Page, base: Path, extraction: Extraction | None, captured: list[CapturedResponse]
    ) -> dict[str, str]:
        """증거 보관: 전체 스크린샷, 페이지 HTML, 네트워크 원문. 실패해도 실행은 계속."""
        paths: dict[str, str] = {}
        try:
            page.screenshot(path=f"{base}.png", full_page=True)
            paths["screenshot"] = f"{base}.png"
        except Exception as e:
            log.warning("screenshot 실패: %r", e)
        try:
            Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
            paths["html"] = f"{base}.html"
        except Exception as e:
            log.warning("html 저장 실패: %r", e)
        if extraction and extraction.response_html:
            Path(f"{base}.answer.html").write_text(extraction.response_html, encoding="utf-8")
            paths["answer_html"] = f"{base}.answer.html"
        if captured:
            Path(f"{base}.network.json").write_text(
                json.dumps([c.__dict__ for c in captured], ensure_ascii=False), encoding="utf-8")
            paths["network"] = f"{base}.network.json"
        return paths

    def _build_record(
        self, query: Query, *, batch: str, run_no: int, executed_at: datetime, page: Page,
        extraction: Extraction, captured: list[CapturedResponse], artifacts: dict[str, str], elapsed_s: float,
    ) -> ResponseRecord:
        text = extraction.response_text if extraction.answer_present else None
        mentions = self.matcher.mentions(text) if text else []
        self_mention = next((m for m in mentions if m.entity.role == "self"), None)

        settings = {
            "interface": "web_ui",
            "language": self.language,
            **LOCALES[self.language],
            "headless": self.headless,
            "browser": self.browser_channel or "chromium",
            "user_agent": page.evaluate("navigator.userAgent"),
            "logged_in": self.is_logged_in(page) if self.requires_login else None,
            **self.extra_settings(page),
        }
        raw = {
            "answer_present": extraction.answer_present,
            "page_url": page.url,
            "elapsed_s": elapsed_s,
            "artifacts": artifacts,
            "extra": extraction.extra,
            "captured": [
                {"url": c.url, "status": c.status, "content_type": c.content_type,
                 "body": c.body[: self.max_raw_body_chars], "truncated": len(c.body) > self.max_raw_body_chars}
                for c in captured
            ],
        }
        return ResponseRecord(
            query=query,
            engine=self.engine,
            batch=batch,
            run_no=run_no,
            executed_at=executed_at,
            model=extraction.model,
            settings=settings,
            response_text=text,
            raw=raw,
            infocz_mentioned=(self_mention is not None) if extraction.answer_present else None,
            infocz_rank=self_mention.rank if self_mention else None,
            citations=[(c, self.matcher.entity_for_url(c.url)) for c in extraction.citations],
            fanout_queries=extraction.fanout_queries,
            mentions=mentions,
            notes=None if extraction.answer_present else "no_ai_answer",
        )
