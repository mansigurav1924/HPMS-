import psycopg
from app.config import settings

def check():
    conn = psycopg.connect(settings.DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.property_id, p.name, p.status, op.verification_status
            FROM properties p
            LEFT JOIN owner_profiles op ON op.owner_id = p.owner_id
        """)
        rows = cur.fetchall()
        for r in rows:
            print(f"ID: {r[0][:8]} | Name: {r[1]} | p.status: '{r[2]}' | op.status: '{r[3]}'")

check()
