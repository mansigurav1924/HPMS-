"""
Phase 8: Complaints & Maintenance test suite.

Tests (all use a real DB connection via conftest.py):
 1. Full lifecycle: open → acknowledged → in_progress → resolved → closed
 2. Invalid transitions rejected (with correct exception codes)
 3. Resolve without notes rejected
 4. Reopen inside window succeeds; outside window rejected
 5. reopened_count increments correctly
 6. Only active/notice tenancy can raise a complaint
 7. Rating: only owner of complaint, 1-5, once only
 8. Internal notes never appear in student RLS query
 9. Student A cannot see student B's complaints (isolation)
10. Owner A cannot see/edit Owner B's complaints (isolation)
11. Warden sees only assigned property complaints (isolation)
12. Escalation: breach SLA → level 1; still open → level 2; idempotent
13. Auto-close is idempotent; does not close reopened complaints
14. Maintenance block: trigger sets bed to maintenance; done restores it
15. Blocking occupied/booked bed returns BED_IN_USE
"""

import pytest
import psycopg
import uuid
from datetime import date, timedelta
from decimal import Decimal
from psycopg.rows import dict_row

from app.config import settings

# ─── Helpers (reuse seed_tenancy pattern from phase 7) ───────────────────────

def make_conn():
    """Autocommit service-role connection that bypasses auth FK checks."""
    c = psycopg.connect(settings.DATABASE_URL)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET session_replication_role = 'replica'")
    return c


def seed_tenancy(conn, status="active"):
    """
    Minimal insert: users → property → room → bed → booking → tenancy.
    Returns a dict with all created IDs.
    """
    uid = lambda: str(uuid.uuid4())
    owner_id, student_id = uid(), uid()
    prop_id, room_id, bed_id = uid(), uid(), uid()
    booking_id, tenancy_id = uid(), uid()
    move_in = date.today().replace(day=1)

    with conn.cursor() as cur:
        cur.execute("INSERT INTO users(user_id,role,name,email,status) VALUES(%s,'owner',%s,%s,'Active')",
                    (owner_id, f"own_{owner_id[:6]}", f"own_{owner_id[:6]}@t.com"))
        cur.execute("INSERT INTO users(user_id,role,name,email,status) VALUES(%s,'student',%s,%s,'Active')",
                    (student_id, f"stu_{student_id[:6]}", f"stu_{student_id[:6]}@t.com"))
        cur.execute(
            "INSERT INTO properties(property_id,owner_id,name,type,address,city,booking_mode,status,rent_due_day,rent_cycle_mode) "
            "VALUES(%s,%s,'P8Prop','boys_hostel','1 St','City','instant','Approved',1,'calendar')",
            (prop_id, owner_id)
        )
        cur.execute(
            "INSERT INTO rooms(room_id,property_id,room_number,room_type,capacity,monthly_rent) "
            "VALUES(%s,%s,'R1','single',1,3000)",
            (room_id, prop_id)
        )
        cur.execute(
            "INSERT INTO beds(bed_id,room_id,bed_label,status) VALUES(%s,%s,'A','occupied')",
            (bed_id, room_id)
        )
        cur.execute(
            "INSERT INTO bookings(booking_id,student_id,property_id,room_id,bed_id,move_in_date,rent_amount,status) "
            "VALUES(%s,%s,%s,%s,%s,%s,3000,'confirmed')",
            (booking_id, student_id, prop_id, room_id, bed_id, move_in)
        )
        cur.execute(
            "INSERT INTO tenancies(tenancy_id,booking_id,student_id,property_id,bed_id,"
            "check_in_date,expected_checkout_date,rent_amount,status) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,3000,%s)",
            (tenancy_id, booking_id, student_id, prop_id, bed_id,
             move_in, move_in + timedelta(days=365), status)
        )
    return {
        "owner_id": owner_id, "student_id": student_id,
        "prop_id": prop_id, "room_id": room_id, "bed_id": bed_id,
        "booking_id": booking_id, "tenancy_id": tenancy_id,
    }


