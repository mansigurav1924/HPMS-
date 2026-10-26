"""
Admin router — owner/property verification, duplicate resolution.
All actions write audit_logs and send notifications.
"""
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import psycopg
import json

from ..db.session import get_db_connection, require_role, get_uid
from ..services.audit import log_action
from ..services.notifications import send_notification
from ..services.storage import get_signed_url

router = APIRouter(prefix="/admin", tags=["admin"])


# ─────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────
class VerifyOwnerRequest(BaseModel):
    action: str          # "approve" | "reject"
    reason: Optional[str] = None


class VerifyPropertyRequest(BaseModel):
    action: str          # "approve" | "reject" | "suspend"
    reason: Optional[str] = None


class ResolveDuplicateRequest(BaseModel):
    resolution: str      # "keep_separate" | "marked_duplicate"


# ─────────────────────────────────────────────────────────────
# Owner queue
# ─────────────────────────────────────────────────────────────
@router.get("/owners")
def list_owners(
    request: Request,
    status: Optional[str] = None,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    require_role(request, "admin")
    with conn.cursor() as cur:
        if status:
            cur.execute(
                """SELECT op.owner_id, u.name, u.email, u.phone,
                          op.business_name, op.verification_status, op.kyc_document_url
                   FROM owner_profiles op JOIN users u ON u.user_id = op.owner_id
                   WHERE op.verification_status = %s
                   ORDER BY u.created_at""",
                (status,)
            )
        else:
            cur.execute(
                """SELECT op.owner_id, u.name, u.email, u.phone,
                          op.business_name, op.verification_status, op.kyc_document_url
                   FROM owner_profiles op JOIN users u ON u.user_id = op.owner_id
                   ORDER BY u.created_at"""
            )
        rows = cur.fetchall()

    result = []
    for r in rows:
        kyc = r[6]
        signed = {}
        if kyc:
            try:
                paths = json.loads(kyc)
                for k, p in paths.items():
                    signed[k] = get_signed_url("kyc-docs", p)
            except Exception:
                pass
        result.append({
            "owner_id": r[0], "name": r[1], "email": r[2], "phone": r[3],
            "business_name": r[4], "verification_status": r[5],
            "kyc_signed_urls": signed,
        })
    return result


@router.post("/owners/{owner_id}/verify")
def verify_owner(
    owner_id: str,
    body: VerifyOwnerRequest,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    require_role(request, "admin")
    admin_uid = get_uid(require_role(request, "admin"))

    if body.action == "reject" and not body.reason:
        raise HTTPException(422, "Rejection reason is mandatory")
    if body.action not in ("approve", "reject"):
        raise HTTPException(422, "action must be approve or reject")

    new_status = "approved" if body.action == "approve" else "rejected"

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE owner_profiles
               SET verification_status = %s,
                   verified_by = %s, verified_at = now(),
                   rejection_reason = %s
               WHERE owner_id = %s
               RETURNING owner_id""",
            (new_status, admin_uid, body.reason, owner_id)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Owner not found")

        # Fetch name + email for notification
        cur.execute(
            "SELECT name, email FROM users WHERE user_id = %s", (owner_id,)
        )
        user_row = cur.fetchone()
    conn.commit()

    template_id = "owner_approved" if body.action == "approve" else "owner_rejected"
    if user_row:
        send_notification(conn, owner_id, template_id, user_row[1],
                          {"name": user_row[0], "reason": body.reason or ""})

    log_action(conn, admin_uid, "admin", f"OWNER_{body.action.upper()}",
               "owner_profiles", owner_id,
               None, {"status": new_status, "reason": body.reason})

    return {"message": f"Owner {body.action}d successfully"}


# ─────────────────────────────────────────────────────────────
# Property verification queue
# ─────────────────────────────────────────────────────────────
@router.get("/properties")
def list_properties_for_review(
    request: Request,
    status: Optional[str] = None,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    require_role(request, "admin")
    with conn.cursor() as cur:
        if status:
            cur.execute(
                """SELECT p.property_id, p.name, p.type, p.city, p.address, p.status,
                          u.name AS owner_name, u.email AS owner_email, p.created_at
                   FROM properties p JOIN users u ON u.user_id = p.owner_id
                   WHERE p.status = %s ORDER BY p.created_at""",
                (status,)
            )
        else:
            cur.execute(
                """SELECT p.property_id, p.name, p.type, p.city, p.address, p.status,
                          u.name AS owner_name, u.email AS owner_email, p.created_at
                   FROM properties p JOIN users u ON u.user_id = p.owner_id
                   ORDER BY p.created_at"""
            )
        rows = cur.fetchall()

    return [
        {
            "property_id": r[0], "name": r[1], "type": r[2], "city": r[3],
            "address": r[4], "status": r[5], "owner_name": r[6],
            "owner_email": r[7], "created_at": r[8],
        }
        for r in rows
    ]


@router.post("/properties/{property_id}/verify")
def verify_property(
    property_id: str,
    body: VerifyPropertyRequest,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    require_role(request, "admin")
    admin_uid = get_uid(require_role(request, "admin"))

    if body.action in ("reject", "suspend") and not body.reason:
        raise HTTPException(422, "Reason is mandatory for reject/suspend")
    if body.action not in ("approve", "reject", "suspend"):
        raise HTTPException(422, "action must be approve, reject, or suspend")

    status_map = {"approve": "Approved", "reject": "Rejected", "suspend": "Suspended"}
    new_status = status_map[body.action]

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE properties SET status = %s
               WHERE property_id = %s RETURNING property_id, owner_id, name""",
            (new_status, property_id)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Property not found")
        _, owner_id, prop_name = row

        cur.execute("SELECT name, email FROM users WHERE user_id = %s", (owner_id,))
        owner_row = cur.fetchone()
    conn.commit()

    template_map = {
        "approve": "property_approved",
        "reject":  "property_rejected",
        "suspend": "property_suspended",
    }
    if owner_row:
        send_notification(conn, owner_id, template_map[body.action], owner_row[1],
                          {"name": owner_row[0], "property_name": prop_name,
                           "reason": body.reason or ""})

    log_action(conn, admin_uid, "admin", f"PROPERTY_{body.action.upper()}",
               "properties", property_id,
               None, {"status": new_status, "reason": body.reason})

    return {"message": f"Property {body.action}d successfully"}


# ─────────────────────────────────────────────────────────────
# Duplicate flags
# ─────────────────────────────────────────────────────────────
@router.get("/duplicates")
def list_duplicates(
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    require_role(request, "admin")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM duplicate_flags WHERE status = 'pending' ORDER BY created_at"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.post("/duplicates/{flag_id}/resolve")
def resolve_duplicate(
    flag_id: str,
    body: ResolveDuplicateRequest,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    require_role(request, "admin")
    admin_uid = get_uid(require_role(request, "admin"))

    if body.resolution not in ("keep_separate", "marked_duplicate"):
        raise HTTPException(422, "resolution must be keep_separate or marked_duplicate")

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE duplicate_flags
               SET status = %s, resolved_by = %s, resolved_at = now()
               WHERE flag_id = %s RETURNING flag_id""",
            (body.resolution, admin_uid, flag_id)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Flag not found")
    conn.commit()

    log_action(conn, admin_uid, "admin", "DUPLICATE_RESOLVED",
               "duplicate_flags", flag_id,
               None, {"resolution": body.resolution})

    return {"message": f"Duplicate resolved as {body.resolution}"}

import csv
from io import StringIO
from fastapi import Query
from fastapi.responses import StreamingResponse

@router.get("/audit-logs")
def get_audit_logs(
    request: Request,
    user: Optional[str] = None,
    role: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    export: bool = False,
    conn = Depends(get_db_connection)
):
    require_role(request, "admin")
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        conds = []
        params = []
        if user:
            conds.append("user_id = %s")
            params.append(user)
        if role:
            conds.append("role = %s")
            params.append(role)
        if action:
            conds.append("action = %s")
            params.append(action)
        if entity_type:
            conds.append("entity_type = %s")
            params.append(entity_type)
        if date_from:
            conds.append("timestamp >= %s")
            params.append(date_from)
        if date_to:
            conds.append("timestamp <= %s")
            params.append(date_to)
            
        where = "WHERE " + " AND ".join(conds) if conds else ""
        
        if export:
            # CSV export capped at 5000 rows
            cur.execute(f"SELECT * FROM audit_logs {where} ORDER BY timestamp DESC LIMIT 5000", params)
            rows = cur.fetchall()
            
            output = StringIO()
            writer = csv.writer(output)
            if rows:
                writer.writerow(rows[0].keys())
                for row in rows:
                    writer.writerow(row.values())
            
            output.seek(0)
            return StreamingResponse(
                iter([output.getvalue()]), 
                media_type="text/csv", 
                headers={"Content-Disposition": "attachment; filename=audit_logs.csv"}
            )
            
        cur.execute(f"SELECT COUNT(*) FROM audit_logs {where}", params)
        total = cur.fetchone()["count"]
        
        cur.execute(f"SELECT * FROM audit_logs {where} ORDER BY timestamp DESC LIMIT %s OFFSET %s", params + [page_size, (page - 1) * page_size])
        return {"logs": cur.fetchall(), "total": total, "page": page, "page_size": page_size}

# ─────────────────────────────────────────────────────────────
# Users Management
# ─────────────────────────────────────────────────────────────
@router.get("/users")
def list_users(
    request: Request,
    search: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn = Depends(get_db_connection)
):
    require_role(request, "admin")
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        conds = []
        params = []
        
        if search:
            conds.append("(u.name ILIKE %s OR u.email ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        if role:
            conds.append("u.role = %s")
            params.append(role)
        if status:
            conds.append("u.status = %s")
            params.append(status)
            
        where = "WHERE " + " AND ".join(conds) if conds else ""
        
        cur.execute(f"SELECT COUNT(*) FROM users u {where}", params)
        total = cur.fetchone()["count"]
        
        cur.execute(f"SELECT user_id, name, email, phone, role, status, created_at FROM users u {where} ORDER BY created_at DESC LIMIT %s OFFSET %s", params + [page_size, (page - 1) * page_size])
        
        return {
            "users": cur.fetchall(),
            "total": total,
            "page": page,
            "page_size": page_size
        }

@router.post("/users/{user_id}/deactivate")
def deactivate_user(
    request: Request,
    user_id: str,
    conn = Depends(get_db_connection)
):
    admin_uid = get_uid(require_role(request, "admin"))
    if str(admin_uid) == str(user_id):
        raise HTTPException(400, "Cannot deactivate yourself")
        
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET status = 'inactive' WHERE user_id = %s RETURNING user_id", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found")
            
        log_action(conn, admin_uid, "admin", "DEACTIVATE_USER", "users", user_id, None, {"status": "inactive"})
        conn.commit()
        return {"status": "success"}

@router.post("/users/{user_id}/activate")
def activate_user(
    request: Request,
    user_id: str,
    conn = Depends(get_db_connection)
):
    admin_uid = get_uid(require_role(request, "admin"))
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET status = 'active' WHERE user_id = %s RETURNING user_id", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found")
            
        log_action(conn, admin_uid, "admin", "ACTIVATE_USER", "users", user_id, None, {"status": "active"})
        conn.commit()
        return {"status": "success"}
