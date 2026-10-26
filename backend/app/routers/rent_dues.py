"""
Phase 7: Rent Dues router
- GET /rent-dues          — list, role-scoped, paginated, filtered
- GET /rent-dues/summary  — owner monthly summary + overdue list
- GET /rent-dues/{id}     — detail with payments and charges
- POST /rent-dues/{id}/charges — add extra charge (owner)
- POST /rent-dues/{id}/waive   — waive due (owner)
- POST /rent-dues/{id}/pay     — already in Phase 5; re-exposed here for clarity
- GET /tenancies/{id}/ledger   — per-tenant ledger (owner/student)
"""
import json
from datetime import date
from typing import Optional

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row
from pydantic import BaseModel

from ..db.session import get_db_connection, require_role, get_uid
from ..services.audit import log_action
from ..core.rate_limit import limiter

router = APIRouter(tags=["rent-dues"])


# ─── Pydantic models ────────────────────────────────────────────────────────

class ExtraChargeRequest(BaseModel):
    description: str
    amount: float

class WaiveRequest(BaseModel):
    reason: str

class OfflineRentPayment(BaseModel):
    """Body for POST /payments/offline (type = rent)"""
    due_id: str
    amount: float
    mode: str        # cash | bank_transfer | upi_offline
    reference_no: str


# ─── LIST ───────────────────────────────────────────────────────────────────

