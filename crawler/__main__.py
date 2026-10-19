"""사용법
    python -m crawler login chatgpt                       # 헤드풀 브라우저로 직접 로그인 (최초 1회)
    python -m crawler run chatgpt --batch baseline-2026-10 --runs 3 --market kr_ax --limit 10
    python -m crawler run naver_ai_briefing --batch baseline-2026-10 --runs 10 --workers 3   # 쿼리를 3개 프로세스로 나눠 실행
    python -m crawler run google_aio --batch baseline-2026-10 --proxy socks5://127.0.0.1:1080
"""

from __future__ import annotations

import argparse
import logging
import random
import subprocess
import sys
from pathlib import Path

from . import channels  # noqa: F401  (채널 등록)
from .base import BaseCrawler, BlockedError, CrawlerError
from .entities import EntityMatcher
from .store import Store

log = logging.getLogger("crawler")

# 연달아 이만큼 실패하면 채널을 멈춘다 (프록시 끊김·로그인 만료 등으로 쿼리만 소진되지 않게)
MAX_CONSECUTIVE_FAILURES = 5


def _spawn_workers(n: int) -> None:
    """같은 인자로 --shard i/n 프로세스 n개를 띄우고 모두 끝날 때까지 기다린다.

    Playwright sync API는 스레드 간 공유가 안 되므로 스레드 대신 프로세스로 나눈다.
    """
    argv, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
        elif a == "--workers":
            skip = True
        elif not a.startswith("--workers="):
            argv.append(a)
    procs = [subprocess.Popen([sys.executable, "-m", "crawler", *argv, "--shard", f"{i}/{n}"]) for i in range(n)]
    codes = [p.wait() for p in procs]
    if any(codes):
        log.warning("워커 종료 코드: %s", codes)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m crawler")
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("engine")
    common.add_argument("--browser-channel", help="'chrome'이면 설치된 Chrome 사용")
    common.add_argument("--data-dir", type=Path, default=Path("data"))
    common.add_argument("--account", help="같은 엔진의 다른 로그인 계정 이름 (예: acct1). 프로필은 data/accounts/<account>/profiles/<engine>")

    login = sub.add_parser("login", parents=[common])
    login.add_argument("--cookies", type=Path,
                       help="화면 없는 서버: 다른 PC에서 내보낸 쿠키 JSON을 프로필에 넣는다 (헤드리스로 실행)")
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
    run.add_argument("--workers", type=int, default=1,
                     help="병렬 프로세스 수. 쿼리를 나눠 맡고, 워커마다 브라우저 프로필 복사본(profiles/<engine>-wN)을 쓴다")
    run.add_argument("--shard", help=argparse.SUPPRESS)  # 'i/n' — --workers가 내부적으로 붙인다
    run.add_argument("--no-artifacts", action="store_true",
                     help="성공한 실행의 스크린샷·HTML을 저장하지 않는다 (응답·인용·원문은 DB에 있다). 실패한 실행은 저장")
    run.add_argument("--reverse", action="store_true", help="쿼리를 역순으로. 같은 배치를 두 번째 계정으로 동시에 돌릴 때")
    run.add_argument("--shuffle", metavar="SEED",
                     help="쿼리 순서를 SEED로 섞는다. 여러 계정이 같은 배치를 동시에 돌 때 계정마다 다른 SEED")
    run.add_argument("--usage-cap", type=float,
                     help="Claude: 5시간·주간 사용량이 이 %%를 넘으면 그 계정을 멈춘다 (예: 50). 러너가 나중에 다시 시도한다")
    run.add_argument("--proxy", help="브라우저 프록시, 예: socks5://127.0.0.1:1080. settings.proxy·exit_ip에 남는다")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cls = BaseCrawler.registry.get(args.engine)
    if cls is None:
        parser.error(f"알 수 없는 engine '{args.engine}'. 등록됨: {sorted(BaseCrawler.registry)}")

    if args.cmd == "login":  # DB 불필요
        with cls(None, EntityMatcher([]), headless=bool(args.cookies), browser_channel=args.browser_channel,
                 data_dir=args.data_dir, account=args.account) as crawler:
            if args.cookies:
                log.info("%s: 쿠키 %d개 저장, 로그인 확인됨", args.engine, crawler.import_cookies(args.cookies))
            else:
                crawler.login()
        return

    if args.workers > 1 and not args.shard:
        _spawn_workers(args.workers)
        return
    worker, shards = map(int, args.shard.split("/")) if args.shard else (0, 1)

    with Store() as store:
        matcher = EntityMatcher(store.load_entities())
        queries = store.get_queries(origin=args.origin, market=args.market, language=args.language,
                                    ids=args.ids, limit=args.limit)[worker::shards]
        if args.reverse:  # 같은 배치를 다른 계정으로 동시에 돌릴 때 반대쪽 끝부터 (겹침 최소화)
            queries.reverse()
        if args.shuffle:  # 여러 계정·프로세스가 같은 배치를 나눠 돌 때 서로 다른 순서로 (겹침 최소화)
            random.Random(f"{args.shuffle}-{worker}").shuffle(queries)
        failures = 0
        # locale은 브라우저 실행 옵션이라 언어별로 세션을 나눈다
        for language in ("ko", "en"):
            batch_queries = [q for q in queries if q.language == language]
            if not batch_queries:
                continue
            with cls(store, matcher, language=language, headless=args.headless,
                     browser_channel=args.browser_channel, data_dir=args.data_dir,
                     proxy=args.proxy, worker=worker, save_artifacts=not args.no_artifacts,
                     account=args.account) as crawler:
                crawler.usage_cap = args.usage_cap
                # run_no 바깥 루프: 같은 쿼리를 연달아 묻지 않도록 회차별로 한 바퀴씩
                for run_no in range(1, args.runs + 1):
                    for q in batch_queries:
                        try:
                            if crawler.run(q, batch=args.batch, run_no=run_no, skip_existing=not args.no_skip):
                                crawler.pause_between_runs()
                            failures = 0
                        except BlockedError:
                            log.exception("차단됨 — %s 중단", args.engine)
                            return
                        except CrawlerError:
                            log.exception("실패 q%s r%s — 다음 쿼리로", q.id, run_no)
                            failures += 1
                            if failures >= MAX_CONSECUTIVE_FAILURES:
                                log.error("%d번 연속 실패 — %s 중단", failures, args.engine)
                                return


if __name__ == "__main__":
    main()
