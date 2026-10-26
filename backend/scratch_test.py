import psycopg
from app.config import settings

conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
with conn.cursor() as cur:
    cur.execute("SET session_replication_role = 'replica'")
    cur.execute("""
        DO $$
        DECLARE
            v_bed uuid;
            v_room uuid;
            v_prop uuid;
            v_owner uuid;
            v_student uuid;
            v_status text;
        BEGIN
            v_owner := gen_random_uuid();
            v_student := gen_random_uuid();
            v_prop := gen_random_uuid();
            v_room := gen_random_uuid();
            v_bed := gen_random_uuid();
            
            INSERT INTO users(user_id, email, name, role) VALUES (v_owner, 'o@a.com', 'O', 'owner'), (v_student, 's@a.com', 'S', 'student');
            INSERT INTO properties(property_id, owner_id, name, type, city, address, status, booking_mode) VALUES (v_prop, v_owner, 'P', 'coed', 'C', 'A', 'Approved', 'approval');
            INSERT INTO rooms(room_id, property_id, room_number, room_type, capacity, monthly_rent, status) VALUES (v_room, v_prop, '1', 'single', 1, 1000, 'Active');
            INSERT INTO beds(bed_id, room_id, bed_label, status) VALUES (v_bed, v_room, 'B1', 'available');
            
            INSERT INTO bookings(student_id, property_id, room_id, bed_id, move_in_date, duration_months, rent_amount, deposit_amount, status, requested_at, owner_response_due_at, hold_expires_at)
            VALUES (v_student, v_prop, v_room, v_bed, '2024-01-01', 1, 1000, 1000, 'requested', now(), now(), now());
            
            SELECT status INTO v_status FROM beds WHERE bed_id = v_bed;
            RAISE EXCEPTION 'Bed status after insert: %', v_status;
        END $$;
    """)
