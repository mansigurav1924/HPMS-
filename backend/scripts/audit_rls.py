import psycopg
import sys
import os
from dotenv import load_dotenv

load_dotenv()

# Uses psycopg 3
conn_str = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

try:
    conn = psycopg.connect(conn_str)
except Exception as e:
    print(f"Failed to connect to database: {e}")
    sys.exit(1)

with conn.cursor() as cur:
    print("--- TABLES WITHOUT RLS ---")
    cur.execute("""
        SELECT tablename 
        FROM pg_tables 
        WHERE schemaname = 'public' AND rowsecurity = false;
    """)
    no_rls = cur.fetchall()
    for row in no_rls:
        print(f"WARNING: Table '{row[0]}' does not have RLS enabled.")
        
    print("\n--- PERMISSIVE POLICIES ---")
    cur.execute("""
        SELECT schemaname, tablename, policyname, roles, cmd, qual, with_check 
        FROM pg_policies 
        WHERE schemaname = 'public' AND (qual::text LIKE '%true%' OR with_check::text LIKE '%true%');
    """)
    permissive = cur.fetchall()
    for row in permissive:
        print(f"WARNING: Policy '{row[2]}' on table '{row[1]}' might be too permissive. Roles: {row[3]}")
