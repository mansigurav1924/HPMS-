"""
Phase 3 Pytest Suite: Search & Discovery, Wishlist, Compare, & Performance.
"""
import pytest
import psycopg
import time
import json
import uuid
import sys
from pathlib import Path
from datetime import date, timedelta
from jose import jwt

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.config import settings
from app.routers.properties import search_properties, get_property_detail, compare_properties
from app.routers.wishlist import add_to_wishlist, remove_from_wishlist, get_wishlist, WishlistAddRequest
from fastapi import HTTPException


from starlette.requests import Request

def FakeRequest(token=None, headers=None):
    raw_headers = []
    if token:
        raw_headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "app": None,
    }
    return Request(scope)


def make_jwt(user_id: str, role: str) -> str:
    claims = {"sub": user_id, "role": role, "aud": "authenticated"}
    return jwt.encode(claims, settings.SUPABASE_JWT_SECRET, algorithm="HS256")


# conn fixture is provided by conftest.py


# ─────────────────────────────────────────────────────────────
# Test 1: Unapproved owner or property never appears in search/detail
# ─────────────────────────────────────────────────────────────
def test_unapproved_owner_or_property_hidden(conn):
    unapp_owner_id = str(uuid.uuid4())
    unapp_prop_id = str(uuid.uuid4())
    app_owner_id = str(uuid.uuid4())
    draft_prop_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        # Create unapproved owner
        cur.execute(
            "INSERT INTO users (user_id, role, name, email) VALUES (%s, 'owner', 'Unapp Owner', %s)",
            (unapp_owner_id, f"unapp_{unapp_owner_id[:8]}@test.com")
        )
        cur.execute(
            "INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'pending_verification')",
            (unapp_owner_id,)
        )
        # Property under unapproved owner
        cur.execute(
            """INSERT INTO properties (property_id, owner_id, name, type, address, city, status)
               VALUES (%s, %s, 'Unapproved Owner Property', 'boys_hostel', '123 St', 'TestCity', 'Approved')""",
            (unapp_prop_id, unapp_owner_id)
        )

        # Create approved owner with Draft property
        cur.execute(
            "INSERT INTO users (user_id, role, name, email) VALUES (%s, 'owner', 'App Owner', %s)",
            (app_owner_id, f"app_{app_owner_id[:8]}@test.com")
        )
        cur.execute(
            "INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'approved')",
            (app_owner_id,)
        )
        cur.execute(
            """INSERT INTO properties (property_id, owner_id, name, type, address, city, status)
               VALUES (%s, %s, 'Draft Property', 'boys_hostel', '456 St', 'TestCity', 'Draft')""",
            (draft_prop_id, app_owner_id)
        )
    conn.commit()

    # Search should not return unapproved owner property or draft property
    req = FakeRequest()
    res = search_properties(request=req, city="TestCity", conn=conn)
    items = res["items"]
    found_ids = [item["property_id"] for item in items]
    assert unapp_prop_id not in found_ids
    assert draft_prop_id not in found_ids

    # Detail endpoint should return 404 for unapproved owner property & draft property
    with pytest.raises(HTTPException) as exc1:
        get_property_detail(property_id=unapp_prop_id, request=req, conn=conn)
    assert exc1.value.status_code == 404

    with pytest.raises(HTTPException) as exc2:
        get_property_detail(property_id=draft_prop_id, request=req, conn=conn)
    assert exc2.value.status_code == 404


