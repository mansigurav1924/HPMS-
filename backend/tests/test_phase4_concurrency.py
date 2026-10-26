import pytest
import psycopg
import concurrent.futures
from uuid import uuid4
from datetime import date
from app.config import settings

def get_conn():
    c = psycopg.connect(settings.DATABASE_URL)
    c.autocommit = True
    c.execute("SET session_replication_role = 'replica'")
    c.execute("ALTER TABLE bookings ENABLE REPLICA TRIGGER trg_bookings_sync")
    c.execute("ALTER TABLE tenancies ENABLE REPLICA TRIGGER trg_tenancies_sync")
    return c

@pytest.fixture(scope="module")
def setup_data():
    conn = get_conn()
    owner_id = str(uuid4())
    prop_id = str(uuid4())
    room_id = str(uuid4())
    bed_id = str(uuid4())
    
    with conn.cursor() as cur:
        # Create user (owner)
        cur.execute("INSERT INTO users(user_id, email, name, role) VALUES (%s, %s, %s, 'owner') ON CONFLICT DO NOTHING", 
                    (owner_id, f"owner_{owner_id}@test.com", "Test Owner"))
        cur.execute("INSERT INTO owner_profiles(owner_id, verification_status) VALUES (%s, 'approved') ON CONFLICT DO NOTHING", 
                    (owner_id,))
        
        # Create property, room, bed
        cur.execute("""
            INSERT INTO properties(property_id, owner_id, name, type, city, address, status, booking_mode) 
            VALUES (%s, %s, 'Test Prop', 'coed', 'City', 'Test Address', 'Approved', 'approval')
            ON CONFLICT DO NOTHING
        """, (prop_id, owner_id))
        
        cur.execute("""
            INSERT INTO rooms(room_id, property_id, room_number, room_type, capacity, monthly_rent, status) 
            VALUES (%s, %s, '101', 'single', 1, 5000, 'Active')
            ON CONFLICT DO NOTHING
        """, (room_id, prop_id))
        
        cur.execute("""
            INSERT INTO beds(bed_id, room_id, bed_label, status) 
            VALUES (%s, %s, 'Bed 1', 'available')
            ON CONFLICT DO NOTHING
        """, (bed_id, room_id))

    yield {"bed_id": bed_id}
    
    # Cleanup
    with conn.cursor() as cur:
        cur.execute("DELETE FROM properties WHERE property_id = %s", (prop_id,))
        cur.execute("DELETE FROM users WHERE user_id = %s", (owner_id,))
    conn.close()


