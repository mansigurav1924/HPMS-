from supabase import create_client
from app.config import settings

sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

for table in ["properties", "rooms", "beds", "colleges", "wishlist", "duplicate_flags", "owner_profiles", "student_profiles"]:
    try:
        res = sb.table(table).select("*").limit(1).execute()
        print(f"Table '{table}': EXISTS")
    except Exception as e:
        print(f"Table '{table}': NOT EXISTS or Error ({e})")
