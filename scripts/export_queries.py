"""queries 테이블 → TSV (stdout). 백업·검토용, import_queries.py로 다시 넣을 수 있다.

  python scripts/export_queries.py > queries_backup.tsv
  python scripts/export_queries.py --origin seed --active-only > seed.tsv
"""
import csv
import sys

from db import connect

COLS = ["id", "origin", "market", "industry", "awareness_stage", "anchor_type", "constraint_type",
        "use_case", "entities", "query_text", "template", "is_active", "review_note"]


def main():
    where, params = ["q.origin <> 'fanout'"], []
    if "--origin" in sys.argv:
        where, params = ["q.origin = %s"], [sys.argv[sys.argv.index("--origin") + 1]]
    if "--active-only" in sys.argv:
        where.append("q.is_active")
    rows = connect().execute(
        f"""select q.id, q.origin, q.market, q.industry, q.awareness_stage, q.anchor_type, q.constraint_type,
                   q.use_case, coalesce(string_agg(e.name, ';' order by e.name), ''), q.query_text, q.template,
                   q.is_active, q.review_note
            from queries q
            left join query_entities qe on qe.query_id = q.id
            left join entities e on e.id = qe.entity_id
            where {' and '.join(where)}
            group by q.id order by q.id""",
        params,
    ).fetchall()
    w = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    w.writerow(COLS)
    w.writerows([["" if v is None else v for v in r] for r in rows])


if __name__ == "__main__":
    main()
