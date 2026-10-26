"""스키마 + 참조 데이터(축 라벨·엔진·엔티티) 생성.

  python scripts/setup_db.py                              # 기본 스키마는 없을 때만 생성, db/analysis.sql은 매번 멱등 적용
  python scripts/setup_db.py --reset --i-understand-data-loss
      # 모든 GEO 테이블/타입 삭제 후 재생성. 쿼리·응답·인용이 전부 사라진다 (DB가 유일한 원본).
      # 먼저 python scripts/export_queries.py > backup.tsv 로 백업할 것.
"""
import sys

from db import ROOT, connect

TABLES = ["response_labels", "page_labels", "pages", "response_mentions", "citations", "response_fanouts",
          "responses", "engines", "query_entities", "queries", "entities", "axis_labels"]
TYPES = ["market", "industry", "awareness_stage", "anchor_type", "constraint_type", "query_origin", "entity_role"]


def main():
    with connect() as conn:
        if "--reset" in sys.argv:
            if "--i-understand-data-loss" not in sys.argv:
                sys.exit("--reset deletes every query and response. Add --i-understand-data-loss to confirm.")
            conn.execute("drop table if exists " + ", ".join(TABLES) + " cascade")
            conn.execute("drop type if exists " + ", ".join(TYPES) + " cascade")
        if conn.execute("select to_regclass('public.queries') is not null").fetchone()[0]:
            print("schema already exists; skipping base schema")
        else:
            conn.execute((ROOT / "db/schema.sql").read_text(encoding="utf-8"))
            conn.execute((ROOT / "db/seed_reference.sql").read_text(encoding="utf-8"))
            print("schema + reference data created (queries table is empty)")
        conn.execute((ROOT / "db/analysis.sql").read_text(encoding="utf-8"))  # 멱등: 기존 DB에도 매번 적용
        print("analysis schema applied")


if __name__ == "__main__":
    main()
