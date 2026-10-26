import psycopg
import sys
import os
sys.path.insert(0, os.path.abspath('.'))
from app.config import settings

sql = """
ALTER TABLE property_staff ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Staff see own assignments" ON property_staff;
CREATE POLICY "Staff see own assignments" ON property_staff
    FOR SELECT USING (user_id = public.get_auth_uid());
"""

conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
cur = conn.cursor()
cur.execute(sql)
print("Applied RLS successfully")
