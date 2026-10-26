import sys
sys.path.append("C:/Users/mgura/OneDrive/Desktop/internship/HPMS_portal/backend")
from app.db.session import get_public_db_connection
import psycopg

conn = next(get_public_db_connection())
try:
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
        cols = [r[0] for r in cur.fetchall()]
        print("USERS COLUMNS:", cols)
finally:
    conn.close()