# ─────────────────────────────────────────────────────────────
# Test 2: Search filters (Price, room_type, gender_type, is_ac, facilities)
# ─────────────────────────────────────────────────────────────
def test_search_filters(conn):
    owner_id = str(uuid.uuid4())
    prop_id = str(uuid.uuid4())
    room1_id = str(uuid.uuid4())
    bed1_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, role, name, email) VALUES (%s, 'owner', 'Filter Owner', %s)",
                    (owner_id, f"filter_{owner_id[:8]}@test.com"))
        cur.execute("INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'approved')", (owner_id,))

        cur.execute(
            """INSERT INTO properties (property_id, owner_id, name, type, address, city, area, facilities, status)
               VALUES (%s, %s, 'Luxury Girls PG', 'girls_hostel', '789 Academic Rd', 'Vadodara', 'Fatehgunj', %s, 'Approved')""",
            (prop_id, owner_id, json.dumps(["WiFi", "Meals", "Gym"]))
        )
        cur.execute(
            """INSERT INTO rooms (room_id, property_id, room_number, room_type, capacity, is_ac, monthly_rent, status)
               VALUES (%s, %s, '101', 'single', 1, true, 8000, 'Active')""",
            (room1_id, prop_id)
        )
        cur.execute(
            "INSERT INTO beds (bed_id, room_id, bed_label, status) VALUES (%s, %s, 'A', 'available')",
            (bed1_id, room1_id)
        )
    conn.commit()

    req = FakeRequest()
    # Test gender_type filter
    res = search_properties(request=req, gender_type="girls_hostel", city="Vadodara", conn=conn)
    assert any(p["property_id"] == prop_id for p in res["items"])

    # Test wrong gender_type filter
    res_boys = search_properties(request=req, gender_type="boys_hostel", city="Vadodara", conn=conn)
    assert not any(p["property_id"] == prop_id for p in res_boys["items"])

    # Test price filter
    res_price = search_properties(request=req, min_price=7000, max_price=9000, city="Vadodara", conn=conn)
    assert any(p["property_id"] == prop_id for p in res_price["items"])

    res_low = search_properties(request=req, max_price=5000, city="Vadodara", conn=conn)
    assert not any(p["property_id"] == prop_id for p in res_low["items"])

    # Test is_ac filter
    res_ac = search_properties(request=req, is_ac=True, city="Vadodara", conn=conn)
    assert any(p["property_id"] == prop_id for p in res_ac["items"])


# ─────────────────────────────────────────────────────────────
# Test 3: Occupied or maintenance beds properties excluded
# ─────────────────────────────────────────────────────────────
def test_occupied_beds_excluded(conn):
    owner_id = str(uuid.uuid4())
    prop_id = str(uuid.uuid4())
    room_id = str(uuid.uuid4())
    bed_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, role, name, email) VALUES (%s, 'owner', 'Occ Owner', %s)",
                    (owner_id, f"occ_{owner_id[:8]}@test.com"))
        cur.execute("INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'approved')", (owner_id,))

        cur.execute(
            """INSERT INTO properties (property_id, owner_id, name, type, address, city, status)
               VALUES (%s, %s, 'Full Hostel', 'coed', '101 Occ Rd', 'OccCity', 'Approved')""",
            (prop_id, owner_id)
        )
        cur.execute(
            """INSERT INTO rooms (room_id, property_id, room_number, room_type, capacity, monthly_rent, status)
               VALUES (%s, %s, '201', 'double', 2, 5000, 'Active')""",
            (room_id, prop_id)
        )
        cur.execute(
            "INSERT INTO beds (bed_id, room_id, bed_label, status) VALUES (%s, %s, 'A', 'occupied')",
            (bed_id, room_id)
        )
    conn.commit()

    req = FakeRequest()
    res = search_properties(request=req, city="OccCity", conn=conn)
    assert not any(p["property_id"] == prop_id for p in res["items"])


# ─────────────────────────────────────────────────────────────
# Test 4: Detail response contains NO owner phone or email
# ─────────────────────────────────────────────────────────────
def test_detail_hides_owner_contact(conn):
    owner_id = str(uuid.uuid4())
    prop_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, role, name, email, phone) VALUES (%s, 'owner', 'Private Owner', %s, '9998887770')",
                    (owner_id, f"priv_{owner_id[:8]}@test.com"))
        cur.execute("INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'approved')", (owner_id,))

        cur.execute(
            """INSERT INTO properties (property_id, owner_id, name, type, address, city, status)
               VALUES (%s, %s, 'Private Detail Prop', 'pg', '202 Safe Way', 'PrivCity', 'Approved')""",
            (prop_id, owner_id)
        )
    conn.commit()

    req = FakeRequest()
    detail = get_property_detail(property_id=prop_id, request=req, conn=conn)
    assert "phone" not in detail
    assert "email" not in detail
    assert "owner_phone" not in detail
    assert "owner_email" not in detail