def test_concurrent_booking_request(setup_data):
    bed_id = setup_data["bed_id"]
    
    # create test-only variant without FOR UPDATE
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE OR REPLACE FUNCTION create_booking_request_no_lock(
                p_student uuid, p_bed uuid, p_move_in date, p_months int)
            RETURNS uuid LANGUAGE plpgsql AS $$
            DECLARE
                v_bed beds%rowtype; v_room rooms%rowtype; v_prop properties%rowtype;
                v_id uuid; v_owner_due timestamptz := now() + interval '24 hours';
            BEGIN
                -- NO FOR UPDATE lock here
                SELECT * INTO v_bed FROM beds WHERE bed_id = p_bed;
                IF NOT FOUND THEN RAISE EXCEPTION 'BED_NOT_FOUND'; END IF;
                SELECT * INTO v_room FROM rooms WHERE room_id = v_bed.room_id;
                SELECT * INTO v_prop FROM properties WHERE property_id = v_room.property_id;
                IF v_prop.status <> 'Approved' OR v_room.status <> 'Active' THEN
                    RAISE EXCEPTION 'NOT_BOOKABLE'; END IF;
                IF v_bed.status <> 'available' THEN RAISE EXCEPTION 'BED_UNAVAILABLE'; END IF;
                PERFORM pg_sleep(0.5);
                INSERT INTO bookings(student_id, property_id, room_id, bed_id, move_in_date,
                    duration_months, rent_amount, deposit_amount, status, requested_at,
                    owner_response_due_at, hold_expires_at)
                VALUES (p_student, v_prop.property_id, v_room.room_id, p_bed, p_move_in, p_months,
                    v_room.monthly_rent, v_room.deposit,
                    CAST(CASE WHEN v_prop.booking_mode = 'instant'::booking_mode THEN 'payment_pending' ELSE 'requested' END AS booking_status),
                    now(), v_owner_due, v_owner_due)
                RETURNING booking_id INTO v_id;
                RETURN v_id;
            END $$;
        """)
    
    # Cleanup any previous bookings for this bed
    with conn.cursor() as cur:
        cur.execute("DELETE FROM bookings WHERE bed_id = %s", (bed_id,))
        cur.execute("UPDATE beds SET status = 'available' WHERE bed_id = %s", (bed_id,))

    def book_bed(func_name, student_id):
        try:
            local_conn = get_conn()
            with local_conn.cursor() as c:
                c.execute("INSERT INTO users(user_id, email, name, role) VALUES (%s, %s, %s, 'student') ON CONFLICT DO NOTHING", 
                          (student_id, f"student_{student_id}@test.com", "Test Student"))
                c.execute(f"SELECT {func_name}(%s, %s, %s, %s)", (student_id, bed_id, date.today(), 6))
            local_conn.close()
            return "SUCCESS"
        except psycopg.errors.RaiseException as e:
            local_conn.close()
            return str(e)
        except psycopg.errors.UniqueViolation as e:
            local_conn.close()
            return f"UNIQUE_VIOLATION: {e}"
        except Exception as e:
            local_conn.close()
            return f"ERROR: {type(e).__name__}: {str(e)}"

    # Test 1: With FOR UPDATE (existing function)
    num_threads = 20
    students = [str(uuid4()) for _ in range(num_threads)]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(book_bed, "create_booking_request", s) for s in students]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    
    successes = [r for r in results if r == "SUCCESS"]
    unavailables = [r for r in results if "BED_UNAVAILABLE" in r]
    
    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}. Results: {results}"
    assert len(unavailables) == 19, f"Expected exactly 19 BED_UNAVAILABLE, got {len(unavailables)}. Results: {results}"
    
    # Clean up for next test
    with conn.cursor() as cur:
        cur.execute("DELETE FROM bookings WHERE bed_id = %s", (bed_id,))
        cur.execute("UPDATE beds SET status = 'available' WHERE bed_id = %s", (bed_id,))

    # Test 2: Without FOR UPDATE (test-only variant)
    students2 = [str(uuid4()) for _ in range(num_threads)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(book_bed, "create_booking_request_no_lock", s) for s in students2]
        results2 = [f.result() for f in concurrent.futures.as_completed(futures)]

    successes2 = [r for r in results2 if r == "SUCCESS"]
    uniques = [r for r in results2 if "UNIQUE_VIOLATION" in r]
    
    assert len(successes2) == 1, f"Expected 1 success, got {len(successes2)}. Results: {results2}"
    assert len(uniques) >= 1, f"Expected at least some UNIQUE_VIOLATION without lock, got {len(uniques)}. Results: {results2}"
    assert len(successes2) + len(uniques) + len([r for r in results2 if "BED_UNAVAILABLE" in r]) == num_threads

    # Test 3: Active tenancy blocks booking
    # Clean up again
    with conn.cursor() as cur:
        cur.execute("DELETE FROM bookings WHERE bed_id = %s", (bed_id,))
        cur.execute("UPDATE beds SET status = 'available' WHERE bed_id = %s", (bed_id,))
        
        # Create active tenancy
        t_id = str(uuid4())
        s_id = str(uuid4())
        cur.execute("INSERT INTO users(user_id, email, name, role) VALUES (%s, %s, %s, 'student') ON CONFLICT DO NOTHING", 
                    (s_id, f"tenant_{s_id}@test.com", "Test Tenant"))
        cur.execute("""
            INSERT INTO bookings(booking_id, student_id, property_id, room_id, bed_id, move_in_date, duration_months, rent_amount, status)
            VALUES (%s, %s, (SELECT property_id FROM rooms WHERE room_id = (SELECT room_id FROM beds WHERE bed_id = %s)), 
                    (SELECT room_id FROM beds WHERE bed_id = %s), %s, '2024-01-01', 6, 5000, 'confirmed')
        """, (t_id, s_id, bed_id, bed_id, bed_id))
        cur.execute("""
            INSERT INTO tenancies(tenancy_id, booking_id, property_id, bed_id, student_id, rent_amount, status)
            VALUES (%s, %s, (SELECT property_id FROM rooms WHERE room_id = (SELECT room_id FROM beds WHERE bed_id = %s)), 
                    %s, %s, 5000, 'active')
        """, (t_id, t_id, bed_id, bed_id, s_id))
        
        # Bed sync trigger updates status to occupied
        cur.execute("SELECT status FROM beds WHERE bed_id = %s", (bed_id,))
        assert cur.fetchone()[0] == 'occupied'

    # Try to book it
    res = book_bed("create_booking_request", str(uuid4()))
    assert "BED_UNAVAILABLE" in res
    
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tenancies WHERE tenancy_id = %s", (t_id,))
    
    conn.close()
