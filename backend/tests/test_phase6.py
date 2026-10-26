import pytest
import psycopg
import uuid
from app.config import settings
import datetime

@pytest.fixture
def conn():
    connection = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    yield connection
    connection.close()

def test_proration(conn):
    # Test prorated_amount function directly
    with conn.cursor() as cur:
        # mid-month move in, 30 day month
        # 2024-04-16 to 2024-04-30 (15 days remaining in 30 day month)
        cur.execute("SELECT prorated_amount(10000, '2024-04-16', '2024-04-30')")
        assert float(cur.fetchone()[0]) == 5000.00
        
        # 31 day month
        # 2024-05-16 to 2024-05-31 (16 days remaining in 31 day month)
        cur.execute("SELECT prorated_amount(10000, '2024-05-16', '2024-05-31')")
        assert float(cur.fetchone()[0]) == 5161.29
        
        # 29 day month (leap year 2024)
        # 2024-02-15 to 2024-02-29 (15 days remaining in 29 day month)
        cur.execute("SELECT prorated_amount(10000, '2024-02-15', '2024-02-29')")
        assert float(cur.fetchone()[0]) == 5172.41
        
        # 28 day month (2023)
        # 2023-02-15 to 2023-02-28 (14 days remaining in 28 day month)
        cur.execute("SELECT prorated_amount(10000, '2023-02-15', '2023-02-28')")
        assert float(cur.fetchone()[0]) == 5000.00

def setup_data_for_checkin(conn, status="confirmed", id_proof="http://proof"):
    owner_id = str(uuid.uuid4())
    student_id = str(uuid.uuid4())
    prop_id = str(uuid.uuid4())
    room_id = str(uuid.uuid4())
    bed_id = str(uuid.uuid4())
    booking_id = str(uuid.uuid4())
    
    with conn.cursor() as cur:
        cur.execute("SET session_replication_role = 'replica'")
        cur.execute("ALTER TABLE tenancies ENABLE REPLICA TRIGGER trg_tenancies_sync")
        cur.execute("ALTER TABLE bookings ENABLE REPLICA TRIGGER trg_bookings_sync")
        
        cur.execute("INSERT INTO users(user_id, role, name, email) VALUES (%s, 'owner', 'O', %s)", (owner_id, f"o{owner_id}@x.com"))
        cur.execute("INSERT INTO users(user_id, role, name, email) VALUES (%s, 'student', 'S', %s)", (student_id, f"s{student_id}@x.com"))
        cur.execute("INSERT INTO owner_profiles(owner_id, verification_status) VALUES (%s, 'approved')", (owner_id,))
        cur.execute("INSERT INTO student_profiles(student_id, id_proof_url) VALUES (%s, %s)", (student_id, id_proof))
        
        cur.execute("INSERT INTO properties(property_id, owner_id, name, type, address, city, status, booking_mode) VALUES (%s, %s, 'P', 'coed', 'A', 'C', 'Approved', 'instant')", (prop_id, owner_id))
        cur.execute("INSERT INTO rooms(room_id, property_id, room_number, room_type, capacity, monthly_rent, status) VALUES (%s, %s, 'R', 'single', 1, 1000, 'Active')", (room_id, prop_id))
        cur.execute("INSERT INTO beds(bed_id, room_id, bed_label, status) VALUES (%s, %s, 'B1', 'available')", (bed_id, room_id))
        
        cur.execute("INSERT INTO bookings(booking_id, student_id, property_id, room_id, bed_id, status, move_in_date, duration_months, rent_amount) VALUES (%s, %s, %s, %s, %s, %s, current_date, 6, 1000)",
                    (booking_id, student_id, prop_id, room_id, bed_id, status))
        
        cur.execute("SET session_replication_role = 'origin'")
        cur.execute("ALTER TABLE tenancies ENABLE TRIGGER trg_tenancies_sync")
        cur.execute("ALTER TABLE bookings ENABLE TRIGGER trg_bookings_sync")
    return owner_id, student_id, booking_id, bed_id

def test_check_in_success(conn):
    owner_id, student_id, booking_id, bed_id = setup_data_for_checkin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT check_in(%s, %s, current_date, 5000, 'Cash', '123')", (booking_id, owner_id))
        tenancy_id = cur.fetchone()[0]
        
        # verify booking status
        cur.execute("SELECT status FROM bookings WHERE booking_id = %s", (booking_id,))
        assert cur.fetchone()[0] == 'checked_in'
        
        # verify tenancy created
        cur.execute("SELECT status, deposit_paid FROM tenancies WHERE tenancy_id = %s", (tenancy_id,))
        t = cur.fetchone()
        assert t[0] == 'active'
        assert t[1] == 5000
        
        # verify bed occupied
        cur.execute("SELECT status FROM beds WHERE bed_id = %s", (bed_id,))
        assert cur.fetchone()[0] == 'occupied'
        
        # verify ledger
        cur.execute("SELECT count(*) FROM deposit_ledger WHERE tenancy_id = %s AND entry_type = 'collected'", (tenancy_id,))
        assert cur.fetchone()[0] == 1
        
        # verify payment
        cur.execute("SELECT count(*) FROM payments WHERE booking_id = %s AND type = 'deposit'", (booking_id,))
        assert cur.fetchone()[0] == 1

