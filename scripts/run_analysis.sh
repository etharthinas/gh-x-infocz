#!/usr/bin/env bash
# 분석 전체 실행 (서버용). 서로 의존하지 않는 두 줄기를 동시에 돌리고, 둘 다 끝나면 규칙 축과 리포트를 만든다.
#   A: 질문 persona → 답변 라벨 + 업체 추출 (Haiku, 한 호출)
#   B: 인용 페이지 수집 → 질문-페이지 정합성 → 페이지·도메인·인용 축 (Jev) → 페이지 키워드 (Haiku)
# 단계마다 이미 처리한 항목은 건너뛰므로, 크롤링이 더 쌓인 뒤 다시 실행하면 새 답변만 처리한다.
# 키워드 사전(normalize-keywords)은 임베딩 모델이 필요해 로컬에서 따로 돌린다.
#   nohup scripts/run_analysis.sh > data/logs/analysis.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
step() { echo "$(date '+%F %T') ▶ $*"; $PY -u -m analysis "$@" || { rc=$?; echo "$(date '+%F %T') ✗ $* (exit $rc)"; }; }

( step label-queries; step label-responses ) 2>&1 | sed -u 's/^/[A] /' &
( step fetch --concurrency 40; step overlap; step label-axes; step label-keywords ) 2>&1 | sed -u 's/^/[B] /' &
wait
step rules
step report
echo "$(date '+%F %T') done"
