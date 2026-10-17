"""화면 있는 PC에서 로그인 → 쿠키 JSON 내보내기. 크롤러 서버(화면 없음)에 옮겨 import_cookies.sh로 넣는다.

DB·.env 없이 Playwright만 있으면 된다.

  pip install playwright && playwright install chromium
  python scripts/export_cookies.py claude chatgpt gemini       # 엔진마다 브라우저가 뜨면 직접 로그인 → Enter
  python scripts/export_cookies.py claude --browser-channel msedge
  scp data/cookies/*.json <server>:gh-x-infocz/data/cookies/

- Google 계정 로그인(Gemini, Google로 로그인하는 ChatGPT·Claude)은 Playwright 기본 Chromium을 막는 경우가 있어
  기본값으로 설치된 Chrome(--browser-channel chrome)을 쓴다. Chrome이 없으면 --browser-channel chromium.
- 결과는 Playwright storage_state 형식({"cookies": [...], "origins": [...]}). 로그인 세션이 그대로 들어 있으니
  커밋·공유하지 않는다 (data/cookies/는 .gitignore).
- 측정용 계정은 맞춤 설정·메모리를 꺼 둔다 (ChatGPT 메모리, Claude 메모리/검색 기록 참조 등).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent

# engine → (로그인 시작 URL, 저장할 쿠키 도메인, 로그인 확인용 쿠키 이름). crawler/channels/*.py의 is_logged_in과 같은 기준
ENGINES = {
    "chatgpt": ("https://chatgpt.com/auth/login", ("chatgpt.com", "openai.com"), "__Secure-next-auth.session-token"),
    "claude": ("https://claude.ai/login", ("claude.ai", "anthropic.com"), "sessionKey"),
    "gemini": ("https://accounts.google.com/ServiceLogin?continue=https://gemini.google.com/app",
               ("google.com", "gemini.google.com"), "__Secure-1PSID"),
}


def logged_in(cookies: list[dict], marker: str) -> bool:
    # ChatGPT 세션 토큰은 길면 .0 .1로 쪼개진다
    return any(c["name"] == marker or c["name"].startswith(marker + ".") for c in cookies)


def keep(cookie: dict, domains: tuple[str, ...]) -> bool:
    host = cookie["domain"].lstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def export(pw, engine: str, out_dir: Path, channel: str | None) -> Path:
    url, domains, marker = ENGINES[engine]
    # 매번 빈 임시 프로필: 다른 계정·사이트 쿠키가 섞이지 않게
    with tempfile.TemporaryDirectory(prefix=f"login-{engine}-") as profile:
        ctx = pw.chromium.launch_persistent_context(
            profile, channel=None if channel == "chromium" else channel, headless=False,
            viewport={"width": 1280, "height": 900}, args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        while True:
            answer = input(f"[{engine}] 브라우저에서 로그인을 마치고 Enter (건너뛰려면 s): ").strip().lower()
            if answer == "s":
                ctx.close()
                raise SystemExit(f"{engine}: 건너뜀")
            if logged_in(ctx.cookies(), marker):
                break
            print(f"  아직 로그인 쿠키({marker})가 없습니다. 로그인 완료 후 다시 Enter.")
        state = ctx.storage_state()
        ctx.close()

    state["cookies"] = [c for c in state["cookies"] if keep(c, domains)]
    state["origins"] = [o for o in state.get("origins", []) if any(d in o["origin"] for d in domains)]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{engine}.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    path.chmod(0o600)
    print(f"  → {path} (쿠키 {len(state['cookies'])}개)")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("engines", nargs="*", default=list(ENGINES), choices=list(ENGINES),
                        help="기본: 전부")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "cookies")
    parser.add_argument("--browser-channel", default="chrome", help="chrome(기본) | msedge | chromium")
    args = parser.parse_args()

    done = []
    with sync_playwright() as pw:
        for engine in args.engines:
            try:
                done.append(export(pw, engine, args.out_dir, args.browser_channel))
            except SystemExit as e:
                print(e, file=sys.stderr)
    if done:
        print("\n서버로 복사:  scp " + " ".join(str(p) for p in done) + " <server>:gh-x-infocz/data/cookies/")
        print("서버에서:     scripts/import_cookies.sh")


if __name__ == "__main__":
    main()