def test_check_in_no_id_proof(conn):
    owner_id, student_id, booking_id, bed_id = setup_data_for_checkin(conn, id_proof=None)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="ID_PROOF_MISSING"):
            cur.execute("SELECT check_in(%s, %s, current_date, 5000, 'Cash', '123')", (booking_id, owner_id))

def test_check_in_invalid_status(conn):
    owner_id, student_id, booking_id, bed_id = setup_data_for_checkin(conn, status="requested")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="INVALID_STATUS"):
            cur.execute("SELECT check_in(%s, %s, current_date, 5000, 'Cash', '123')", (booking_id, owner_id))

def test_notice_and_checkout(conn):
    owner_id, student_id, booking_id, bed_id = setup_data_for_checkin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT check_in(%s, %s, current_date, 5000, 'Cash', '123')", (booking_id, owner_id))
        tenancy_id = cur.fetchone()[0]
        
        # notice
        cur.execute("SELECT give_notice(%s, %s, current_date)", (tenancy_id, student_id))
        cur.execute("SELECT status, last_stay_date FROM tenancies WHERE tenancy_id = %s", (tenancy_id,))
        t = cur.fetchone()
        assert t[0] == 'notice'
        # Notice period default 30 days
        assert t[1] == datetime.date.today() + datetime.timedelta(days=30)
        
        # Checkout partial deduction
        cur.execute("SELECT check_out(%s, %s, current_date, '[{\"amount\": 1000, \"reason\": \"Damage\"}]'::jsonb)", (tenancy_id, owner_id))
        cur.execute("SELECT status FROM tenancies WHERE tenancy_id = %s", (tenancy_id,))
        assert cur.fetchone()[0] == 'checked_out'
        
        # Verify refund created
        cur.execute("SELECT amount FROM refunds WHERE tenancy_id = %s", (tenancy_id,))
        assert cur.fetchone()[0] == 4000
        
        # Bed should be available
        cur.execute("SELECT status FROM beds WHERE bed_id = %s", (bed_id,))
        assert cur.fetchone()[0] == 'available'

def test_eviction(conn):
    owner_id, student_id, booking_id, bed_id = setup_data_for_checkin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT check_in(%s, %s, current_date, 5000, 'Cash', '123')", (booking_id, owner_id))
        tenancy_id = cur.fetchone()[0]
        
        cur.execute("SELECT evict(%s, %s, 'Violated rules', '[]'::jsonb)", (tenancy_id, owner_id))
        cur.execute("SELECT status FROM tenancies WHERE tenancy_id = %s", (tenancy_id,))
        t = cur.fetchone()
        assert t[0] == 'evicted'
        
        cur.execute("SELECT status FROM beds WHERE bed_id = %s", (bed_id,))
        assert cur.fetchone()[0] == 'available'

def test_concurrency_room_transfer():
    import concurrent.futures
    
    # Setup
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    owner_id, student_id, booking_id, bed1_id = setup_data_for_checkin(conn)
    
    with conn.cursor() as cur:
        cur.execute("SELECT check_in(%s, %s, current_date, 5000, 'Cash', '123')", (booking_id, owner_id))
        tenancy_id = cur.fetchone()[0]
        
        # Create a second bed
        bed2_id = str(uuid.uuid4())
        cur.execute("SELECT room_id FROM beds WHERE bed_id = %s", (bed1_id,))
        room_id = cur.fetchone()[0]
        cur.execute("INSERT INTO beds(bed_id, room_id, bed_label, status) VALUES (%s, %s, 'B2', 'available')", (bed2_id, room_id))
    
    def transfer(b_id):
        try:
            with psycopg.connect(settings.DATABASE_URL, autocommit=True) as local_conn:
                with local_conn.cursor() as cur:
                    cur.execute("SELECT room_transfer(%s, %s, %s, current_date)", (tenancy_id, owner_id, b_id))
                    return True
        except Exception as e:
            return str(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(transfer, bed2_id) for _ in range(10)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
        
    successes = [r for r in results if r is True]
    print("Results:", results)
    assert len(successes) == 1
    
    conn.close()
