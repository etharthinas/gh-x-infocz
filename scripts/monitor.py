"""측정 현황 모니터 (htop 비슷한 터미널 화면).

    .venv/bin/python scripts/monitor.py                 # q로 종료
    .venv/bin/python scripts/monitor.py --once          # 한 번 출력하고 끝 (파이프·로그용)
    .venv/bin/python scripts/monitor.py --batch baseline-2026-10 --runs 10

채널별: 진행(완료/목표), 현재 반복 회차, 속도(최근 15분·60분), ETA, 마지막 저장, 브라우저 수, 러너 상태, 마지막 오류.
Claude 계정별 사용량은 크롤러가 확인할 때마다 남기는 data/state/claude_usage.json을 읽는다 (브라우저를 따로 띄우지 않는다).
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from db import connect

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ENGINES = ("naver_ai_briefing", "gemini", "chatgpt", "claude", "google_aio")
DB_EVERY_S = 30
_PROFILE = re.compile(r"--user-data-dir=(\S+)")
_ACCOUNT_PROFILE = re.compile(r"/accounts/([^/]+)/profiles/([^/]+)$")
_LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


# ---------------------------------------------------------------- 수집
def db_stats(batch: str, runs: int) -> dict:
    with connect() as c:
        target = c.execute("select count(*) from queries where is_active and origin in ('seed', 'control')"
                           ).fetchone()[0] * runs
        rows = c.execute("""
            select engine, count(*),
                   count(*) filter (where executed_at > now() - interval '15 min'),
                   count(*) filter (where executed_at > now() - interval '60 min'),
                   max(executed_at),
                   count(*) filter (where notes = 'no_ai_answer')
            from responses where batch = %s group by engine""", (batch,)).fetchall()
        reps = c.execute("""
            select r.engine, r.run_no, count(*) from responses r join queries q on q.id = r.query_id
            where r.batch = %s and q.origin = 'seed' group by 1, 2""", (batch,)).fetchall()
        seeds = c.execute("select count(*) from queries where is_active and origin = 'seed'").fetchone()[0]
        models = c.execute("""
            select distinct on (engine) engine, model from responses
            where batch = %s and model is not null order by engine, executed_at desc""", (batch,)).fetchall()
    stats = {e: {"done": 0, "r15": 0, "r60": 0, "last": None, "no_ai": 0} for e in ENGINES}
    for engine, done, r15, r60, last, no_ai in rows:
        stats.setdefault(engine, {}).update(done=done, r15=r15, r60=r60, last=last, no_ai=no_ai)
    for engine, run_no, n in reps:
        rep = stats.setdefault(engine, {}).setdefault("reps", {})
        rep[run_no] = n
    for engine, model in models:
        stats.setdefault(engine, {})["model"] = model
    return {"target": target, "seeds": seeds, "engines": stats, "at": datetime.now(timezone.utc)}


def processes() -> dict:
    """채널별 브라우저 수(계정 포함)와 러너 상태를 ps에서 읽는다."""
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout.splitlines()
    browsers: dict[str, list[str]] = {e: [] for e in ENGINES}
    runners: dict[str, list[str]] = {e: [] for e in ENGINES}
    crawlers: dict[str, int] = {e: 0 for e in ENGINES}
    for args in out:
        if not re.match(r"\S*(python3?|chrome)\s", args + " "):  # 셸 명령줄(문자열만 포함)은 건너뛴다
            continue
        if "--user-data-dir=" in args and "--type=" not in args and "chrome" in args:
            path = _PROFILE.search(args).group(1)
            if m := _ACCOUNT_PROFILE.search(path):
                account, engine = m.groups()
            else:
                account, engine = None, re.sub(r"-w\d+$", "", Path(path).name)
            browsers.setdefault(engine, []).append(account or "main")
        elif "data/run_full.py" in args:
            parts = args.split()
            engine = parts[parts.index("data/run_full.py") + 1]
            runners.setdefault(engine, []).append("run_full " + " ".join(p for p in parts if p.startswith("--workers")
                                                                      or p.isdigit()))
        elif "data/run_claude.py" in args:
            parts = args.split()
            lane = parts[parts.index("data/run_claude.py") + 1]
            runners["claude"].append(f"lane {lane}")
        elif re.search(r"-m crawler run (\S+)", args) and "xvfb-run" not in args:
            engine = re.search(r"-m crawler run (\S+)", args).group(1)
            if "--workers" not in args:  # --workers 부모 프로세스는 세지 않는다
                crawlers[engine] = crawlers.get(engine, 0) + 1
    return {"browsers": browsers, "runners": runners, "crawlers": crawlers}


def last_log_event(engine: str) -> tuple[str | None, str | None]:
    """로그 끝에서 가장 최근 오류(시각, 요약)."""
    path = DATA / "logs" / f"{engine}.log"
    if not path.exists():
        return None, None
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 200_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    last_time = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if " ERROR " in line and _LOG_TIME.match(line):
            last_time = _LOG_TIME.match(line).group(1)
            # 바로 뒤 traceback의 마지막 'Error:' 줄이 원인
            cause = next((l for l in lines[i + 1:i + 80] if re.match(r"^\S*(Error|Exception)\b.*: ", l)), line)
            cause = re.sub(r"^[\w.]+(Error|Exception): ", "", cause)
            return last_time, cause.strip()
    return None, None


def claude_usage() -> dict:
    path = DATA / "state" / "claude_usage.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def system() -> dict:
    load = os.getloadavg()
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
    total, avail = int(mem["MemTotal"]) / 2**20, int(mem["MemAvailable"]) / 2**20
    disk = shutil.disk_usage("/")
    try:
        with socket.create_connection(("127.0.0.1", 1080), timeout=0.5):
            tunnel = "up"
    except OSError:
        tunnel = "down"
    return {"load": load, "mem_used": total - avail, "mem_total": total, "disk_free": disk.free / 2**30,
            "cpus": os.cpu_count(), "tunnel": tunnel}


# ---------------------------------------------------------------- 표시
def fmt_eta(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def fmt_ago(ts: datetime | str | None, now: datetime) -> str:
    if ts is None:
        return "—"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.astimezone()
    s = (now - ts).total_seconds()
    sign = "-" if s < 0 else ""
    s = abs(s)
    return sign + (f"{s:.0f}s" if s < 90 else f"{s / 60:.0f}m" if s < 5400 else f"{s / 3600:.1f}h")


def render(stats: dict | None, procs: dict, sysinfo: dict, usage: dict, batch: str, runs: int) -> list[tuple[str, int]]:
    """(텍스트, 스타일) 줄 목록. 스타일: 0 기본, 1 제목, 2 경고, 3 정상, 4 흐림."""
    now = datetime.now(timezone.utc)
    lines: list[tuple[str, int]] = []
    s = sysinfo
    lines.append((f"GEO crawl monitor  {datetime.now():%Y-%m-%d %H:%M:%S}  batch={batch}  runs={runs}", 1))
    lines.append((f"load {s['load'][0]:.2f} {s['load'][1]:.2f} {s['load'][2]:.2f} ({s['cpus']} cpu)   "
                  f"mem {s['mem_used']:.1f}/{s['mem_total']:.1f}G   disk free {s['disk_free']:.2f}G   "
                  f"google tunnel {s['tunnel']}"
                  + (f"   db {fmt_ago(stats['at'], now)} ago" if stats else "   db …"),
                  2 if s["disk_free"] < 0.5 or s["load"][0] > 2 * s["cpus"] else 0))
    lines.append(("", 0))
    lines.append((f"{'CHANNEL':<18}{'DONE':>14}{'%':>7}{'REP':>9}{'/15m':>6}{'/h':>6}{'ETA':>8}"
                  f"{'LAST':>7}  {'BROWSERS':<16}RUNNER / LAST ERROR", 1))
    for engine in ENGINES:
        e = (stats or {}).get("engines", {}).get(engine, {})
        target = stats["target"] if stats else 0
        done = e.get("done", 0)
        rate_h = e.get("r60", 0) or e.get("r15", 0) * 4
        eta = (target - done) / rate_h if rate_h else None
        reps = e.get("reps") or {}
        cur = max((r for r, n in reps.items() if n), default=0)
        rep = f"{cur}:{reps.get(cur, 0) * 100 // max(stats['seeds'], 1)}%" if cur and stats else "—"
        browsers = procs["browsers"].get(engine, [])
        accounts = sorted(set(browsers) - {"main"})
        btxt = f"{len(browsers)}" + (f" ({','.join(a.replace('gh_claude_', 'c') for a in accounts)})" if accounts else "")
        runner = ", ".join(procs["runners"].get(engine, [])) or "STOPPED"
        state = "active" if browsers else ("waiting" if procs["runners"].get(engine) else "stopped")
        err_time, err = last_log_event(engine)
        err_txt = f"  err {err_time[11:16]} {err[:60]}" if err else ""
        style = 3 if state == "active" else 2 if state == "stopped" else 4
        lines.append((f"{engine:<18}{done:>7,}/{target:<6,}{done * 100 / max(target, 1):>6.1f}%{rep:>9}"
                      f"{e.get('r15', 0):>6}{rate_h:>6}{fmt_eta(eta):>8}{fmt_ago(e.get('last'), now):>7}"
                      f"  {btxt:<16}[{state}] {runner}{err_txt}", style))
    lines.append(("", 0))

    # Claude 계정
    lines.append((f"{'CLAUDE ACCOUNT':<18}{'5h':>6}{'resets':>9}{'7d':>6}{'resets':>9}{'cap':>6}{'checked':>9}  STATE", 1))
    in_use = set(procs["browsers"].get("claude", []))
    for account in sorted(usage):
        u = usage[account]
        five, week = u.get("five_hour") or {}, u.get("seven_day") or {}
        cap = u.get("cap")
        pcts = [p for p in (five.get("pct"), week.get("pct")) if p is not None]
        capped = cap is not None and any(p >= cap for p in pcts)
        state = "IN USE" if account in in_use else "capped" if capped else "ready"
        until = lambda r: fmt_ago(r, now).lstrip("-") if r else "—"  # noqa: E731 (리셋까지 남은 시간)
        lines.append((f"{account:<18}{_pct(five.get('pct')):>6}{until(five.get('resets_at')):>9}"
                      f"{_pct(week.get('pct')):>6}{until(week.get('resets_at')):>9}{_pct(cap):>6}"
                      f"{fmt_ago(u.get('checked_at'), now):>9}  {state}",
                      3 if state == "IN USE" else 2 if capped else 0))
    if not usage:
        lines.append(("(사용량 기록 없음 — Claude 크롤러가 --usage-cap으로 돌면 채워진다)", 4))
    chatgpt_model = ((stats or {}).get("engines", {}).get("chatgpt", {}) or {}).get("model")
    lines.append(("", 0))
    lines.append((f"ChatGPT (Plus): 사용량 API 없음 — 모델이 바뀌면(한도 → 작은 모델) 크롤러가 멈춘다. 최근 모델: {chatgpt_model or '—'}", 4))
    lines.append(("q 종료 · 화면 5초, DB 30초마다 갱신", 4))
    return lines


def _pct(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


# ---------------------------------------------------------------- 실행
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="baseline-2026-10")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        for text, _ in render(db_stats(args.batch, args.runs), processes(), system(), claude_usage(), args.batch, args.runs):
            print(text)
        return
    curses.wrapper(lambda scr: loop(scr, args))


def loop(scr, args) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    for i, color in ((1, curses.COLOR_CYAN), (2, curses.COLOR_RED), (3, curses.COLOR_GREEN), (4, 8 if curses.COLORS > 8 else curses.COLOR_WHITE)):
        curses.init_pair(i, color, -1)
    scr.timeout(5000)
    stats, fetched = None, 0.0
    while True:
        if time.monotonic() - fetched > DB_EVERY_S:
            try:
                stats = db_stats(args.batch, args.runs)
            except Exception:
                pass  # 풀러 일시 오류 — 다음 주기에 다시
            fetched = time.monotonic()
        lines = render(stats, processes(), system(), claude_usage(), args.batch, args.runs)
        scr.erase()
        height, width = scr.getmaxyx()
        for y, (text, style) in enumerate(lines[:height - 1]):
            attr = curses.color_pair(style) | (curses.A_BOLD if style == 1 else 0)
            scr.addnstr(y, 0, text, width - 1, attr)
        scr.refresh()
        if scr.getch() in (ord("q"), ord("Q")):
            return


if __name__ == "__main__":
    main()