def raise_complaint_sql(conn, student_id, category="Plumbing", priority="medium"):
    """Directly call raise_complaint DB function and return the UUID."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT raise_complaint(%s, %s::complaint_category, 'Test complaint', %s::complaint_priority, NULL)",
            (student_id, category, priority)
        )
        return cur.fetchone()[0]


def change_status_sql(conn, complaint_id, actor_id, new_status, note=None):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT change_complaint_status(%s, %s, %s::complaint_status, %s)",
            (complaint_id, actor_id, new_status, note)
        )


def fetch_complaint(conn, complaint_id):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM complaints WHERE complaint_id = %s", (complaint_id,))
        return cur.fetchone()


# ─── 1. Full lifecycle ────────────────────────────────────────────────────────

def test_full_lifecycle():
    """open → acknowledged → in_progress → resolved → closed; timestamps set."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]

        cid = raise_complaint_sql(conn, sid)
        c = fetch_complaint(conn, cid)
        assert c["status"] == "open"
        assert c["acknowledged_at"] is None
        assert c["resolved_at"] is None

        change_status_sql(conn, cid, oid, "acknowledged")
        c = fetch_complaint(conn, cid)
        assert c["status"] == "acknowledged"
        assert c["acknowledged_at"] is not None

        change_status_sql(conn, cid, oid, "in_progress")
        assert fetch_complaint(conn, cid)["status"] == "in_progress"

        change_status_sql(conn, cid, oid, "resolved", note="Fixed the pipe")
        c = fetch_complaint(conn, cid)
        assert c["status"] == "resolved"
        assert c["resolved_at"] is not None
        assert "Fixed" in c["resolution_notes"]

        change_status_sql(conn, cid, oid, "closed")
        c = fetch_complaint(conn, cid)
        assert c["status"] == "closed"
        assert c["closed_at"] is not None
    finally:
        conn.close()


# ─── 2. Invalid transitions ───────────────────────────────────────────────────

def test_invalid_transition_open_to_resolved():
    """Cannot jump from open directly to resolved."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        cid = raise_complaint_sql(conn, ids["student_id"])
        with pytest.raises(psycopg.errors.RaiseException, match="INVALID_TRANSITION"):
            change_status_sql(conn, cid, ids["owner_id"], "resolved", note="Done")
    finally:
        conn.close()


def test_invalid_transition_closed_is_terminal():
    """Cannot transition out of closed."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Done")
        change_status_sql(conn, cid, oid, "closed")
        with pytest.raises(psycopg.errors.RaiseException, match="INVALID_TRANSITION"):
            change_status_sql(conn, cid, oid, "acknowledged")
    finally:
        conn.close()


# ─── 3. Resolve requires notes ────────────────────────────────────────────────

def test_resolve_without_notes_rejected():
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        with pytest.raises(psycopg.errors.RaiseException, match="RESOLUTION_NOTES_REQUIRED"):
            change_status_sql(conn, cid, oid, "resolved", note=None)
    finally:
        conn.close()


# ─── 4. Reopen window ────────────────────────────────────────────────────────

def test_reopen_inside_window():
    """Student can reopen a resolved complaint within the window."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Fixed")
        # resolved_at is now(); window is 7 days → should succeed
        change_status_sql(conn, cid, sid, "reopened", note="Still broken")
        c = fetch_complaint(conn, cid)
        assert c["status"] == "reopened"
        assert c["reopened_count"] == 1
        assert c["resolved_at"] is None  # cleared on reopen
    finally:
        conn.close()


def test_reopen_outside_window_rejected():
    """Reopen is rejected if resolved_at is older than reopen_window_days."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Fixed")

        # Backdate resolved_at by 8 days (beyond default 7-day window)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE complaints SET resolved_at = now() - interval '8 days' WHERE complaint_id = %s",
                (cid,)
            )

        with pytest.raises(psycopg.errors.RaiseException, match="REOPEN_WINDOW_EXPIRED"):
            change_status_sql(conn, cid, sid, "reopened")
    finally:
        conn.close()


# ─── 5. reopened_count increments ────────────────────────────────────────────

def test_reopened_count_increments():
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)

        def full_cycle():
            change_status_sql(conn, cid, oid, "acknowledged")
            change_status_sql(conn, cid, oid, "in_progress")
            change_status_sql(conn, cid, oid, "resolved", note="Fixed again")

        full_cycle()
        change_status_sql(conn, cid, sid, "reopened")
        assert fetch_complaint(conn, cid)["reopened_count"] == 1

        # Second cycle
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Fixed for real")
        change_status_sql(conn, cid, sid, "reopened")
        assert fetch_complaint(conn, cid)["reopened_count"] == 2
    finally:
        conn.close()


# ─── 6. No tenancy → no complaint ────────────────────────────────────────────

def test_no_tenancy_cannot_raise_complaint():
    """Student without an active/notice tenancy should get NO_ACTIVE_TENANCY."""
    conn = make_conn()
    try:
        # Create just a student user, no tenancy
        student_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users(user_id,role,name,email,status) VALUES(%s,'student','NoTenantStu','noten@t.com','Active')",
                (student_id,)
            )
        with pytest.raises(psycopg.errors.RaiseException, match="NO_ACTIVE_TENANCY"):
            raise_complaint_sql(conn, student_id)
    finally:
        conn.close()


