"""크롤링 결과 분석: 인용 페이지 수집 → LLM 라벨링 → 집계. 실행은 `python -m analysis --help`."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from db import connect  # noqa: E402,F401  (IPv4 세션 풀러 연결, scripts/db.py)

DEFAULT_BATCH = "baseline-2026-10"  # 분석 대상 측정 회차. dryrun·excluded·invalid 배치는 제외된다.
