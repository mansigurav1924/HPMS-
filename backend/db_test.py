import sys
sys.path.insert(0, '.')
from app.config import settings
import psycopg
print("Connecting to:", settings.DATABASE_URL[:50])
c = psycopg.connect(settings.DATABASE_URL, connect_timeout=10)
print("Connected!")
with c.cursor() as cur:
    cur.execute("SELECT version()")
    row = cur.fetchone()
    print("Version:", row)
    try:
        cur.execute("SET session_replication_role = replica")
        print("FK disable: OK")
    except Exception as e:
        print("FK disable FAILED:", e)
c.close()
print("Done")
