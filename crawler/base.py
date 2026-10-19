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

import gzip
import json
import logging
import random
import re
import shutil
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, Request, Response, sync_playwright

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


class PersonalizationError(BlockedError):
    """메모리·맞춤 지침 등 답변을 바꿀 수 있는 계정 설정이 켜져 있음. 꺼야 측정할 수 있으므로 채널을 중단한다."""


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
        proxy: str | None = None,  # 예: socks5://127.0.0.1:1080 (맥북 역방향 SSH 터널)
        worker: int = 0,  # 병렬 실행 시 워커 번호. 0이 아니면 프로필 복사본을 쓴다
        save_artifacts: bool = True,  # False면 성공한 실행의 증거 파일을 남기지 않는다 (DB에 다 있다). 실패는 항상 남긴다
    ):
        self.store = store
        self.matcher = matcher
        self.language = language
        self.headless = headless
        self.browser_channel = browser_channel
        self.proxy = proxy
        self.save_artifacts = save_artifacts
        self.worker = worker
        self.base_profile_dir = data_dir / "profiles" / self.engine
        # 같은 프로필 디렉터리를 두 브라우저가 동시에 열 수 없어 워커마다 복사본을 쓴다
        self.profile_dir = self.base_profile_dir if worker == 0 else data_dir / "profiles" / f"{self.engine}-w{worker}"
        self.artifacts_root = data_dir / "artifacts"
        self.max_raw_body_chars = max_raw_body_chars

        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._pending: list[Response] = []
        self._capture_res = [re.compile(p) for p in self.capture_patterns]
        self._personalization: dict[str, Any] | None = None  # 세션 첫 실행에서 확인
        self._exit_ip: str | None = None  # 프록시를 쓸 때 실제 나가는 IP (세션 첫 실행에서 확인)

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

    def check_personalization(self, page: Page) -> dict[str, Any]:
        """세션마다 한 번, 답변을 사용자별로 바꾸는 계정 설정(메모리, 대화 기록 참조, 맞춤 지침 등)을 확인.

        켜진 항목이 있으면 PersonalizationError. 확인한 값은 responses.settings.personalization에 남는다.
        로그인하지 않은 채널은 맞춤화가 없으므로 기본 구현은 빈 dict.
        """
        return {}

    # ======================================================================
    # 브라우저 수명주기
    # ======================================================================
    def __enter__(self) -> BaseCrawler:
        if self.profile_dir != self.base_profile_dir and not self.profile_dir.exists() and self.base_profile_dir.exists():
            # 로그인 세션을 이어받는다. 다시 로그인했으면 profiles/<engine>-w* 를 지워야 새 세션이 복사된다
            shutil.copytree(self.base_profile_dir, self.profile_dir,
                            ignore=shutil.ignore_patterns("Singleton*", "*.lock", "lockfile"))
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=self.browser_channel,
            headless=self.headless,
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            proxy={"server": self.proxy} if self.proxy else None,
            **LOCALES[self.language],
        )
        self.context.on("requestfinished", self._on_request_finished)  # 모든 탭에 적용
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

    def import_cookies(self, path: Path) -> int:
        """화면이 없는 서버용 로그인: 다른 PC 브라우저에서 내보낸 쿠키를 프로필에 넣는다.

        Playwright storage_state JSON({"cookies": [...]}) 또는 Cookie-Editor 류 확장의 JSON 배열을 받는다.
        """
        assert self.context and self.page
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        raw = data["cookies"] if isinstance(data, dict) else data
        same_site = {"no_restriction": "None", "unspecified": "Lax", "lax": "Lax", "strict": "Strict", "none": "None"}
        cookies = []
        for c in raw:
            cookie: dict[str, Any] = {
                "name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/"),
                "secure": bool(c.get("secure", False)), "httpOnly": bool(c.get("httpOnly", False)),
            }
            expires = c.get("expires", c.get("expirationDate"))
            if expires and not c.get("session"):
                cookie["expires"] = float(expires)
            if ss := c.get("sameSite"):
                cookie["sameSite"] = same_site.get(str(ss).lower(), ss)
            if cookie.get("sameSite") == "None":
                cookie["secure"] = True
            cookies.append(cookie)
        self.context.add_cookies(cookies)
        self.page.goto(self.start_url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(5000)
        if not self.is_logged_in(self.page):
            raise NotLoggedInError(f"{self.engine}: 쿠키 {len(cookies)}개를 넣었지만 로그인 상태가 확인되지 않음")
        return len(cookies)

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
            if self._personalization is None:
                self._personalization = self.check_personalization(page)
            if self.proxy and self._exit_ip is None:
                self._exit_ip = self._check_exit_ip()
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

        artifacts = self._save_artifacts(page, artifact_base, extraction, captured) if self.save_artifacts else {}
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
            try:  # 스트리밍 중 요소가 교체되는 순간(ChatGPT 검색 중 등)에는 잠깐 없을 수 있다
                current = target.first.inner_text(timeout=2_000) if target.count() else ""
            except Exception:
                current = ""
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
    def walk_json(obj: Any) -> Any:
        """중첩 JSON의 모든 dict를 깊이 우선(등장 순서)으로 순회. 포맷이 자주 바뀌는 스트림에서 키로 찾을 때."""
        stack = [obj]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(reversed(list(node.values())))
            elif isinstance(node, list):
                stack.extend(reversed(node))

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

    def _on_request_finished(self, request: Request) -> None:
        # 본문까지 다 받은 요청만 모은다. 열린 채 남는 스트림(WebChannel 등)을 읽으면 끝없이 기다린다.
        # 핸들러 안에서 읽지 않고 객체만 모아 두었다가 답변 완료 후 읽는다
        if any(p.search(request.url) for p in self._capture_res) and (response := request.response()):
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

    def _check_exit_ip(self) -> str:
        """프록시가 실제로 쓰이는지 확인. 프록시가 죽어 있으면 서버 IP로 새지 않도록 실패시킨다."""
        assert self.context
        try:
            ip = self.context.request.get("https://api.ipify.org", timeout=15_000).text().strip()
        except Exception as e:
            raise BlockedError(f"{self.engine}: 프록시 {self.proxy} 연결 실패 — 터널 확인 ({e!r})") from e
        log.info("%s: 프록시 %s → 나가는 IP %s", self.engine, self.proxy, ip)
        return ip

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
        """증거 보관: 전체 스크린샷(JPEG), 페이지 HTML(gzip), 답변 HTML, 네트워크 원문. 실패해도 실행은 계속.

        응답 8만 건 규모라 PNG·원본 HTML(건당 ~1.3MB)로는 디스크가 버티지 못해 압축해서 남긴다 (건당 ~0.3MB).
        """
        paths: dict[str, str] = {}

        def save(key: str, path: str, write: Callable[[], None]) -> None:
            try:  # 디스크가 차도 측정(DB 저장)은 계속한다
                write()
                paths[key] = path
            except Exception as e:
                log.warning("%s 저장 실패: %r", key, e)

        save("screenshot", f"{base}.jpg",
             lambda: page.screenshot(path=f"{base}.jpg", full_page=True, type="jpeg", quality=60))
        save("html", f"{base}.html.gz",
             lambda: Path(f"{base}.html.gz").write_bytes(gzip.compress(page.content().encode("utf-8"))))
        if extraction and extraction.response_html:
            save("answer_html", f"{base}.answer.html",
                 lambda: Path(f"{base}.answer.html").write_text(extraction.response_html, encoding="utf-8"))
        if captured:
            save("network", f"{base}.network.json.gz", lambda: Path(f"{base}.network.json.gz").write_bytes(
                gzip.compress(json.dumps([c.__dict__ for c in captured], ensure_ascii=False).encode("utf-8"))))
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
            "personalization": self._personalization,
            "proxy": self.proxy,
            "exit_ip": self._exit_ip,
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
