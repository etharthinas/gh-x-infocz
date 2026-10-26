"""거시 집계를 CSV로 내보낸다. 뷰(db/analysis.sql)와 라벨 테이블을 질의하고, 결과는 data/analysis/ 아래에 쓴다."""

from __future__ import annotations

import csv
from pathlib import Path

from . import DEFAULT_BATCH, ROOT, connect

# 이름: SQL. 모든 질의는 측정 회차(batch) 하나만 대상으로 하며 %(batch)s로 받는다.
REPORTS = {
    "source_share": "select engine, source_type, n, share from v_engine_source_share where batch = %(batch)s "
                    "order by engine, n desc",
    "citation_rank": "select engine, source_type, n, avg_position from v_engine_citation_rank where batch = %(batch)s "
                     "order by engine, source_type",
    "top_domains": "select engine, domain_rank, domain, n, cum_share from v_domain_concentration "
                   "where batch = %(batch)s and domain_rank <= 30 order by engine, domain_rank",
    "page_age": """select engine, source_type, count(*) as n,
                          round((percentile_cont(0.5) within group (order by page_age_days) / 30.4)::numeric, 1)
                              as median_age_months
                   from v_citations where batch = %(batch)s and page_age_days >= 0
                   group by engine, source_type order by engine, source_type""",
    "page_form": """select engine, page_form, count(*) as n,
                           round(count(*) * 1.0 / sum(count(*)) over (partition by engine), 4) as share
                    from v_citations where batch = %(batch)s and page_form is not null
                    group by engine, page_form order by engine, n desc""",
    "query_overlap": "select engine, source_type, count(query_overlap) as n, round(avg(query_overlap)::numeric, 3) "
                     "as avg_overlap from v_citations where batch = %(batch)s group by engine, source_type "
                     "order by engine, source_type",
    "label_coverage": """select engine, count(*) as citations, count(page_id) as with_page,
                                count(*) filter (where http_status = 200) as fetched_ok,
                                count(page_form) as labeled
                         from v_citations where batch = %(batch)s group by engine order by engine""",
    "mention_rate": """select engine, count(*) as responses, count(response_text) as with_answer,
                              count(*) filter (where infocz_mentioned) as infocz_mentioned,
                              round(avg(infocz_rank)::numeric, 2) as avg_infocz_rank
                       from responses where batch = %(batch)s group by engine order by engine""",
    "self_stance": """select r.engine, m.stance, m.sentiment, count(*) as n
                      from response_mentions m
                      join entities e on e.id = m.entity_id and e.role = 'self'
                      join responses r on r.id = m.response_id
                      where r.batch = %(batch)s and m.stance is not null
                      group by r.engine, m.stance, m.sentiment order by r.engine, n desc""",
    # 인포시즈 서술 정확도: 모델 판정(미검토)과 반영된 값. 사람이 검토해서 responses.accuracy에 반영한다.
    "accuracy_review": """select r.id as response_id, r.engine, q.query_text, l.accuracy_pred, r.accuracy as accuracy_saved,
                                 l.label_version, r.response_text
                          from response_labels l
                          join responses r on r.id = l.response_id
                          join queries q on q.id = r.query_id
                          where r.batch = %(batch)s and l.accuracy_pred is not null
                          order by l.accuracy_pred, r.engine, r.id""",
    "accuracy": "select engine, accuracy, count(*) as n from responses where batch = %(batch)s and accuracy is not null "
                "group by engine, accuracy order by engine, n desc",
}


def run(*, batch: str = DEFAULT_BATCH, out: Path | None = None) -> None:
    out = out or ROOT / "data" / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    conn = connect(autocommit=True)
    for name, sql in REPORTS.items():
        cur = conn.execute(sql, {"batch": batch})
        rows = cur.fetchall()
        path = out / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: 엑셀에서 한글이 깨지지 않는다
            writer = csv.writer(f)
            writer.writerow([d.name for d in cur.description])
            writer.writerows(rows)
        print(f"report: {name}: {len(rows)} rows -> {path}")
    conn.close()
