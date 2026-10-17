#!/usr/bin/env bash
# 크롤러 서버에서: data/cookies/<engine>.json (export_cookies.py 결과)을 크롤러 프로필에 넣고 로그인 확인.
#   scripts/import_cookies.sh                 # data/cookies/에 있는 엔진 전부
#   scripts/import_cookies.sh claude          # 지정한 엔진만
set -euo pipefail
cd "$(dirname "$0")/.."

dir=data/cookies
engines=("$@")
if [ ${#engines[@]} -eq 0 ]; then
  for f in "$dir"/*.json; do [ -e "$f" ] && engines+=("$(basename "$f" .json)"); done
fi
[ ${#engines[@]} -gt 0 ] || { echo "$dir/*.json 없음 — PC에서 scripts/export_cookies.py 먼저 실행" >&2; exit 1; }

failed=0
for engine in "${engines[@]}"; do
  echo "== $engine"
  .venv/bin/python -m crawler login "$engine" --cookies "$dir/$engine.json" || { echo "!! $engine 실패" >&2; failed=1; }
done
exit $failed