def test_checked_out_tenancy_cannot_raise_complaint():
    """Checked-out tenancy does not count as active."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn, status="checked_out")
        with pytest.raises(psycopg.errors.RaiseException, match="NO_ACTIVE_TENANCY"):
            raise_complaint_sql(conn, ids["student_id"])
    finally:
        conn.close()


# ─── 7. Rating ───────────────────────────────────────────────────────────────

def test_rating_works_once():
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Done")

        with conn.cursor() as cur:
            cur.execute("SELECT rate_complaint(%s, %s, 4)", (cid, sid))
        assert fetch_complaint(conn, cid)["student_rating"] == 4

        # Second rating should raise ALREADY_RATED
        with pytest.raises(psycopg.errors.RaiseException, match="ALREADY_RATED"):
            with conn.cursor() as cur:
                cur.execute("SELECT rate_complaint(%s, %s, 3)", (cid, sid))
    finally:
        conn.close()


def test_rating_invalid_value():
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Done")
        with pytest.raises(psycopg.errors.RaiseException, match="INVALID_RATING"):
            with conn.cursor() as cur:
                cur.execute("SELECT rate_complaint(%s, %s, 6)", (cid, sid))
    finally:
        conn.close()


def test_rating_only_by_complaint_owner():
    conn = make_conn()
    try:
        ids  = seed_tenancy(conn)
        ids2 = seed_tenancy(conn)  # another student
        sid, oid = ids["student_id"], ids["owner_id"]
        other_sid = ids2["student_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Done")
        with pytest.raises(psycopg.errors.RaiseException, match="UNAUTHORIZED"):
            with conn.cursor() as cur:
                cur.execute("SELECT rate_complaint(%s, %s, 5)", (cid, other_sid))
    finally:
        conn.close()


# ─── 8. Internal notes RLS ───────────────────────────────────────────────────

def test_internal_notes_not_visible_to_student():
    """
    An 'internal' complaint_update must never appear in a query
    scoped to the student's JWT claims (simulated via set_config).
    """
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)

        # Insert an internal update (as owner)
        internal_msg = f"INTERNAL_SECRET_{uuid.uuid4()}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO complaint_updates(complaint_id, author_id, visibility, message) "
                "VALUES(%s, %s, 'internal', %s)",
                (cid, oid, internal_msg)
            )

        # Query as student via RLS: SET JWT claims to student's role
        student_conn = psycopg.connect(settings.DATABASE_URL)
        try:
            with student_conn.cursor(row_factory=dict_row) as cur:
                # Simulate RLS context
                import json
                claims = json.dumps({"sub": sid, "role": "student"})
                cur.execute("SET SESSION ROLE authenticated")
                cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
                cur.execute(
                    "SELECT * FROM complaint_updates WHERE complaint_id = %s",
                    (cid,)
                )
                updates = cur.fetchall()
                messages = [u["message"] for u in updates]
                assert internal_msg not in messages, \
                    f"Internal note leaked to student: {internal_msg}"
                # Only public entries
                for u in updates:
                    assert u["visibility"] == "public"
            student_conn.rollback()
        finally:
            student_conn.close()
    finally:
        conn.close()


# ─── 9. Student isolation ────────────────────────────────────────────────────

def test_student_isolation():
    """Student A cannot see Student B's complaints via the DB function."""
    conn = make_conn()
    try:
        ids_a = seed_tenancy(conn)
        ids_b = seed_tenancy(conn)
        cid_b = raise_complaint_sql(conn, ids_b["student_id"])

        # Query as student A
        student_a_conn = psycopg.connect(settings.DATABASE_URL)
        try:
            import json
            with student_a_conn.cursor() as cur:
                claims = json.dumps({"sub": ids_a["student_id"], "role": "student"})
                cur.execute("SET SESSION ROLE authenticated")
                cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
                cur.execute("SELECT * FROM complaints WHERE complaint_id = %s", (cid_b,))
                assert cur.fetchone() is None, "Student A should not see Student B's complaint"
            student_a_conn.rollback()
        finally:
            student_a_conn.close()
    finally:
        conn.close()


# ─── 10. Owner isolation ─────────────────────────────────────────────────────

