"""인용 URL 본문 수집과 규칙 기반 특징 추출 → pages. 수집하지 못한 URL은 http_status·fetch_error만 남긴다."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
import trafilatura
from lxml import etree, html as lxml_html
from trafilatura.utils import decode_file

from crawler.entities import url_domain

from . import DEFAULT_BATCH, connect

TEXT_KEEP = 4000          # pages.text에 보관할 앞부분 길이. 전체를 저장하면 DB 용량이 감당되지 않는다.
MAX_BYTES = 3_000_000
CHUNK = 200               # 수집 후 한 번에 저장하는 URL 수
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

# ponytail: 단위 목록 기반의 거친 수치 판별이다. 연도·날짜는 세지 않는다. 오탐이 문제되면 단위를 줄인다.
_STAT = re.compile(r"\d+(?:[.,]\d+)*\s?(?:%|퍼센트|배|억|조|만|건|명|원|달러|개|곳|ms|GB|TB|x)(?![A-Za-z])")
_QUOTE = re.compile(r"[“\"「『]([^”\"」』]{10,200}?)[”\"」』]")
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
_MODIFIED_META = ("article:modified_time", "og:updated_time")


def _dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ld_nodes(doc):
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            stack = [json.loads(script.text_content())]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def _guess_lang(text: str) -> str:
    # ponytail: 한국어/영어만 구분한다. 다른 언어가 늘면 py3langid 같은 판별기로 바꾼다.
    sample = text[:2000]
    return "ko" if len(re.findall(r"[가-힣]", sample)) >= len(sample) * 0.1 else "en"


def extract_features(html: str, url: str) -> dict:
    """HTML에서 pages 열에 들어갈 값을 뽑는다. 네트워크를 쓰지 않는 순수 함수."""
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, etree.LxmlError):
        return {}

    types, published, modified = set(), None, None
    for node in _ld_nodes(doc):
        t = node.get("@type")
        types.update([t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)])
        published = published or _dt(node.get("datePublished"))
        modified = modified or _dt(node.get("dateModified"))
    for name in _MODIFIED_META:
        modified = modified or _dt(next(iter(doc.xpath(f'//meta[@property="{name}"]/@content')), None))

    imgs = doc.xpath("//img")
    alt_ratio = sum(1 for i in imgs if (i.get("alt") or "").strip()) / len(imgs) if imgs else None
    page_domain = url_domain(url)
    outlinks = {url_domain(h) for h in doc.xpath("//a/@href") if h.startswith("http")} - {"", page_domain}
    has_video = bool(doc.xpath("//video")) or any(
        any(host in src for host in _VIDEO_HOSTS) for src in doc.xpath("//iframe/@src")
    )
    blockquotes = len(doc.xpath("//blockquote"))
    html_lang = (doc.get("lang") or "").split("-")[0].lower() or None

    meta = trafilatura.extract_metadata(html, default_url=url)
    text = trafilatura.extract(html, url=url, include_tables=True, favor_recall=True)
    if not text:
        for junk in doc.xpath("//script|//style|//noscript"):
            junk.drop_tree()
        text = " ".join(doc.text_content().split())
    text = text.replace("\x00", "")
    title = (getattr(meta, "title", None) or doc.findtext(".//title") or "").replace("\x00", "").strip() or None

    return {
        "title": title,
        "text": text[:TEXT_KEEP],
        "lang": html_lang or getattr(meta, "language", None) or _guess_lang(text),
        "published_at": published or _dt(getattr(meta, "date", None)),
        "modified_at": modified,
        "schema_types": sorted(types),
        "h_count": int(doc.xpath("count(//h1|//h2|//h3|//h4|//h5|//h6)")),
        "img_count": len(imgs),
        "alt_ratio": alt_ratio,
        "table_count": len(doc.xpath("//table")),
        "has_video": has_video,
        "stat_count": len(_STAT.findall(text)),
        "quote_count": len(_QUOTE.findall(text)) + blockquotes,
        "outlink_count": len(outlinks),
        "word_count": len(text.split()),
    }


_NAVER_BLOG = re.compile(r"^https?://blog\.naver\.com/([^/?#]+)/(\d+)")


def _rewrite(url: str) -> str:
    # 데스크톱 네이버 블로그는 본문이 iframe 안에 있어서, 서버 렌더링되는 모바일 주소로 요청한다.
    m = _NAVER_BLOG.match(url)
    return f"https://m.blog.naver.com/{m[1]}/{m[2]}" if m else url


async def _fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, host_sems, url: str) -> dict:
    row: dict = {"url": url}
    target = _rewrite(url)
    async with host_sems[urlsplit(target).hostname], sem:
        try:
            resp = await client.get(target)
        except httpx.HTTPError as e:
            row["fetch_error"] = type(e).__name__
            return row
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    row.update(http_status=resp.status_code, content_type=ctype)
    if resp.status_code == 200 and "html" in ctype:  # PDF 등은 본문 추출을 생략한다
        html = decode_file(resp.content[:MAX_BYTES])
        row.update(await asyncio.to_thread(extract_features, html, url))
    return row


async def _fetch_all(urls: list[str], concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    host_sems = defaultdict(lambda: asyncio.Semaphore(2))  # 같은 호스트에는 동시에 2건까지만
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"},
        follow_redirects=True, timeout=httpx.Timeout(20.0, connect=10.0),
    ) as client:
        return await asyncio.gather(*(_fetch_one(client, sem, host_sems, u) for u in urls))


_COLS = ["http_status", "fetch_error", "content_type", "lang", "title", "text", "published_at", "modified_at",
         "h_count", "img_count", "alt_ratio", "table_count", "has_video", "stat_count", "quote_count",
         "outlink_count", "word_count"]
_UPSERT = (
    f"insert into pages (url, schema_types, {', '.join(_COLS)}) "
    f"values (%(url)s, %(schema_types)s::text[], {', '.join(f'%({c})s' for c in _COLS)}) "
    f"on conflict (url) do update set fetched_at = now(), schema_types = excluded.schema_types, "
    + ", ".join(f"{c} = excluded.{c}" for c in _COLS)
)


def link_citations(conn) -> int:
    return conn.execute(
        "update citations c set page_id = p.id from pages p where p.url = c.url and c.page_id is null"
    ).rowcount


def run(*, batch: str = DEFAULT_BATCH, limit: int | None = None, concurrency: int = 20,
        retry_failed: bool = False, random: bool = False) -> None:
    conn = connect(autocommit=True)
    failed = "or p.http_status is null or p.http_status >= 500" if retry_failed else ""
    order = "random()" if random else "count(*) desc"  # 기본은 많이 인용된 URL부터
    urls = [r[0] for r in conn.execute(
        f"""
        select c.url from citations c
        join responses r on r.id = c.response_id
        left join pages p on p.url = c.url
        where r.batch = %s and (p.id is null {failed})
        group by c.url order by {order} limit %s
        """,
        (batch, limit),
    ).fetchall()]
    print(f"fetch: {len(urls)} URLs")
    for i in range(0, len(urls), CHUNK):
        rows = asyncio.run(_fetch_all(urls[i : i + CHUNK], concurrency))
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, [{c: None for c in _COLS} | {"schema_types": []} | r for r in rows])
        ok = sum(1 for r in rows if r.get("http_status") == 200)
        print(f"fetch: {min(i + CHUNK, len(urls))}/{len(urls)} saved ({ok}/{len(rows)} HTTP 200 in this chunk)")
    print(f"fetch: linked {link_citations(conn)} citations to pages")
    conn.close()
