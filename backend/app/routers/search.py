from fastapi import APIRouter, Depends, Query, HTTPException, Request
import psycopg
from app.db.session import get_db_connection, require_role, get_uid

router = APIRouter(prefix="/search", tags=["search"])

from app.core.rate_limit import limiter

@router.get("/")
@limiter.limit("20/minute")
def global_search(
    request: Request,
    q: str = Query(..., min_length=2),
    type: str = Query(None, description="Filter by type (users, properties, bookings, complaints)"),
    db: psycopg.Connection = Depends(get_db_connection)
):
    claims = require_role(request, "admin", "owner", "student")
    role = claims.get("role") or claims.get("user_metadata", {}).get("role") or claims.get("app_metadata", {}).get("role")
    uid = get_uid(claims)
    results = []

    with db.cursor() as cur:
        # Search Users/Tenants
        if type in (None, "users"):
            if role == "admin":
                cur.execute("""
                    SELECT user_id as id, name, email, role, 'user' as result_type
                    FROM users
                    WHERE name ILIKE %s OR email ILIKE %s
                    LIMIT 20
                """, (f"%{q}%", f"%{q}%"))
                columns = [desc[0] for desc in cur.description]
                results.extend([dict(zip(columns, row)) for row in cur.fetchall()])
            elif role == "owner":
                cur.execute("""
                    SELECT DISTINCT u.user_id as id, u.name, u.email, u.role, 'tenant' as result_type
                    FROM users u
                    JOIN tenancies t ON u.user_id = t.student_id
                    JOIN properties p ON t.property_id = p.property_id
                    WHERE p.owner_id = %s AND (u.name ILIKE %s OR u.email ILIKE %s)
                    LIMIT 20
                """, (uid, f"%{q}%", f"%{q}%"))
                columns = [desc[0] for desc in cur.description]
                results.extend([dict(zip(columns, row)) for row in cur.fetchall()])

        # Search Properties
        if type in (None, "properties") and role == "admin":
            cur.execute("""
                SELECT property_id as id, name, city, 'property' as result_type
                FROM properties
                WHERE name ILIKE %s OR city ILIKE %s
                LIMIT 20
            """, (f"%{q}%", f"%{q}%"))
            columns = [desc[0] for desc in cur.description]
            results.extend([dict(zip(columns, row)) for row in cur.fetchall()])

        # Search Bookings
        if type in (None, "bookings"):
            if role == "admin":
                cur.execute("""
                    SELECT b.booking_id as id, b.status, p.name as property_name, u.name as student_name, 'booking' as result_type
                    FROM bookings b
                    JOIN properties p ON b.property_id = p.property_id
                    JOIN users u ON b.student_id = u.user_id
                    WHERE u.name ILIKE %s OR p.name ILIKE %s OR b.status ILIKE %s
                    LIMIT 20
                """, (f"%{q}%", f"%{q}%", f"%{q}%"))
                columns = [desc[0] for desc in cur.description]
                results.extend([dict(zip(columns, row)) for row in cur.fetchall()])
            elif role == "owner":
                cur.execute("""
                    SELECT b.booking_id as id, b.status, p.name as property_name, u.name as student_name, 'booking' as result_type
                    FROM bookings b
                    JOIN properties p ON b.property_id = p.property_id
                    JOIN users u ON b.student_id = u.user_id
                    WHERE p.owner_id = %s AND (u.name ILIKE %s OR b.status ILIKE %s)
                    LIMIT 20
                """, (uid, f"%{q}%", f"%{q}%"))
                columns = [desc[0] for desc in cur.description]
                results.extend([dict(zip(columns, row)) for row in cur.fetchall()])

        # Search Complaints
        if type in (None, "complaints"):
            if role == "admin":
                cur.execute("""
                    SELECT c.complaint_id as id, c.category, c.status, c.description, 'complaint' as result_type
                    FROM complaints c
                    WHERE c.description ILIKE %s OR c.status ILIKE %s
                    LIMIT 20
                """, (f"%{q}%", f"%{q}%"))
                columns = [desc[0] for desc in cur.description]
                results.extend([dict(zip(columns, row)) for row in cur.fetchall()])
            elif role == "owner":
                cur.execute("""
                    SELECT c.complaint_id as id, c.category, c.status, c.description, 'complaint' as result_type
                    FROM complaints c
                    JOIN properties p ON c.property_id = p.property_id
                    WHERE p.owner_id = %s AND (c.description ILIKE %s OR c.status ILIKE %s)
                    LIMIT 20
                """, (uid, f"%{q}%", f"%{q}%"))
                columns = [desc[0] for desc in cur.description]
                results.extend([dict(zip(columns, row)) for row in cur.fetchall()])

    return {"results": results}
