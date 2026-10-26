import psycopg
import sys
import os
sys.path.insert(0, os.path.abspath('.'))
from app.config import settings
from supabase import create_client

conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
cur = conn.cursor()

admin_id = '5dd0b737-d82b-46fb-a9ef-7525d29cc309'

cur.execute("""
    INSERT INTO users (user_id, role, name, email, status) 
    VALUES (%s, 'admin', 'Admin', 'admin@hpms.com', 'Active') 
    ON CONFLICT (user_id) DO UPDATE SET role = 'admin';
""", (admin_id,))

# Also update the user's auth metadata via Supabase admin client so JWT is correct
supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
supabase.auth.admin.update_user_by_id(admin_id, {"user_metadata": {"role": "admin"}})

print("Admin user fixed!")
