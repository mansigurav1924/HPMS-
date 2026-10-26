import pytest
import psycopg
import uuid
import json
from datetime import date, timedelta
from psycopg.rows import dict_row

from app.config import settings

def make_conn():
    c = psycopg.connect(settings.DATABASE_URL)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET session_replication_role = 'replica'")
    return c

def seed_tenancy(conn, status="active"):
    uid = lambda: str(uuid.uuid4())
    owner_id, student_id = uid(), uid()
    prop_id, room_id, bed_id = uid(), uid(), uid()
    booking_id, tenancy_id = uid(), uid()

    with conn.cursor() as cur:
        # Users
        cur.execute("INSERT INTO users (user_id, email, role, name, phone) VALUES (%s, %s, 'owner', 'Owner', '111')", (owner_id, f"o_{owner_id}@t.com"))
        cur.execute("INSERT INTO users (user_id, email, role, name, phone) VALUES (%s, %s, 'student', 'Student', '222')", (student_id, f"s_{student_id}@t.com"))

        # Prop -> Room -> Bed
        cur.execute("INSERT INTO properties (property_id, owner_id, name, type, address, city, booking_mode, status, rent_due_day, rent_cycle_mode) VALUES (%s, %s, 'Test Prop', 'boys_hostel', 'Add', 'City', 'instant', 'Approved', 1, 'calendar')", (prop_id, owner_id))
        cur.execute("INSERT INTO rooms (room_id, property_id, room_number, room_type, capacity, monthly_rent) VALUES (%s, %s, '101', 'shared', 2, 1000)", (room_id, prop_id))
        cur.execute("INSERT INTO beds (bed_id, room_id, bed_label, status) VALUES (%s, %s, '101A', 'occupied')", (bed_id, room_id))

        # Booking -> Tenancy
        cur.execute("""
            INSERT INTO bookings (booking_id, property_id, student_id, room_id, bed_id, status, move_in_date, rent_amount)
            VALUES (%s, %s, %s, %s, %s, 'confirmed', current_date - interval '10 days', 1000)
        """, (booking_id, prop_id, student_id, room_id, bed_id))

        cur.execute("""
            INSERT INTO tenancies (tenancy_id, property_id, booking_id, student_id, bed_id, status, check_in_date, expected_checkout_date, rent_amount)
            VALUES (%s, %s, %s, %s, %s, %s, current_date - interval '10 days', current_date + interval '6 months', 1000)
        """, (tenancy_id, prop_id, booking_id, student_id, bed_id, status))

    return {
        "owner_id": owner_id, "student_id": student_id,
        "property_id": prop_id, "tenancy_id": tenancy_id, "booking_id": booking_id, "room_id": room_id
    }

def test_notices_isolation(conn):
    t1 = seed_tenancy(conn)
    t2 = seed_tenancy(conn)
    
    with conn.cursor() as cur:
        # Owner 1 posts notice
        cur.execute("INSERT INTO notices (property_id, title, body, posted_by) VALUES (%s, 'T1', 'B1', %s) RETURNING notice_id", 
                    (t1["property_id"], t1["owner_id"]))
        n1 = cur.fetchone()[0]
        
        # Owner 2 posts notice
        cur.execute("INSERT INTO notices (property_id, title, body, posted_by) VALUES (%s, 'T2', 'B2', %s) RETURNING notice_id", 
                    (t2["property_id"], t2["owner_id"]))
        n2 = cur.fetchone()[0]
        
        # Test student 1 can see n1 but not n2
        cur.execute("SET LOCAL role authenticated")
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": t1["student_id"], "role": "student"}),))
        
        cur.execute("SELECT notice_id FROM notices")
        notices = [r[0] for r in cur.fetchall()]
        assert n1 in notices
        assert n2 not in notices

def test_expired_notices_hidden(conn):
    t = seed_tenancy(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO notices (property_id, title, body, posted_by, expires_at) VALUES (%s, 'T', 'B', %s, now() - interval '1 day') RETURNING notice_id", 
                    (t["property_id"], t["owner_id"]))
        
        cur.execute("SET LOCAL role authenticated")
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": t["student_id"], "role": "student"}),))
        
        cur.execute("SELECT count(*) FROM notices")
        assert cur.fetchone()[0] == 0

def test_non_owner_cannot_post(conn):
    t = seed_tenancy(conn)
    t_other = seed_tenancy(conn)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL role authenticated")
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": t_other["owner_id"], "role": "owner"}),))
        
        try:
            cur.execute("INSERT INTO notices (property_id, title, body, posted_by) VALUES (%s, 'T', 'B', %s)", 
                        (t["property_id"], t_other["owner_id"]))
            pytest.fail("Should have raised RLS error")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback() # expected

def test_reviews_one_per_tenancy(conn):
    t = seed_tenancy(conn, "checked_out")
    with conn.cursor() as cur:
        cur.execute("SELECT create_review(%s, %s, 5, 'Great') as r1", (t["student_id"], t["tenancy_id"]))
        with pytest.raises(psycopg.errors.RaiseException, match="A review already exists"):
            cur.execute("SELECT create_review(%s, %s, 4, 'Again') as r2", (t["student_id"], t["tenancy_id"]))

