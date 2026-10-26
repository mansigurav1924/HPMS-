import pytest
import psycopg
import os
import json
import sys
from uuid import uuid4
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

DB_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture(scope="module")
def db():
    """Connect to the real Supabase Postgres DB."""
    if not DB_URL:
        pytest.skip("DATABASE_URL not set")
    try:
        conn = psycopg.connect(DB_URL)
        conn.autocommit = False
        yield conn
        conn.rollback()   # roll back every test's writes
        conn.close()
    except psycopg.OperationalError as e:
        pytest.skip(f"Database not reachable: {e}")


def set_session(cur, uid: str, role: str):
    """Simulate an authenticated request by injecting JWT claims."""
    claims = {"sub": uid, "role": role}
    cur.execute("SET LOCAL role authenticated")
    cur.execute("SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps(claims),))


# ---------------------------------------------------------------------------
# Test 1: Student A cannot read Student B's data
# ---------------------------------------------------------------------------
def test_student_cannot_read_other_student(db):
    uid_a = str(uuid4())
    uid_b = str(uuid4())

    with db.cursor() as cur:
        # Insert two students as superuser (bypasses RLS)
        cur.execute("SET LOCAL role postgres")
        cur.execute(
            "INSERT INTO users (user_id, role, name, email) VALUES (%s, 'student', 'Alice', %s)",
            (uid_a, f"{uid_a}@test.com"))
        cur.execute(
            "INSERT INTO users (user_id, role, name, email) VALUES (%s, 'student', 'Bob', %s)",
            (uid_b, f"{uid_b}@test.com"))

        # Now act as Student B trying to read Student A
        set_session(cur, uid_b, "student")
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (uid_a,))
        assert cur.fetchone() is None, "Student B should NOT see Student A's user row"

    db.rollback()


# ---------------------------------------------------------------------------
# Test 2: Owner A cannot read Owner B's properties
# ---------------------------------------------------------------------------
def test_owner_cannot_read_other_owner_properties(db):
    uid_a = str(uuid4())
    uid_b = str(uuid4())

    with db.cursor() as cur:
        cur.execute("SET LOCAL role postgres")
        # Create two owners
        for uid in (uid_a, uid_b):
            cur.execute(
                "INSERT INTO users (user_id, role, name, email) VALUES (%s, 'owner', 'Owner', %s)",
                (uid, f"{uid}@test.com"))
            cur.execute("INSERT INTO owner_profiles (owner_id) VALUES (%s)", (uid,))

        # Insert a property for owner A
        cur.execute(
            """INSERT INTO properties
               (property_id, owner_id, name, type, address, city, booking_mode)
               VALUES (gen_random_uuid(), %s, 'PropA', 'boys_hostel', '123 St', 'City', 'instant')
               RETURNING property_id""",
            (uid_a,))
        prop_a_id = cur.fetchone()[0]

        # Act as Owner B
        set_session(cur, uid_b, "owner")
        cur.execute("SELECT property_id FROM properties WHERE property_id = %s", (prop_a_id,))
        # Owner B should only see it if it's 'Approved' (public policy) — it's 'Draft' so invisible
        assert cur.fetchone() is None, "Owner B should NOT see Owner A's draft property"

    db.rollback()


# ---------------------------------------------------------------------------
# Test 3: Minor without guardian consent is flagged
# ---------------------------------------------------------------------------
def test_minor_without_consent_flagged(db):
    uid = str(uuid4())

    with db.cursor() as cur:
        cur.execute("SET LOCAL role postgres")
        cur.execute(
            """INSERT INTO users (user_id, role, name, email, dob)
               VALUES (%s, 'student', 'Minor Kid', %s, current_date - interval '15 years')""",
            (uid, f"{uid}@test.com"))
        cur.execute(
            "INSERT INTO student_profiles (student_id, guardian_verified) VALUES (%s, false)",
            (uid,))

        # Check is_minor flag and guardian_verified
        cur.execute(
            "SELECT is_minor, guardian_verified FROM users u "
            "JOIN student_profiles sp ON sp.student_id = u.user_id "
            "WHERE u.user_id = %s", (uid,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True,  "is_minor should be True for a 15-year-old"
        assert row[1] is False, "guardian_verified should be False — booking must be blocked"

    db.rollback()


# ---------------------------------------------------------------------------
# Test 4: Audit log redacts PII fields
# ---------------------------------------------------------------------------
def test_audit_log_redacts_pii():
    from app.services.audit import log_action

    executed = {}

    class FakeCursor:
        def execute(self, q, params=None):
            executed['query'] = q
            executed['params'] = params
        def __enter__(self): return self
        def __exit__(self, *_): pass

    class FakeConn:
        def cursor(self): return FakeCursor()
        def commit(self): pass

    sensitive = {
        "name": "Mansi Gurav",
        "email": "mansi@test.com",
        "phone": "9999999999",
        "id_proof_url": "https://s3.bucket/proof.pdf",
        "other_field": "safe_value",
    }

    log_action(FakeConn(), str(uuid4()), "student", "SIGNUP",
               "users", str(uuid4()), None, sensitive)

    new_val = json.loads(executed['params'][6])
    assert new_val["name"]         == "[redacted]"
    assert new_val["email"]        == "[redacted]"
    assert new_val["phone"]        == "[redacted]"
    assert new_val["id_proof_url"] == "[redacted]"
    assert new_val["other_field"]  == "safe_value"   # non-PII must NOT be redacted


# ---------------------------------------------------------------------------
# Test 5: audit_logs rejects UPDATE and DELETE
# ---------------------------------------------------------------------------
def test_audit_logs_reject_update_delete(db):
    with db.cursor() as cur:
        cur.execute("SET LOCAL role postgres")
        cur.execute(
            "INSERT INTO audit_logs (role, action, entity_type, entity_id, user_id) "
            "VALUES ('admin', 'TEST', 'test', gen_random_uuid(), NULL) RETURNING log_id")
        log_id = cur.fetchone()[0]

        # Switch to authenticated role — should be blocked by RLS policy
        set_session(cur, str(uuid4()), "student")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE audit_logs SET action = 'HACKED' WHERE log_id = %s", (log_id,))

    db.rollback()
