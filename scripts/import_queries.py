"""TSV → queries 테이블 (upsert). 대량 추가·복원용.

  python scripts/import_queries.py new_queries.tsv

필수 열: origin market industry awareness_stage anchor_type constraint_type entities query_text
선택 열: use_case template is_active review_note  (export_queries.py 출력과 호환)

entities: 'A;B' = 쿼리에 나오는 엔티티 (entities.name 그대로)
query_text에 {c}가 있으면 entities는 'A|B>표시명|C' = 변형 목록. 변형마다 쿼리 1개를 만들고
{c}를 표시명(없으면 엔티티명)으로 치환하며, 원문은 template 열에 남긴다.
"""
import csv
import sys
from collections import Counter

from db import connect

AXES = ["market", "industry", "awareness_stage", "anchor_type", "constraint_type"]


def expand(row):
    text, ents = row["query_text"], row["entities"]
    if "{c}" not in text:
        return [(text, row.get("template") or None, [e for e in ents.split(";") if e])]
    out = []
    for variant in ents.split("|"):
        name, _, display = variant.partition(">")
        out.append((text.replace("{c}", display or name), text, [name]))
    return out


def main(path):
    with open(path, encoding="utf-8") as f:
        rows = [(r, *q) for r in csv.DictReader(f, delimiter="\t") for q in expand(r)]
    dupes = [t for t, n in Counter(q[1] for q in rows).items() if n > 1]
    assert not dupes, f"duplicate queries: {dupes}"

    with connect() as conn:
        entity_ids = dict(conn.execute("select name, id from entities").fetchall())
        missing = {e for *_, ents in rows for e in ents} - entity_ids.keys()
        assert not missing, f"unknown entities (add them to entities first): {missing}"

        with conn.cursor() as cur:
            cur.executemany(
                """insert into queries (query_text, language, origin, market, industry, awareness_stage,
                                        anchor_type, constraint_type, use_case, template, is_active, review_note)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (query_text, parent_query_id) do update set
                       origin = excluded.origin, market = excluded.market, industry = excluded.industry,
                       awareness_stage = excluded.awareness_stage, anchor_type = excluded.anchor_type,
                       constraint_type = excluded.constraint_type, use_case = excluded.use_case,
                       template = excluded.template, is_active = excluded.is_active,
                       review_note = excluded.review_note
                   returning id, (xmax = 0)""",
                [(text, "en" if r["market"] == "us_security" else "ko", r["origin"],
                  *[r[k] or None for k in AXES], r.get("use_case") or None, template,
                  r.get("is_active", "true").lower() != "false", r.get("review_note") or None)
                 for r, text, template, _ in rows],
                returning=True,
            )
            results = []
            while True:
                results.append(cur.fetchone())
                if not cur.nextset():
                    break
            cur.executemany(
                "insert into query_entities values (%s, %s) on conflict do nothing",
                [(qid, entity_ids[e]) for (qid, _), (*_, ents) in zip(results, rows) for e in ents],
            )
        print(f"queries: {len(rows)} rows upserted ({sum(new for _, new in results)} new)")


if __name__ == "__main__":
    main(sys.argv[1])
