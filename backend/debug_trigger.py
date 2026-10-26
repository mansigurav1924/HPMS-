import psycopg
import uuid
import json
from app.config import settings

def run_test():
    conn_str = settings.DATABASE_URL
    print(f"Connecting to {conn_str}")
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            from test_phase9 import seed_tenancy
            t1 = seed_tenancy(conn, "checked_out")
            print("Seeded tenancy", t1)
            
            cur.execute("SELECT create_review(%s, %s, 5, 'Great')", (t1["student_id"], t1["tenancy_id"]))
            
            s2 = str(uuid.uuid4())
            cur.execute("INSERT INTO users (user_id, email, role, name, phone) VALUES (%s, %s, 'student', 'Test', '123')", (s2, f"s2_{s2}@t.com"))
            cur.execute("""
                INSERT INTO bookings (booking_id, property_id, student_id, room_id, bed_id, status, move_in_date, rent_amount)
                VALUES (%s, %s, %s, %s, %s, 'confirmed', current_date, 1000)
            """, (str(uuid.uuid4()), t1["property_id"], s2, t1["room_id"], str(uuid.uuid4())))
            
            cur.execute("INSERT INTO reviews (tenancy_id, property_id, student_id, rating) VALUES (%s, %s, %s, %s)",
                        (str(uuid.uuid4()), t1["property_id"], s2, 3))
            
            cur.execute("SELECT * FROM reviews WHERE property_id = %s", (t1["property_id"],))
            reviews = cur.fetchall()
            print("Reviews:", reviews)
            
            cur.execute("SELECT property_id, avg_rating, review_count FROM properties WHERE property_id = %s", (t1["property_id"],))
            prop = cur.fetchone()
            print("Property:", prop)

            # check if trigger exists
            cur.execute("SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'reviews'::regclass")
            print("Triggers on reviews:", cur.fetchall())

if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests")
    run_test()