def test_reviews_rating_bounds(conn):
    t = seed_tenancy(conn, "active")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="between 1 and 5"):
            cur.execute("SELECT create_review(%s, %s, 6, 'Bad')", (t["student_id"], t["tenancy_id"]))

def test_reviews_avg_rating_update(conn):
    t1 = seed_tenancy(conn, "checked_out")
    with conn.cursor() as cur:
        # We are in replica mode in tests, so normal triggers don't fire.
        # Make the trigger fire even in replica mode for this test.
        cur.execute("ALTER TABLE reviews ENABLE ALWAYS TRIGGER trg_update_property_rating;")
        
        cur.execute("SELECT create_review(%s, %s, 5, 'Great')", (t1["student_id"], t1["tenancy_id"]))
        
        # create another student in same property
        s2 = str(uuid.uuid4())
        cur.execute("INSERT INTO users (user_id, email, role, name, phone) VALUES (%s, %s, 'student', 'Test', '123')", (s2, f"s2_{s2}@t.com"))
        cur.execute("""
            INSERT INTO bookings (booking_id, property_id, student_id, room_id, bed_id, status, move_in_date, rent_amount)
            VALUES (%s, %s, %s, %s, %s, 'confirmed', current_date, 1000)
        """, (str(uuid.uuid4()), t1["property_id"], s2, t1["room_id"], str(uuid.uuid4()))) # skip bed FK safely
        
        # just directly insert review
        cur.execute("INSERT INTO reviews (tenancy_id, property_id, student_id, rating) VALUES (%s, %s, %s, %s)", 
                    (str(uuid.uuid4()), t1["property_id"], s2, 3))
        
        # avg rating should be (5 + 3) / 2 = 4.0
        cur.execute("SELECT avg_rating, review_count FROM properties WHERE property_id = %s", (t1["property_id"],))
        res = cur.fetchone()
        assert float(res[0]) == 4.0
        assert res[1] == 2
        
        # hide a review
        cur.execute("UPDATE reviews SET status = 'Hidden' WHERE student_id = %s", (s2,))
        cur.execute("SELECT avg_rating, review_count FROM properties WHERE property_id = %s", (t1["property_id"],))
        res = cur.fetchone()
        assert float(res[0]) == 5.0
        assert res[1] == 1

def test_audit_logs_append_only(conn):
    with conn.cursor() as cur:
        cur.execute("SET LOCAL role authenticated")
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(uuid.uuid4()), "role": "admin"}),))
        
        cur.execute("""INSERT INTO audit_logs (user_id, role, action, entity_type, entity_id) 
                       VALUES (%s, 'admin', 'test', 'test', %s) RETURNING log_id""", 
                    (str(uuid.uuid4()), str(uuid.uuid4())))
        log_id = cur.fetchone()[0]
        
        # Try to update
        with pytest.raises((psycopg.errors.RaiseException, psycopg.errors.InsufficientPrivilege)):
            cur.execute("UPDATE audit_logs SET action = 'tampered' WHERE log_id = %s", (log_id,))
            
        

def test_audit_logs_pii_redaction(conn):
    from app.services.audit import log_action
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        prev = {"name": "Alice", "age": 20}
        newv = {"name": "Alice M", "age": 21}
        log_action(conn, uid, "student", "UPDATE", "users", uid, prev, newv)
        
        cur.execute("SELECT previous_value, new_value FROM audit_logs WHERE user_id = %s ORDER BY timestamp DESC LIMIT 1", (uid,))
        res = cur.fetchone()
        pv = res[0]
        nv = res[1]
        
        assert "Alice" not in str(pv)
        assert "[redacted]" in pv["name"]
        assert "sha256" in pv["name"]
        assert pv["age"] == 20

def test_notification_missing_var():
    from app.services.notifications import NotificationService, NotificationError
    conn = make_conn()
    svc = NotificationService(conn)
    with pytest.raises(NotificationError, match="Missing variable 'missing'"):
        svc.render("Hello {{missing}}", {"other": "value"})

def test_webhooks_idempotency(conn):
    from app.routers.notifications import email_webhook, WebhookPayload
    from fastapi import Request
    from unittest.mock import MagicMock
    
    req = MagicMock(spec=Request)
    req.headers = {"X-Webhook-Signature": "test-sig"}
    
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, email, role, name, phone) VALUES (%s, %s, 'student', 'Test', '123')", (uid, "bounce@t.com"))
        
    data = WebhookPayload(event_id="ev_1", event="bounce", email="bounce@t.com", reason="Hard bounce")
    
    # Process 1
    res = email_webhook(req, data, conn)
    assert res["status"] == "processed"
    
    # Process 2 (idempotent)
    res = email_webhook(req, data, conn)
    assert res["status"] == "already_processed"
    
    with conn.cursor() as cur:
        cur.execute("SELECT email_bounced FROM users WHERE email = 'bounce@t.com'")
        assert cur.fetchone()[0] is True
        
        cur.execute("SELECT reason FROM suppression_list WHERE email = 'bounce@t.com'")
        assert cur.fetchone()[0] == "bounce: Hard bounce"
