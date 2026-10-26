import psycopg
from app.config import settings

def run():
    print("Connecting to:", settings.DATABASE_URL)
    conn = psycopg.connect(settings.DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        with open("../supabase/migrations/20240114000000_maps_cache.sql", "r") as f:
            sql = f.read()
        cur.execute(sql)
    print("Migration applied")

if __name__ == "__main__":
    run()