def test_owner_isolation():
    """Owner A cannot see/edit Owner B's complaints via RLS."""
    conn = make_conn()
    try:
        ids_a = seed_tenancy(conn)
        ids_b = seed_tenancy(conn)
        cid_b = raise_complaint_sql(conn, ids_b["student_id"])

        owner_a_conn = psycopg.connect(settings.DATABASE_URL)
        try:
            import json
            with owner_a_conn.cursor() as cur:
                claims = json.dumps({"sub": ids_a["owner_id"], "role": "owner"})
                cur.execute("SET SESSION ROLE authenticated")
                cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
                cur.execute("SELECT * FROM complaints WHERE complaint_id = %s", (cid_b,))
                assert cur.fetchone() is None, "Owner A should not see Owner B's complaint"
            owner_a_conn.rollback()
        finally:
            owner_a_conn.close()
    finally:
        conn.close()


# ─── 11. Warden isolation ────────────────────────────────────────────────────

def test_warden_sees_only_assigned_property():
    """Warden sees complaints only for their assigned property."""
    conn = make_conn()
    try:
        ids_a = seed_tenancy(conn)
        ids_b = seed_tenancy(conn)
        cid_a = raise_complaint_sql(conn, ids_a["student_id"])
        cid_b = raise_complaint_sql(conn, ids_b["student_id"])

        warden_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            # Need to temporarily bypass auth FK
            cur.execute("SET session_replication_role = 'replica'")
            cur.execute(
                "INSERT INTO users(user_id,role,name,email,status) VALUES(%s,'warden','Ward','ward@t.com','Active')",
                (warden_id,)
            )
            cur.execute(
                "INSERT INTO property_staff(property_id,user_id,staff_role) VALUES(%s,%s,'warden')",
                (ids_a["prop_id"], warden_id)
            )
            cur.execute("SET session_replication_role = 'origin'")

        warden_conn = psycopg.connect(settings.DATABASE_URL)
        try:
            import json
            with warden_conn.cursor() as cur:
                claims = json.dumps({"sub": warden_id, "role": "warden"})
                cur.execute("SET SESSION ROLE authenticated")
                cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
                cur.execute(
                    "SELECT complaint_id FROM complaints WHERE complaint_id IN (%s, %s)",
                    (cid_a, cid_b)
                )
                visible = [r[0] for r in cur.fetchall()]
                assert str(cid_a) in [str(v) for v in visible], "Warden should see property A complaint"
                assert str(cid_b) not in [str(v) for v in visible], "Warden should NOT see property B complaint"
            warden_conn.rollback()
        finally:
            warden_conn.close()
    finally:
        conn.close()


# ─── 12. Escalation logic ────────────────────────────────────────────────────

def test_escalation_level1():
    """A complaint older than the ack SLA gets escalated to level 1."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        cid = raise_complaint_sql(conn, ids["student_id"], priority="medium")

        # Backdate created_at to simulate SLA breach (medium = 72h resolve, ack = 24h)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE complaints SET created_at = now() - interval '25 hours' WHERE complaint_id = %s",
                (cid,)
            )

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT escalate_overdue_complaints()")
            result = cur.fetchone()["escalate_overdue_complaints"]

        c = fetch_complaint(conn, cid)
        assert c["escalation_level"] >= 1
        assert c["escalated"] is True
    finally:
        conn.close()


def test_escalation_level2():
    """A complaint at 2× the resolve SLA gets escalated to level 2."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        cid = raise_complaint_sql(conn, ids["student_id"], priority="high")

        # high priority resolve SLA = 48h; 2× = 96h
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE complaints SET created_at = now() - interval '97 hours', "
                "escalation_level = 1, escalated = true WHERE complaint_id = %s",
                (cid,)
            )

        with conn.cursor() as cur:
            cur.execute("SELECT escalate_overdue_complaints()")

        c = fetch_complaint(conn, cid)
        assert c["escalation_level"] == 2
    finally:
        conn.close()


def test_escalation_idempotent():
    """Running escalate_overdue_complaints twice does not raise the level above 2."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        cid = raise_complaint_sql(conn, ids["student_id"])

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE complaints SET created_at = now() - interval '200 hours', "
                "escalation_level = 2, escalated = true WHERE complaint_id = %s",
                (cid,)
            )

        # Run twice
        for _ in range(2):
            with conn.cursor() as cur:
                cur.execute("SELECT escalate_overdue_complaints()")

        c = fetch_complaint(conn, cid)
        assert c["escalation_level"] == 2, "Escalation level must not exceed 2"
    finally:
        conn.close()


# ─── 13. Auto-close ──────────────────────────────────────────────────────────

def test_auto_close_resolved_complaint():
    """Resolved complaint past reopen window gets auto-closed."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Done")

        # Backdate resolved_at to 8 days ago (beyond 7-day window)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE complaints SET resolved_at = now() - interval '8 days' WHERE complaint_id = %s",
                (cid,)
            )
            cur.execute("SELECT auto_close_resolved_complaints()")
            count = cur.fetchone()[0]

        assert count >= 1
        assert fetch_complaint(conn, cid)["status"] == "closed"
    finally:
        conn.close()


