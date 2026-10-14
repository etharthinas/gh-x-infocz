"""Supabase Postgres 연결 헬퍼.

.env의 connection_string(직접 연결 db.<ref>.supabase.co)은 IPv6 전용이라 IPv4 망에서는 붙지 않는다.
그래서 같은 계정 정보로 IPv4 세션 풀러(aws-0-ap-southeast-2)에 연결한다.
.env에 pooler_host를 넣으면 그 값을 쓴다.
"""
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POOLER_HOST = "aws-0-ap-southeast-2.pooler.supabase.com"


def connect(**kwargs) -> psycopg.Connection:
    env = dotenv_values(ROOT / ".env")
    url = urlparse(env["connection_string"])
    project_ref = url.hostname.split(".")[1]  # db.<ref>.supabase.co
    return psycopg.connect(
        host=env.get("pooler_host", DEFAULT_POOLER_HOST),
        port=5432,
        dbname=url.path.lstrip("/") or "postgres",
        user=f"{url.username}.{project_ref}",
        password=url.password,
        **kwargs,
    )