@router.get("/rent-dues")
def list_rent_dues(
    request: Request,
    month:       Optional[str] = None,
    status:      Optional[str] = None,
    tenancy_id:  Optional[str] = None,
    property_id: Optional[str] = None,
    search:      Optional[str] = None,   # tenant name or room label
    page:        int = Query(1, ge=1),
    page_size:   int = Query(20, ge=1, le=100),
):
    """
    Returns a paginated list of rent_dues scoped by role:
    - student  → own dues only
    - owner    → dues for their properties
    - warden   → dues for assigned property
    - admin    → all
    """
    conn = get_db_connection(request)
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid = get_uid(claims)
    role = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        # Build base query with role filter
        base = """
            SELECT
                rd.due_id, rd.tenancy_id, rd.month, rd.base_amount,
                rd.extra_charges, rd.late_fee, rd.total_amount,
                rd.amount_paid, rd.due_date, rd.status,
                rd.waive_reason, rd.reminder_stage,
                u.name  AS tenant_name,
                p.name  AS property_name,
                r.room_number,
                b.bed_label
            FROM rent_dues rd
            JOIN tenancies t  ON t.tenancy_id  = rd.tenancy_id
            JOIN properties p ON p.property_id = t.property_id
            JOIN users u      ON u.user_id      = t.student_id
            JOIN beds b       ON b.bed_id        = t.bed_id
            JOIN rooms r      ON r.room_id       = b.room_id
        """
        conditions, params = [], []

        if role == "student":
            conditions.append("t.student_id = %s")
            params.append(uid)
        elif role == "owner":
            conditions.append("p.owner_id = %s")
            params.append(uid)
        elif role == "warden":
            # Wardens only see their assigned property
            conditions.append("""
                p.property_id IN (
                    SELECT property_id FROM property_staff WHERE user_id = %s
                )
            """)
            params.append(uid)
        # admin: no filter

        if month:
            conditions.append("rd.month = date_trunc('month', %s::date)")
            params.append(month)
        if status:
            conditions.append("rd.status = %s")
            params.append(status)
        if tenancy_id:
            conditions.append("rd.tenancy_id = %s")
            params.append(tenancy_id)
        if property_id:
            conditions.append("p.property_id = %s")
            params.append(property_id)
        if search:
            conditions.append("(u.name ILIKE %s OR r.room_number ILIKE %s OR b.bed_label ILIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        # Count
        cur.execute(f"SELECT COUNT(*) FROM rent_dues rd JOIN tenancies t ON t.tenancy_id = rd.tenancy_id JOIN properties p ON p.property_id = t.property_id JOIN users u ON u.user_id = t.student_id JOIN beds b ON b.bed_id = t.bed_id JOIN rooms r ON r.room_id = b.room_id {where}", params)
        total = cur.fetchone()["count"]

        # Fetch page
        cur.execute(f"{base} {where} ORDER BY rd.due_date ASC LIMIT %s OFFSET %s",
                    params + [page_size, offset])
        rows = cur.fetchall()

    return {"total": total, "page": page, "page_size": page_size, "dues": rows}


# ─── SUMMARY (owner) ─────────────────────────────────────────────────────────

@router.get("/rent-dues/summary")
def rent_dues_summary(
    request: Request,
    property_id: Optional[str] = None,
    month:       Optional[str] = None,
):
    """
    Owner: monthly collected vs pending vs overdue totals,
    plus a list of overdue tenants.
    """
    conn = get_db_connection(request)
    claims = require_role(request, "owner", "admin")
    uid = get_uid(claims)
    role = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        prop_cond = ""
        params = []
        if role == "owner":
            prop_cond = "AND p.owner_id = %s"
            params.append(uid)
        if property_id:
            prop_cond += " AND p.property_id = %s"
            params.append(property_id)
        month_cond = ""
        if month:
            month_cond = "AND rd.month = date_trunc('month', %s::date)"
            params.append(month)

        cur.execute(f"""
            SELECT
                rd.status,
                COUNT(*)                      AS due_count,
                SUM(rd.total_amount)          AS total_amount,
                SUM(rd.amount_paid)           AS amount_paid
            FROM rent_dues rd
            JOIN tenancies t  ON t.tenancy_id  = rd.tenancy_id
            JOIN properties p ON p.property_id = t.property_id
            WHERE 1=1 {prop_cond} {month_cond}
            GROUP BY rd.status
        """, params)
        summary = cur.fetchall()

        # Overdue list
        cur.execute(f"""
            SELECT
                rd.due_id, rd.month, rd.due_date,
                rd.total_amount - rd.amount_paid AS outstanding,
                u.name AS tenant_name, u.email,
                p.name AS property_name,
                r.room_number, b.bed_label,
                rd.reminder_stage
            FROM rent_dues rd
            JOIN tenancies t  ON t.tenancy_id  = rd.tenancy_id
            JOIN properties p ON p.property_id = t.property_id
            JOIN users u      ON u.user_id      = t.student_id
            JOIN beds b       ON b.bed_id       = t.bed_id
            JOIN rooms r      ON r.room_id      = b.room_id
            WHERE rd.status = 'Overdue' {prop_cond} {month_cond}
            ORDER BY rd.due_date
        """, params)
        overdue = cur.fetchall()

    return {"summary": summary, "overdue_tenants": overdue}


# ─── DETAIL ───────────────────────────────────────────────────────────────────

@router.get("/rent-dues/{due_id}")
def get_rent_due(request: Request, due_id: str):
    """
    Detail for a single due: includes payments list and charge breakdown.
    """
    conn = get_db_connection(request)
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid = get_uid(claims)
    role = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT rd.*, t.student_id, p.owner_id
            FROM rent_dues rd
            JOIN tenancies t  ON t.tenancy_id  = rd.tenancy_id
            JOIN properties p ON p.property_id = t.property_id
            WHERE rd.due_id = %s
        """, (due_id,))
        due = cur.fetchone()
        if not due:
            raise HTTPException(404, "Due not found")

        # Authorization
        if role == "student" and due["student_id"] != uid:
            raise HTTPException(403, "Unauthorized")
        if role == "owner" and due["owner_id"] != uid:
            raise HTTPException(403, "Unauthorized")

        # Payments on this due
        cur.execute("""
            SELECT payment_id, amount, mode, status, reference_no, paid_at, recorded_by
            FROM payments
            WHERE due_id = %s
            ORDER BY paid_at
        """, (due_id,))
        payments = cur.fetchall()

    return {**due, "payments": payments}


# ─── EXTRA CHARGE ─────────────────────────────────────────────────────────────

@router.post("/rent-dues/{due_id}/charges")
@limiter.limit("10/minute")
def add_extra_charge(request: Request, due_id: str, data: ExtraChargeRequest):
    """
    Owner-only: append an extra charge to a non-Paid/non-Waived due.
    Recomputes total_amount via recompute_due_totals.
    """
    conn = get_db_connection(request)
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT add_extra_charge(%s, %s, %s, %s)",
                        (due_id, uid, data.description, data.amount))
        log_action(conn, uid, "owner", "ADD_EXTRA_CHARGE", "rent_dues", due_id,
                   new_val={"description": data.description, "amount": data.amount})
        conn.commit()
        return {"status": "success"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "ALREADY_PAID" in msg:
            raise HTTPException(409, "Due is already paid")
        if "DUE_WAIVED" in msg:
            raise HTTPException(409, "Due is already waived")
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── WAIVE ────────────────────────────────────────────────────────────────────

@router.post("/rent-dues/{due_id}/waive")
@limiter.limit("5/minute")
def waive_due(request: Request, due_id: str, data: WaiveRequest):
    """
    Owner-only: waive a due (sets status = 'Waived').
    Reason is mandatory; audit-logged.
    """
    conn = get_db_connection(request)
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT waive_due(%s, %s, %s)", (due_id, uid, data.reason))
        conn.commit()
        return {"status": "waived"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "INVALID_STATUS" in msg:
            raise HTTPException(409, "Due is already Paid or Waived")
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── TENANT LEDGER ────────────────────────────────────────────────────────────

@router.get("/tenancies/{tenancy_id}/ledger")
def tenant_ledger(request: Request, tenancy_id: str):
    """
    Full chronological ledger for one tenancy:
    - rent_dues list (all months)
    - payments list
    Accessible by the student themselves or the property owner.
    """
    conn = get_db_connection(request)
    claims = require_role(request, "student", "owner", "admin")
    uid = get_uid(claims)
    role = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        # Check auth
        cur.execute("""
            SELECT t.student_id, p.owner_id
            FROM tenancies t JOIN properties p ON p.property_id = t.property_id
            WHERE t.tenancy_id = %s
        """, (tenancy_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Tenancy not found")
        if role == "student" and row["student_id"] != uid:
            raise HTTPException(403, "Unauthorized")
        if role == "owner" and row["owner_id"] != uid:
            raise HTTPException(403, "Unauthorized")

        # Dues
        cur.execute("""
            SELECT due_id, month, base_amount, extra_charges, late_fee,
                   total_amount, amount_paid, due_date, status, waive_reason
            FROM rent_dues
            WHERE tenancy_id = %s
            ORDER BY month
        """, (tenancy_id,))
        dues = cur.fetchall()

        # Payments
        cur.execute("""
            SELECT payment_id, amount, type, mode, status, reference_no,
                   gateway_payment_id, paid_at, receipt_url
            FROM payments
            WHERE tenancy_id = %s
            ORDER BY paid_at
        """, (tenancy_id,))
        payments = cur.fetchall()

    return {"dues": dues, "payments": payments}
