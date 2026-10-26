from fastapi import APIRouter, Request, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import Optional, List, Any, Dict
from datetime import date
import psycopg
import json

from ..db.session import get_db_connection, require_role, get_uid, _decode_token
from ..services.audit import log_action
from ..services.storage import get_signed_url
from ..services.pdf import generate_and_upload_agreement

router = APIRouter(tags=["tenancies"])

class CheckInRequest(BaseModel):
    check_in_date: date
    deposit_collected: float
    mode: str = "Cash"
    reference_no: str = ""

class NoticeRequest(BaseModel):
    notice_date: date

class RoomTransferRequest(BaseModel):
    new_bed_id: str
    effective_date: date

class Deduction(BaseModel):
    amount: float
    reason: str

class CheckOutRequest(BaseModel):
    check_out_date: date
    deductions: List[Deduction] = []

class EvictRequest(BaseModel):
    reason: str
    deductions: List[Deduction] = []

class NoShowDecision(BaseModel):
    action: str # forfeit or refund

@router.post("/tenancies/{booking_id}/check-in")
def check_in(request: Request, booking_id: str, data: CheckInRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT check_in(%s, %s, %s, %s, %s, %s)
            """, (booking_id, owner_id, data.check_in_date, data.deposit_collected, data.mode, data.reference_no))
            tenancy_id = cur.fetchone()[0]
            
            # trigger celery job
            generate_and_upload_agreement.delay(str(tenancy_id))
            
            log_action(conn, owner_id, "owner", "CHECK_IN", "tenancies", str(tenancy_id), json.dumps({"booking_id": booking_id}))
            conn.commit()
            return {"status": "success", "tenancy_id": tenancy_id}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "ID_PROOF_MISSING" in msg: raise HTTPException(status_code=403, detail="ID proof missing")
        raise HTTPException(status_code=400, detail=msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/tenancies")
def get_tenancies(request: Request, status: Optional[str] = None, property_id: Optional[str] = None, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = _decode_token(request)
    uid = get_uid(claims)
    # determine role
    # assuming we can just query by student_id or owner_id based on policy, but doing it explicitly
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE user_id = %s", (uid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(401, "User not found")
        role = row[0]
        
        query = "SELECT t.tenancy_id, t.status, t.rent_amount, t.property_id, t.bed_id, t.check_in_date FROM tenancies t"
        params = []
        if role == 'student':
            query += " WHERE t.student_id = %s"
            params.append(uid)
        elif role == 'owner':
            query += " JOIN properties p ON t.property_id = p.property_id WHERE p.owner_id = %s"
            params.append(uid)
        elif role == 'admin':
            pass
        else:
            raise HTTPException(403, "Unauthorized")
            
        if status:
            query += " AND t.status = %s"
            params.append(status)
        if property_id:
            query += " AND t.property_id = %s"
            params.append(property_id)
            
        cur.execute(query, params)
        rows = cur.fetchall()
        return {
            "tenancies": [
                {
                    "id": str(r[0]),
                    "status": r[1],
                    "rent": float(r[2]) if r[2] is not None else 0.0,
                    "property_id": str(r[3]),
                    "bed_id": str(r[4]) if r[4] else None,
                    "check_in_date": r[5].isoformat() if hasattr(r[5], 'isoformat') else r[5]
                }
                for r in rows
            ]
        }

@router.get("/tenancies/{tenancy_id}")
def get_tenancy_detail(request: Request, tenancy_id: str, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = _decode_token(request)
    uid = get_uid(claims)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.tenancy_id, t.status, t.student_id, p.owner_id, sp.id_proof_url
            FROM tenancies t
            JOIN properties p ON t.property_id = p.property_id
            JOIN student_profiles sp ON t.student_id = sp.student_id
            WHERE t.tenancy_id = %s
        """, (tenancy_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Not found")
        t_id, status, student_id, owner_id, id_proof_url = row
        
        if uid != student_id and uid != owner_id:
            raise HTTPException(403, "Unauthorized")
            
        signed_url = None
        if uid == owner_id and id_proof_url:
            # write audit log
            log_action(conn, owner_id, "owner", "VIEW_ID_PROOF", "student_profiles", student_id, "{}")
            signed_url = get_signed_url("kyc", id_proof_url)
            conn.commit()
            
        return {
            "tenancy_id": t_id,
            "status": status,
            "student_id": student_id,
            "id_proof_signed_url": signed_url
        }

@router.post("/tenancies/{tenancy_id}/notice")
def give_notice(request: Request, tenancy_id: str, data: NoticeRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = _decode_token(request)
    uid = get_uid(claims)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT give_notice(%s, %s, %s)", (tenancy_id, uid, data.notice_date))
            log_action(conn, uid, "user", "GIVE_NOTICE", "tenancies", tenancy_id, json.dumps({"notice_date": str(data.notice_date)}))
            conn.commit()
            return {"status": "success"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, detail=str(e))

@router.post("/tenancies/{tenancy_id}/room-transfer")
def room_transfer(request: Request, tenancy_id: str, data: RoomTransferRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT room_transfer(%s, %s, %s, %s)", (tenancy_id, owner_id, data.new_bed_id, data.effective_date))
            conn.commit()
            return {"status": "success"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        raise HTTPException(409, detail=str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, detail=str(e))

@router.post("/tenancies/{tenancy_id}/check-out")
def check_out(request: Request, tenancy_id: str, data: CheckOutRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    try:
        with conn.cursor() as cur:
            deductions_json = json.dumps([d.dict() for d in data.deductions])
            cur.execute("SELECT check_out(%s, %s, %s, %s)", (tenancy_id, owner_id, data.check_out_date, deductions_json))
            conn.commit()
            return {"status": "success"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "DEDUCTIONS_EXCEED_DEPOSIT" in msg:
            raise HTTPException(422, detail=msg)
        raise HTTPException(400, detail=msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, detail=str(e))

@router.post("/tenancies/{tenancy_id}/evict")
def evict_tenant(request: Request, tenancy_id: str, data: EvictRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    try:
        with conn.cursor() as cur:
            deductions_json = json.dumps([d.dict() for d in data.deductions])
            cur.execute("SELECT evict(%s, %s, %s, %s)", (tenancy_id, owner_id, data.reason, deductions_json))
            conn.commit()
            return {"status": "success"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "DEDUCTIONS_EXCEED_DEPOSIT" in msg:
            raise HTTPException(422, detail=msg)
        raise HTTPException(400, detail=msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, detail=str(e))

@router.get("/tenancies/{tenancy_id}/deposit-ledger")
def get_ledger(request: Request, tenancy_id: str, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = _decode_token(request)
    uid = get_uid(claims)
    with conn.cursor() as cur:
        # Check permission
        cur.execute("""
            SELECT t.student_id, p.owner_id
            FROM tenancies t JOIN properties p ON t.property_id = p.property_id
            WHERE t.tenancy_id = %s
        """, (tenancy_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Not found")
        if uid != row[0] and uid != row[1]: raise HTTPException(403, "Unauthorized")
        
        cur.execute("SELECT ledger_id, type, amount, reason, created_at FROM deposit_ledger WHERE tenancy_id = %s ORDER BY created_at ASC", (tenancy_id,))
        rows = cur.fetchall()
        return {"ledger": [{"id": r[0], "type": r[1], "amount": r[2], "reason": r[3], "created_at": r[4]} for r in rows]}

class MarkProcessedRequest(BaseModel):
    reference_no: str

@router.post("/refunds/{refund_id}/mark-processed")
def mark_refund_processed(request: Request, refund_id: str, data: MarkProcessedRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mark_refund_processed(%s, %s, %s)", (refund_id, owner_id, data.reference_no))
            conn.commit()
            return {"status": "success"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, detail=str(e))

@router.post("/bookings/{booking_id}/no-show-decision")
def no_show_decision(request: Request, booking_id: str, data: NoShowDecision, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    try:
        with conn.cursor() as cur:
            # mock for now
            cur.execute("UPDATE bookings SET cancel_reason = %s, status = 'closed' WHERE booking_id = %s", (data.action, booking_id))
            conn.commit()
            return {"status": "success"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, detail=str(e))

@router.get("/tenancies/{tenancy_id}/agreement")
def get_agreement(request: Request, tenancy_id: str, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = _decode_token(request)
    uid = get_uid(claims)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.agreement_url, t.student_id, p.owner_id
            FROM tenancies t JOIN properties p ON t.property_id = p.property_id
            WHERE t.tenancy_id = %s
        """, (tenancy_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Not found")
        url, s_id, o_id = row
        if uid != s_id and uid != o_id: raise HTTPException(403, "Unauthorized")
        if not url or url == 'FAILED': raise HTTPException(404, "Agreement not generated")
        
        signed_url = get_signed_url("agreements", url)
        return {"signed_url": signed_url}
