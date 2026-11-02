"""DB 입출력. 응답 한 건과 그 하위 행(citations·fanouts·mentions)을 한 트랜잭션으로 저장한다."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .entities import Entity, Mention
from .models import Citation, Query

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from db import connect  # noqa: E402  (IPv4 세션 풀러 연결, scripts/db.py)

_HANGUL = re.compile(r"[가-힣]")


def guess_language(text: str) -> str:
    return "ko" if _HANGUL.search(text) else "en"


@dataclass
class ResponseRecord:
    query: Query
    engine: str
    batch: str
    run_no: int
    executed_at: datetime
    model: str | None
    settings: dict[str, Any]
    response_text: str | None
    raw: dict[str, Any]
    infocz_mentioned: bool | None
    infocz_rank: int | None
    citations: list[tuple[Citation, Entity | None]] = field(default_factory=list)
    fanout_queries: list[str] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    notes: str | None = None


class Store:
    def __init__(self, conninfo: str | None = None):
        self.conn = psycopg.connect(conninfo, autocommit=True) if conninfo else connect(autocommit=True)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 읽기 ----------
    def load_entities(self) -> list[Entity]:
        rows = self.conn.execute("select id, name, aliases, role, domains from entities").fetchall()
        return [Entity(id, name, tuple(aliases), str(role), tuple(domains))
                for id, name, aliases, role, domains in rows]

    def get_queries(
        self,
        *,
        origin: str = "seed",
        market: str | None = None,
        language: str | None = None,
        ids: list[int] | None = None,
        limit: int | None = None,
    ) -> list[Query]:
        sql = "select id, query_text, language, market::text from queries where origin = %s and is_active"
        params: list[Any] = [origin]
        if market:
            sql += " and market = %s"
            params.append(market)
        if language:
            sql += " and language = %s"
            params.append(language)
        if ids:
            sql += " and id = any(%s)"
            params.append(ids)
        sql += " order by id"
        if limit:
            sql += " limit %s"
            params.append(limit)
        return [Query(*row) for row in self.conn.execute(sql, params).fetchall()]

    def response_exists(self, *, batch: str, query_id: int, engine: str, run_no: int) -> bool:
        # model은 실행 후에야 알 수 있으므로 재개 판단에서는 빼고 본다
        row = self.conn.execute(
            "select 1 from responses where batch = %s and query_id = %s and engine = %s and run_no = %s",
            (batch, query_id, engine, run_no),
        ).fetchone()
        return row is not None

    def existing_runs(self, *, batch: str, engine: str) -> set[tuple[int, int]]:
        """이 배치·엔진에서 이미 저장된 (query_id, run_no) 전부 — 재개할 때 한 번에 걸러 내려고"""
        rows = self.conn.execute(
            "select query_id, run_no from responses where batch = %s and engine = %s", (batch, engine),
        ).fetchall()
        return {(q, r) for q, r in rows}

    # ---------- 쓰기 ----------
    def save_response(self, r: ResponseRecord) -> int:
        with self.conn.transaction():
            (response_id,) = self.conn.execute(
                """
                insert into responses (query_id, engine, model, batch, run_no, settings, executed_at,
                                       response_text, raw, infocz_mentioned, infocz_rank, notes)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (r.query.id, r.engine, r.model, r.batch, r.run_no, Jsonb(r.settings), r.executed_at,
                 r.response_text, Jsonb(r.raw), r.infocz_mentioned, r.infocz_rank, r.notes),
            ).fetchone()

            fanout_ids: dict[str, int] = {}
            fanouts = [t for t in (q.strip() for q in r.fanout_queries) if t]
            for position, text in enumerate(dict.fromkeys(fanouts), start=1):
                fanout_ids[text] = self._upsert_fanout_query(text, parent_id=r.query.id)
                self.conn.execute(
                    "insert into response_fanouts (response_id, query_id, position) values (%s, %s, %s) "
                    "on conflict do nothing",
                    (response_id, fanout_ids[text], position),
                )

            seen_urls: set[str] = set()
            position = 0
            for citation, entity in r.citations:
                if citation.url in seen_urls:
                    continue
                seen_urls.add(citation.url)
                position += 1
                fanout_id = None
                if fq := (citation.fanout_query or "").strip():
                    fanout_id = fanout_ids.get(fq) or self._upsert_fanout_query(fq, parent_id=r.query.id)
                self.conn.execute(
                    """
                    insert into citations (response_id, url, title, position, snippet, fanout_query_id, entity_id)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (response_id, citation.url, citation.title, position, citation.snippet, fanout_id,
                     entity.id if entity else None),
                )

            for m in r.mentions:
                self.conn.execute(
                    "insert into response_mentions (response_id, entity_id, rank, context) values (%s, %s, %s, %s)",
                    (response_id, m.entity.id, m.rank, m.context),
                )
        return response_id

    def _upsert_fanout_query(self, text: str, *, parent_id: int) -> int:
        row = self.conn.execute(
            """
            insert into queries (query_text, language, origin, parent_query_id)
            values (%s, %s, 'fanout', %s)
            on conflict (query_text, parent_query_id) do nothing
            returning id
            """,
            (text, guess_language(text), parent_id),
        ).fetchone()
        if row:
            return row[0]
        (qid,) = self.conn.execute(
            "select id from queries where query_text = %s and parent_query_id = %s", (text, parent_id)
        ).fetchone()
        return qid
