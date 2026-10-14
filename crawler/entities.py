"""엔티티 레지스트리 기반 언급·인용 도메인 매칭."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# 순위를 매기는 역할: 추천 목록에 '벤더'로 등장하는 쪽만. 생태계·고객사는 언급만 기록.
RANKED_ROLES = {"self", "competitor"}


@dataclass(frozen=True)
class Entity:
    id: int
    name: str
    aliases: tuple[str, ...]
    role: str
    domains: tuple[str, ...]


@dataclass(frozen=True)
class Mention:
    entity: Entity
    offset: int  # 첫 등장 위치
    rank: int | None
    context: str


def _term_pattern(term: str) -> re.Pattern[str]:
    if not term.isascii():
        return re.compile(re.escape(term))  # 한국어는 단어 경계가 없으므로 부분 일치
    # 짧은 대문자 약어(GOS, KT, AIP, JSON, AWS)는 대소문자 구분 — 'gos', 'json' 같은 일반 단어 오탐 방지
    flags = 0 if (len(term) <= 4 and term.isupper()) else re.IGNORECASE
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", flags)


def url_domain(url: str) -> str:
    """schema.sql의 citations.domain 생성식과 같은 규칙 (소문자, www. 제거)."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


class EntityMatcher:
    def __init__(self, entities: list[Entity]):
        self.entities = entities
        self._patterns = [
            (e, [_term_pattern(t) for t in (e.name, *e.aliases) if t]) for e in entities
        ]

    def mentions(self, text: str, context_chars: int = 80) -> list[Mention]:
        found: list[tuple[int, Entity]] = []
        for entity, patterns in self._patterns:
            offsets = [m.start() for p in patterns if (m := p.search(text))]
            if offsets:
                found.append((min(offsets), entity))
        found.sort(key=lambda x: x[0])

        result, rank = [], 0
        for offset, entity in found:
            entity_rank = None
            if entity.role in RANKED_ROLES:
                rank += 1
                entity_rank = rank
            lo, hi = max(0, offset - context_chars), offset + context_chars
            result.append(Mention(entity, offset, entity_rank, text[lo:hi].strip()))
        return result

    def entity_for_url(self, url: str) -> Entity | None:
        domain = url_domain(url)
        if not domain:
            return None
        for entity in self.entities:
            for d in entity.domains:
                d = d.lower().removeprefix("www.")
                if domain == d or domain.endswith("." + d):
                    return entity
        return None
