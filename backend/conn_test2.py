import psycopg, urllib.parse
pwd = urllib.parse.unquote("N8%25v%40%26UDvK232gb")
ref = "oppfiozbafwxepryueht"

# Format 1: session pooler
urls = [
    ("Session pooler 5432", f"postgresql://postgres.{ref}:{urllib.parse.quote(pwd, safe='')}@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"),
    ("Transaction pooler 6543", f"postgresql://postgres.{ref}:{urllib.parse.quote(pwd, safe='')}@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"),
    ("Direct DB 5432 no decode", f"postgresql://postgres:{urllib.parse.quote(pwd, safe='')}@db.{ref}.supabase.co:5432/postgres"),
]

for label, url in urls:
    print(f"\nTrying {label}...")
    try:
        c = psycopg.connect(url, connect_timeout=8)
        print(f"  SUCCESS!")
        c.close()
        break
    except Exception as e:
        print(f"  FAILED: {str(e)[:120]}")