def test_auto_close_does_not_close_reopened():
    """Auto-close must not close a reopened complaint (it's no longer 'resolved')."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Done")
        change_status_sql(conn, cid, sid, "reopened")  # now status = 'reopened'

        with conn.cursor() as cur:
            cur.execute("SELECT auto_close_resolved_complaints()")

        assert fetch_complaint(conn, cid)["status"] == "reopened"
    finally:
        conn.close()


def test_auto_close_idempotent():
    """Running auto_close twice on the same complaint does not error."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        sid, oid = ids["student_id"], ids["owner_id"]
        cid = raise_complaint_sql(conn, sid)
        change_status_sql(conn, cid, oid, "acknowledged")
        change_status_sql(conn, cid, oid, "in_progress")
        change_status_sql(conn, cid, oid, "resolved", note="Done")
        with conn.cursor() as cur:
            cur.execute("UPDATE complaints SET resolved_at = now() - interval '8 days' WHERE complaint_id = %s", (cid,))
            cur.execute("SELECT auto_close_resolved_complaints()")
            cur.execute("SELECT auto_close_resolved_complaints()")  # second run
        assert fetch_complaint(conn, cid)["status"] == "closed"
    finally:
        conn.close()


# ─── 14. Maintenance block trigger ───────────────────────────────────────────

def test_maintenance_block_sets_bed_to_maintenance():
    """Creating a maintenance task with blocks_availability=true sets bed status to maintenance."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        prop_id = ids["prop_id"]
        room_id = ids["room_id"]

        # Create an extra available bed (not occupied)
        free_bed_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute("SET session_replication_role = 'replica'")
            cur.execute(
                "INSERT INTO beds(bed_id,room_id,bed_label,status) VALUES(%s,%s,'B','available')",
                (free_bed_id, room_id)
            )
            
            # Enable triggers for the maintenance task insert
            cur.execute("SET session_replication_role = 'origin'")
            
            # Create a maintenance task blocking that bed
            cur.execute(
                "INSERT INTO maintenance_tasks(task_id,property_id,room_id,bed_id,title,status,blocks_availability) "
                "VALUES(%s,%s,%s,%s,'Test block','pending',true)",
                (task_id, prop_id, room_id, free_bed_id)
            )
            # Trigger should have set bed status to 'maintenance'
            cur.execute("SELECT status FROM beds WHERE bed_id = %s", (free_bed_id,))
            assert cur.fetchone()[0] == "maintenance"

            # Mark task done → trigger should restore bed to 'available'
            cur.execute(
                "UPDATE maintenance_tasks SET status = 'done' WHERE task_id = %s", (task_id,)
            )
            cur.execute("SELECT status FROM beds WHERE bed_id = %s", (free_bed_id,))
            assert cur.fetchone()[0] == "available"
    finally:
        conn.close()


def test_maintenance_block_occupied_bed_rejected():
    """Blocking an occupied or booked bed returns BED_IN_USE."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        # ids["bed_id"] is occupied (set status='occupied' in seed_tenancy)
        task_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO maintenance_tasks(task_id,property_id,room_id,bed_id,title,status,blocks_availability) "
                "VALUES(%s,%s,%s,%s,'Block occupied','pending',true)",
                (task_id, ids["prop_id"], ids["room_id"], ids["bed_id"])
            )
            with pytest.raises(psycopg.errors.RaiseException, match="BED_IN_USE"):
                cur.execute("SELECT validate_maintenance_block(%s)", (task_id,))
    finally:
        conn.close()


def test_room_level_task_does_not_affect_beds():
    """A room-level task (no bed_id) does not change any bed status."""
    conn = make_conn()
    try:
        ids = seed_tenancy(conn)
        task_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO maintenance_tasks(task_id,property_id,room_id,title,status,blocks_availability) "
                "VALUES(%s,%s,%s,'Room-level task','pending',true)",
                (task_id, ids["prop_id"], ids["room_id"])
            )
            # The occupied bed should remain occupied (not 'maintenance')
            cur.execute("SELECT status FROM beds WHERE bed_id = %s", (ids["bed_id"],))
            assert cur.fetchone()[0] == "occupied"
    finally:
        conn.close()
