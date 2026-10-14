"""사용법
    python -m crawler login chatgpt                       # 헤드풀 브라우저로 직접 로그인 (최초 1회)
    python -m crawler run chatgpt --batch baseline-2026-10 --runs 3 --market kr_ax --limit 10
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import channels  # noqa: F401  (채널 등록)
from .base import BaseCrawler, BlockedError, CrawlerError
from .entities import EntityMatcher
from .store import Store

log = logging.getLogger("crawler")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m crawler")
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("engine")
    common.add_argument("--browser-channel", help="'chrome'이면 설치된 Chrome 사용")
    common.add_argument("--data-dir", type=Path, default=Path("data"))

    sub.add_parser("login", parents=[common])
    run = sub.add_parser("run", parents=[common])
    run.add_argument("--batch", required=True, help="측정 회차 라벨, 예: baseline-2026-10")
    run.add_argument("--runs", type=int, default=3, help="쿼리당 반복 횟수 (run_no 1..N)")
    run.add_argument("--market")
    run.add_argument("--language", choices=["ko", "en"])
    run.add_argument("--origin", default="seed", choices=["seed", "control"])
    run.add_argument("--ids", type=int, nargs="*")
    run.add_argument("--limit", type=int)
    run.add_argument("--headless", action="store_true")
    run.add_argument("--no-skip", action="store_true", help="이미 있는 (batch, query, engine, run_no)도 다시 실행")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cls = BaseCrawler.registry.get(args.engine)
    if cls is None:
        parser.error(f"알 수 없는 engine '{args.engine}'. 등록됨: {sorted(BaseCrawler.registry)}")

    with Store() as store:
        matcher = EntityMatcher(store.load_entities())
        if args.cmd == "login":
            with cls(store, matcher, browser_channel=args.browser_channel, data_dir=args.data_dir) as crawler:
                crawler.login()
            return

        queries = store.get_queries(origin=args.origin, market=args.market, language=args.language,
                                    ids=args.ids, limit=args.limit)
        # locale은 브라우저 실행 옵션이라 언어별로 세션을 나눈다
        for language in ("ko", "en"):
            batch_queries = [q for q in queries if q.language == language]
            if not batch_queries:
                continue
            with cls(store, matcher, language=language, headless=args.headless,
                     browser_channel=args.browser_channel, data_dir=args.data_dir) as crawler:
                # run_no 바깥 루프: 같은 쿼리를 연달아 묻지 않도록 회차별로 한 바퀴씩
                for run_no in range(1, args.runs + 1):
                    for q in batch_queries:
                        try:
                            if crawler.run(q, batch=args.batch, run_no=run_no, skip_existing=not args.no_skip):
                                crawler.pause_between_runs()
                        except BlockedError:
                            log.exception("차단됨 — %s 중단", args.engine)
                            return
                        except CrawlerError:
                            log.exception("실패 q%s r%s — 다음 쿼리로", q.id, run_no)


if __name__ == "__main__":
    main()
