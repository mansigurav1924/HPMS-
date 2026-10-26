import psycopg, urllib.parse
pwd = urllib.parse.unquote("N8%25v%40%26UDvK232gb")
print("Password decoded:", pwd[:4] + "***")
# Try Supabase transaction pooler (port 5432 on pooler host)
url = "postgresql://postgres.oppfiozbafwxepryueht:" + urllib.parse.quote(pwd, safe="") + "@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
print("Trying pooler...")
try:
    c = psycopg.connect(url, connect_timeout=10)
    print("Pooler port 5432: OK")
    with c.cursor() as cur:
        cur.execute("SELECT 1")
        print("Query OK:", cur.fetchone())
    c.close()
except Exception as e:
    print("Pooler FAILED:", str(e)[:200])
