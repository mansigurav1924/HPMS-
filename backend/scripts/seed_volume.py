import psycopg
import os
import uuid
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
conn_str = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

print("Seeding large volume data (Phase 11)...")
conn = psycopg.connect(conn_str)

owner_id = uuid.uuid4()
admin_id = uuid.uuid4()

with conn.cursor() as cur:
    # 1. Create one admin and one owner
    print("Generating 5000 students...")
    cur.execute("SET session_replication_role = 'replica';")
    students = [(uuid.uuid4(), f"student_{i}@hpms.com", f"Student {i}", "student") for i in range(5000)]
    cur.execute("INSERT INTO users (user_id, email, name, role) VALUES (%s, 'admin_load@hpms.com', 'Load Admin', 'admin') ON CONFLICT DO NOTHING", (admin_id,))
    cur.execute("INSERT INTO users (user_id, email, name, role) VALUES (%s, 'owner_load@hpms.com', 'Load Owner', 'owner') ON CONFLICT DO NOTHING", (owner_id,))
    cur.execute("INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'approved') ON CONFLICT DO NOTHING", (owner_id,))
    cur.executemany("INSERT INTO users (user_id, email, name, role) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", students)

    print("Generating 50 properties...")
    props = []
    for i in range(50):
        prop_id = uuid.uuid4()
        props.append((
            prop_id, owner_id, f"Property {i}", "coed", "Address", "City", 
            random.uniform(28.0, 29.0), random.uniform(77.0, 78.0), "Draft"
        ))
    cur.executemany(
        """INSERT INTO properties (property_id, owner_id, name, type, address, city, latitude, longitude, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""", props
    )

    print("Generating rooms and beds (2000 total)...")
    rooms, beds = [], []
    for prop in props:
        prop_id = prop[0]
        for r in range(20): # 20 rooms per property = 1000 rooms
            room_id = uuid.uuid4()
            rooms.append((room_id, prop_id, f"Room {r}", 1, "double", 2, True, True, 5000, 10000))
            for b in range(2): # 2 beds per room = 2000 beds
                beds.append((uuid.uuid4(), room_id, f"{r}-{b}"))
                
    cur.executemany("INSERT INTO rooms (room_id, property_id, room_number, floor, room_type, capacity, is_ac, attached_bath, monthly_rent, deposit) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", rooms)
    cur.executemany("INSERT INTO beds (bed_id, room_id, bed_label) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", beds)

    print("Approving properties...")
    cur.execute("UPDATE properties SET status = 'Approved'")

    print("Generating bookings (3000) and tenancies (1500)...")
    # For speed, skip real transition logic, insert directly
    # 3000 pending bookings, 1500 active tenancies
    bookings = []
    for i in range(4500):
        student_id = students[i][0]
        bed_id = beds[i%2000][0]
        room_id = beds[i%2000][1]
        prop_id = next(r[1] for r in rooms if r[0] == room_id)
        b_id = uuid.uuid4()
        status = "approved" if i < 1500 else "requested"
        bookings.append((b_id, student_id, prop_id, room_id, bed_id, status, datetime.utcnow().date(), 5000, 10000))
        
    tenancies = []
    for i in range(1500):
        b = bookings[i]
        t_id = uuid.uuid4()
        tenancies.append((t_id, b[0], b[1], b[2], b[3], b[4], "active", datetime.utcnow().date(), 5000))
        
    cur.executemany("INSERT INTO bookings (booking_id, student_id, property_id, room_id, bed_id, status, move_in_date, rent_amount, deposit_amount) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", bookings)
    cur.executemany("INSERT INTO tenancies (tenancy_id, booking_id, student_id, property_id, bed_id, status, check_in_date, rent_amount) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", [(*t[:4], *t[5:]) for t in tenancies])
    
    print("Generating complaints (3000)...")
    complaints = []
    cats = ['Electrical', 'Plumbing', 'Cleaning', 'WiFi']
    for i in range(3000):
        t = tenancies[i % 1500]
        c_id = uuid.uuid4()
        complaints.append((c_id, t[0], t[1], t[2], random.choice(cats), f"Issue {i}", "open", False))
        
    cur.executemany("INSERT INTO complaints (complaint_id, tenancy_id, student_id, property_id, category, description, status, escalated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", complaints)

conn.commit()
conn.close()
print("Seeding complete.")
