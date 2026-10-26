from fastapi import APIRouter, Depends, Query, HTTPException, Response, Request
import psycopg
from app.db.session import get_db_connection
from app.db.session import require_role, get_uid
from app.config import settings
import csv
import io
import json
from datetime import datetime

router = APIRouter(prefix="", tags=["export"])

def escape_csv_cell(value):
    if value and isinstance(value, str):
        if value.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value

def generate_csv_stream(columns, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        escaped_row = [escape_csv_cell(val) for val in row]
        writer.writerow(escaped_row)
    return output.getvalue()

from ..core.rate_limit import limiter

@router.get("/export/{resource}")
@limiter.limit("5/minute")
def export_resource(
    request: Request,
    resource: str,
    format: str = Query("csv", regex="^(csv|xlsx)$"),
    db: psycopg.Connection = Depends(get_db_connection)
):
    if resource in ("payments", "rent_ledger", "deposit_ledger", "collection_report", "overdue_report"):
        if not settings.PAYMENTS_ENABLED:
            raise HTTPException(status_code=404, detail="payments paused")
    
    claims = require_role(request, "admin", "owner")
    role = claims.get("role") or claims.get("user_metadata", {}).get("role") or claims.get("app_metadata", {}).get("role")
    uid = get_uid(claims)
    
    query = ""
    params = []
    
    if resource == "bookings":
        if role == "admin":
            query = "SELECT booking_id, status, move_in_date, rent_amount FROM bookings LIMIT 5001"
        else:
            query = """
                SELECT b.booking_id, b.status, b.move_in_date, b.rent_amount 
                FROM bookings b JOIN properties p ON b.property_id = p.property_id
                WHERE p.owner_id = %s LIMIT 5001
            """
            params.append(uid)
    elif resource == "tenants":
        if role == "admin":
            query = "SELECT user_id, name, email, phone FROM users WHERE role = 'student' LIMIT 5001"
        else:
            query = """
                SELECT DISTINCT u.user_id, u.name, u.email, u.phone 
                FROM users u
                JOIN tenancies t ON u.user_id = t.student_id
                JOIN properties p ON t.property_id = p.property_id
                WHERE p.owner_id = %s LIMIT 5001
            """
            params.append(uid)
    elif resource == "complaints":
        if role == "admin":
            query = "SELECT complaint_id, category, status, priority FROM complaints LIMIT 5001"
        else:
            query = """
                SELECT c.complaint_id, c.category, c.status, c.priority
                FROM complaints c JOIN properties p ON c.property_id = p.property_id
                WHERE p.owner_id = %s LIMIT 5001
            """
            params.append(uid)
    else:
        raise HTTPException(status_code=400, detail="Invalid resource")

    with db.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        
        if len(rows) > 5000:
            raise HTTPException(status_code=422, detail="Export exceeds 5,000 rows. Please narrow your filters.")
        
        # log export (mock)
        cur.execute("""
            INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, old_data, new_data)
            VALUES (%s, 'export', %s, NULL, NULL, %s)
        """, (uid, resource, json.dumps({"format": format, "row_count": len(rows)})))
        db.commit()

        columns = [desc[0] for desc in cur.description] if rows else []
        
        if format == "csv":
            csv_content = generate_csv_stream(columns, rows)
            return Response(content=csv_content, media_type="text/csv", headers={
                "Content-Disposition": f"attachment; filename={resource}_{datetime.now().isoformat()}.csv"
            })
        else:
            # mock xlsx for now
            raise HTTPException(status_code=501, detail="XLSX not implemented yet")

@router.get("/reports/{name}")
def get_report(name: str, request: Request, db: psycopg.Connection = Depends(get_db_connection)):
    # minimal mock for reports
    return {"report": name, "data": []}
