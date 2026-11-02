"""분석 CLI.

  python -m analysis fetch --limit 200 --random      # 인용 URL 본문 수집 (파일럿)
  python -m analysis fetch                           # 전체 (많이 인용된 URL부터)
  python -m analysis overlap                         # 질문-페이지 정합성
  python -m analysis label-queries                   # queries.persona 채우기
  python -m analysis label-pages --limit 100         # 직접 호출로 파일럿
  python -m analysis label-pages --api-batch         # 전체는 Batches API로
  python -m analysis label-responses --engine claude --limit 100
  python -m analysis label-axes --limit 100          # analysis 스키마의 페이지·도메인·인용 축 (Jev)
  python -m analysis label-keywords                  # analysis.page_label.topic_keywords (Haiku, label-axes 다음에)
  python -m analysis rules                         # analysis 스키마의 규칙·자동 축 (label-responses 다음에)
  python -m analysis sample                          # 고정 시드 표본으로 위 단계를 모두 실행하고 요약 출력
  python -m analysis report                          # data/analysis/*.csv
"""

import argparse

from . import DEFAULT_BATCH


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m analysis", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, *, batch=True, limit=True, api_batch=False, **kw):
        p = sub.add_parser(name, **kw)
        if batch:
            p.add_argument("--batch", default=DEFAULT_BATCH, help="측정 회차 라벨 (기본 %(default)s)")
        if limit:
            p.add_argument("--limit", type=int, help="처리할 최대 건수")
        if api_batch:
            p.add_argument("--api-batch", action="store_true", help="Message Batches API로 제출 (비용 50%%, 완료까지 최대 24시간)")
        return p

    p = add("fetch", help="인용 URL 본문 수집 + 규칙 기반 특징")
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--random", action="store_true", help="많이 인용된 순서 대신 무작위 (파일럿용)")
    p.add_argument("--retry-failed", action="store_true", help="연결 실패·5xx였던 URL을 다시 시도")
    add("overlap", limit=False, help="질문-페이지 정합성(query_overlap) 계산")
    add("label-queries", batch=False, api_batch=True, help="queries.persona 라벨링")
    p = add("label-pages", batch=False, api_batch=True, help="인용 페이지 라벨링")
    p.add_argument("--version", default=None)
    p = add("label-responses", api_batch=True, help="답변 라벨링 (감정·키워드·서술 정확도)")
    p.add_argument("--engine")
    p.add_argument("--version", default=None)
    add("label-axes", help="analysis 스키마 분류 축 라벨링 (Jev: Format·Topic·Authority·Passage Type 등)")
    add("label-keywords", batch=False, api_batch=True, help="analysis 스키마 페이지 Topic Keywords (Haiku)")
    add("rules", limit=False, help="규칙·자동 축 계산 (Self Authored·Language·Visual·Answer Dominance 등)")
    p = add("sample", limit=False, help="고정 시드 표본(엔진별 인포시즈 인용/비인용 답변)으로 전체 파이프라인 실행 + 요약")
    p.add_argument("--seed", default=None, help="표본 시드 (기본 analysis.sample.SEED)")
    p.add_argument("--per-group", type=int, default=5, help="엔진 × 인용 여부 그룹당 답변 수")
    add("report", limit=False, help="집계 CSV 내보내기")

    a = parser.parse_args()
    if a.cmd == "fetch":
        from . import fetch
        fetch.run(batch=a.batch, limit=a.limit, concurrency=a.concurrency, retry_failed=a.retry_failed,
                  random=a.random)
    elif a.cmd == "overlap":
        from . import metrics
        metrics.run(batch=a.batch)
    elif a.cmd == "label-queries":
        from . import label
        label.label_queries(limit=a.limit, use_batch_api=a.api_batch)
    elif a.cmd == "label-pages":
        from . import label
        label.label_pages(limit=a.limit, version=a.version or label.PAGE_VERSION, use_batch_api=a.api_batch)
    elif a.cmd == "label-responses":
        from . import label
        label.label_responses(batch=a.batch, engine=a.engine, limit=a.limit, version=a.version or label.RESPONSE_VERSION,
                              use_batch_api=a.api_batch)
    elif a.cmd == "label-axes":
        from . import jev
        jev.run(batch=a.batch, limit=a.limit)
    elif a.cmd == "label-keywords":
        from . import label
        label.label_topic_keywords(limit=a.limit, use_batch_api=a.api_batch)
    elif a.cmd == "rules":
        from . import rules
        rules.run(batch=a.batch)
    elif a.cmd == "sample":
        from . import sample
        sample.run(batch=a.batch, seed=a.seed or sample.SEED, per_group=a.per_group)
    elif a.cmd == "report":
        from . import report
        report.run(batch=a.batch)


main()
