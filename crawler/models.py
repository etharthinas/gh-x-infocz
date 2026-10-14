"""크롤러 ↔ DB 사이에서 오가는 값 객체."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Query:
    """queries 테이블의 한 행 (크롤러가 쓰는 필드만)."""

    id: int
    text: str
    language: str  # 'ko' | 'en'
    market: str | None = None


@dataclass
class Citation:
    url: str
    title: str | None = None
    snippet: str | None = None
    fanout_query: str | None = None  # 이 인용을 가져온 하위 검색어 (UI·네트워크에서 알 수 있을 때)


@dataclass
class Extraction:
    """채널 크롤러의 extract()가 돌려주는 결과. 저장·후처리는 BaseCrawler가 맡는다."""

    response_text: str | None
    # Google·Naver처럼 AI 요약이 아예 안 뜨는 경우 False. 응답은 그래도 기록한다 (노출률 분모).
    answer_present: bool = True
    response_html: str | None = None
    model: str | None = None
    citations: list[Citation] = field(default_factory=list)  # 답변 내 순서대로
    fanout_queries: list[str] = field(default_factory=list)  # AI가 실제로 날린 하위 검색어, 순서대로
    extra: dict[str, Any] = field(default_factory=dict)  # 채널별 부가 정보 → responses.raw.extra


@dataclass
class CapturedResponse:
    """capture_patterns에 걸린 네트워크 응답 (SSE·JSON). 화면에 안 보이는 fanout·인용 파싱용."""

    url: str
    status: int
    content_type: str
    body: str
