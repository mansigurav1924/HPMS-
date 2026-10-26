import psycopg
from typing import Generator
from ..config import settings

def get_service_connection() -> Generator[psycopg.Connection, None, None]:
    conn = psycopg.connect(settings.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL role service_role")
        yield conn
    finally:
        conn.close()