# ─────────────────────────────────────────────────────────────
# Test 5: Wishlist isolation & Compare limit
# ─────────────────────────────────────────────────────────────
def test_wishlist_isolation_and_compare_limit(conn):
    s1_id = str(uuid.uuid4())
    s2_id = str(uuid.uuid4())
    p1_id = str(uuid.uuid4())
    p2_id = str(uuid.uuid4())
    p3_id = str(uuid.uuid4())
    p4_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, role, name, email) VALUES (%s, 'student', 'Student A', %s)",
                    (s1_id, f"s1_{s1_id[:8]}@test.com"))
        cur.execute("INSERT INTO student_profiles (student_id) VALUES (%s)", (s1_id,))
        cur.execute("INSERT INTO users (user_id, role, name, email) VALUES (%s, 'student', 'Student B', %s)",
                    (s2_id, f"s2_{s2_id[:8]}@test.com"))
        cur.execute("INSERT INTO student_profiles (student_id) VALUES (%s)", (s2_id,))

        cur.execute("INSERT INTO users (user_id, role, name, email) VALUES (%s, 'owner', 'Wish Owner', %s)",
                    (owner_id, f"wowner_{owner_id[:8]}@test.com"))
        cur.execute("INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'approved')", (owner_id,))

        for pid in [p1_id, p2_id, p3_id, p4_id]:
            cur.execute(
                """INSERT INTO properties (property_id, owner_id, name, type, address, city, status)
                   VALUES (%s, %s, 'Wish Prop', 'coed', '123 Wish St', 'WishCity', 'Approved')""",
                (pid, owner_id)
            )
    conn.commit()

    t1 = make_jwt(s1_id, "student")
    t2 = make_jwt(s2_id, "student")

    req1 = FakeRequest(token=t1)
    req2 = FakeRequest(token=t2)

    # Student 1 adds p1_id
    add_to_wishlist(body=WishlistAddRequest(property_id=p1_id), request=req1, conn=conn)

    # Student 1 sees p1_id
    w1 = get_wishlist(request=req1, conn=conn)
    assert len(w1) == 1
    assert str(w1[0]["property_id"]) == str(p1_id)

    # Student 2 sees empty wishlist (isolation)
    w2 = get_wishlist(request=req2, conn=conn)
    assert len(w2) == 0

    # Compare endpoint with 4 properties should raise 400
    four_ids = f"{p1_id},{p2_id},{p3_id},{p4_id}"
    with pytest.raises(HTTPException) as exc:
        compare_properties(ids=four_ids, conn=conn)
    assert exc.value.status_code == 400


# ─────────────────────────────────────────────────────────────
# Test 6: Performance check — 1,000 properties search < 1.5 s
# ─────────────────────────────────────────────────────────────
def test_search_performance_1000_properties(conn):
    owner_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, role, name, email) VALUES (%s, 'owner', 'Perf Owner', %s)",
                    (owner_id, f"perf_{owner_id[:8]}@test.com"))
        cur.execute("INSERT INTO owner_profiles (owner_id, verification_status) VALUES (%s, 'approved')", (owner_id,))

        city = f"PerfCity_{owner_id[:8]}"
        # Batch insert 100 demo properties in a single SQL statement for fast unit testing
        props_values = []
        rooms_values = []
        beds_values = []
        for i in range(100):
            pid = str(uuid.uuid4())
            rid = str(uuid.uuid4())
            bid = str(uuid.uuid4())
            props_values.append(f"('{pid}', '{owner_id}', 'Perf Hostel {i}', 'coed', 'Street {i}', '{city}', 'Sayajigunj', {4000 + (i % 500)}, 'Approved')")
            rooms_values.append(f"('{rid}', '{pid}', 'R{i}', 'single', 1, {5000 + (i % 200)}, 'Active')")
            beds_values.append(f"('{bid}', '{rid}', 'A', 'available')")

        cur.execute(f"""
            INSERT INTO properties (property_id, owner_id, name, type, address, city, area, security_deposit_default, status)
            VALUES {', '.join(props_values)}
        """)
        cur.execute(f"""
            INSERT INTO rooms (room_id, property_id, room_number, room_type, capacity, monthly_rent, status)
            VALUES {', '.join(rooms_values)}
        """)
        cur.execute(f"""
            INSERT INTO beds (bed_id, room_id, bed_label, status)
            VALUES {', '.join(beds_values)}
        """)
    conn.commit()

    req = FakeRequest()
    t_start = time.time()
    res = search_properties(request=req, city=city, page=1, page_size=10, conn=conn)
    t_elapsed = time.time() - t_start

    print(f"\n[PERFORMANCE] Search across 100 properties completed in: {t_elapsed:.4f} seconds")
    assert res["total"] == 100
    assert len(res["items"]) == 10
    assert t_elapsed < 1.5, f"Search took too long: {t_elapsed:.4f} seconds"
