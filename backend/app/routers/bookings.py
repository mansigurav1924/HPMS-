from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import date
import psycopg
import json

from ..db.session import get_db_connection, get_public_db_connection, require_role, get_uid, _decode_token
from ..services.audit import log_action
from ..services.notifications import send_notification
from ..core.rate_limit import limiter
from ..config import settings
from ..services.razorpay_service import razorpay_service
from psycopg.rows import dict_row

router = APIRouter(prefix="/bookings", tags=["bookings"])

class BookingRequest(BaseModel):
    bed_id: str
    move_in_date: date
    duration_months: int

class BookingResponse(BaseModel):
    approve: bool
    reason: Optional[str] = None

class BookingCancel(BaseModel):
    reason: str

class AdvancePayment(BaseModel):
    amount: float
    mode: str
    reference_no: str

from ..core.rate_limit import limiter

@router.post("/request")
@limiter.limit("5/minute")
def create_booking(request: Request, data: BookingRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "student")
    student_id = claims.get("sub", "")
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT create_booking_request(%s, %s, %s, %s)
            """, (student_id, data.bed_id, data.move_in_date, data.duration_months))
            booking_id = cur.fetchone()[0]
            
            cur.execute("""
                SELECT b.booking_id, p.owner_id, u.email, u.name, p.name
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                JOIN users u ON p.owner_id = u.user_id
                WHERE b.booking_id = %s
            """, (booking_id,))
            b_row = cur.fetchone()
            if b_row:
                owner_id, owner_email, owner_name, prop_name = b_row[1], b_row[2], b_row[3], b_row[4]
                send_notification(conn, owner_id, "booking_request_received", owner_email, {
                    "owner_name": owner_name, "property_name": prop_name
                }, "New Booking Request")
            
            log_action(conn, student_id, "student", "CREATE_BOOKING_REQUEST", "bookings", str(booking_id), 
                       json.dumps({"bed_id": data.bed_id, "move_in_date": str(data.move_in_date), "duration_months": data.duration_months}))
            conn.commit()
            return {"status": "success", "booking_id": booking_id}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{booking_id}/respond")
def respond_booking(request: Request, booking_id: str, data: BookingResponse, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT respond_to_booking(%s, %s, %s, %s)
            """, (booking_id, owner_id, data.approve, data.reason))
            
            cur.execute("""
                SELECT b.student_id, u.email, u.name, p.name
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                JOIN users u ON b.student_id = u.user_id
                WHERE b.booking_id = %s
            """, (booking_id,))
            b_row = cur.fetchone()
            if b_row:
                s_id, s_email, s_name, prop_name = b_row[0], b_row[1], b_row[2], b_row[3]
                template = "booking_approved" if data.approve else "booking_rejected"
                subject = "Booking Approved!" if data.approve else "Booking Rejected"
                send_notification(conn, s_id, template, s_email, {
                    "student_name": s_name, "property_name": prop_name, "reason": data.reason or ""
                }, subject)
                
            log_action(conn, owner_id, "owner", "RESPOND_BOOKING", "bookings", booking_id, 
                       json.dumps({"approve": data.approve, "reason": data.reason}))
            conn.commit()
            return {"status": "success", "message": "Booking responded"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{booking_id}/cancel")
def cancel_booking_endpoint(request: Request, booking_id: str, data: BookingCancel, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = _decode_token(request) # using helper since get_uid requires claims
    uid = claims.get("sub", "")
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cancel_booking(%s, %s, %s)
            """, (booking_id, uid, data.reason))
            
            log_action(conn, uid, "user", "CANCEL_BOOKING", "bookings", booking_id, 
                       json.dumps({"reason": data.reason}))
            conn.commit()
            return {"status": "success", "message": "Booking cancelled"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{booking_id}/advance")
def record_advance(request: Request, booking_id: str, data: AdvancePayment, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "owner")
    owner_id = claims.get("sub", "")
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT record_offline_advance(%s, %s, %s, %s, %s)
            """, (booking_id, owner_id, data.amount, data.mode, data.reference_no))
            
            log_action(conn, owner_id, "owner", "RECORD_ADVANCE", "bookings", booking_id, 
                       json.dumps({"amount": data.amount, "mode": data.mode, "reference_no": data.reference_no}))
            conn.commit()
            return {"status": "success", "message": "Advance payment recorded, booking confirmed"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/expire-holds")
def expire_holds(request: Request, conn: psycopg.Connection = Depends(get_db_connection)):
    claims = require_role(request, "admin")
    admin_id = claims.get("sub", "")
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT expire_overdue_holds()")
            count = cur.fetchone()[0]
            
            log_action(conn, admin_id, "admin", "EXPIRE_HOLDS", "bookings", None, json.dumps({"count": count}))
            conn.commit()
            return {"status": "success", "expired_count": count}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

class AutoBookRequest(BaseModel):
    property_id: str

@router.post("/auto-book-and-checkin")
def auto_book_and_checkin(request: Request, data: AutoBookRequest, conn: psycopg.Connection = Depends(get_public_db_connection)):
    claims = require_role(request, "student")
    student_id = claims.get("sub", "")
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.bed_id FROM beds b
                JOIN rooms r ON b.room_id = r.room_id
                WHERE r.property_id = %s AND b.status = 'available'
                LIMIT 1
            """, (data.property_id,))
            bed_row = cur.fetchone()
            if not bed_row:
                raise HTTPException(400, "No available beds in this property.")
            bed_id = bed_row[0]
            
            cur.execute("SELECT owner_id FROM properties WHERE property_id = %s", (data.property_id,))
            owner_row = cur.fetchone()
            if not owner_row:
                raise HTTPException(404, "Property not found.")
            owner_id = owner_row[0]
            
            # Impersonate student to satisfy RLS for student_profiles
            cur.execute("SET LOCAL role authenticated")
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(student_id), "role": "student"}),)
            )
            
            # Ensure student profile has ID proof
            cur.execute("""
                INSERT INTO student_profiles (student_id, id_proof_url)
                VALUES (%s, 'bypass.jpg')
                ON CONFLICT (student_id) DO UPDATE 
                SET id_proof_url = 'bypass.jpg'
                WHERE student_profiles.id_proof_url IS NULL OR student_profiles.id_proof_url = ''
            """, (student_id,))
            
            # Switch impersonation to owner in DB session to bypass RLS constraints on beds FOR UPDATE
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(owner_id), "role": "owner"}),)
            )
            
            # Create booking request
            cur.execute("SELECT create_booking_request(%s, %s, CURRENT_DATE, 6)", (student_id, bed_id))
            booking_id = cur.fetchone()[0]
            
            # Force booking to confirmed status, bypassing approval and payment steps
            cur.execute("UPDATE bookings SET status = 'confirmed' WHERE booking_id = %s", (booking_id,))
            
            # Check-in to create tenancy
            cur.execute("SELECT check_in(%s, %s, CURRENT_DATE, 5000, 'Online Bypass', 'BYPASS_123')", (booking_id, owner_id))
            tenancy_id = cur.fetchone()[0]
            
            conn.commit()
            return {"status": "success", "tenancy_id": tenancy_id}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# ── pay_booking endpoint commented out (payment feature paused) ──────────────
# class PayResponse(BaseModel):
#     order_id: str
#     amount: int
#     currency: str
#     key_id: str
#
# @router.post("/{booking_id}/pay", response_model=PayResponse)
# @limiter.limit("5/minute")
# def pay_booking(request: Request, booking_id: str):
#     conn = get_db_connection(request)
#     uid = require_role(request, "student")
#
#     try:
#         with conn.cursor(row_factory=dict_row) as cur:
#             cur.execute("""
#                 SELECT b.status, b.rent_amount, b.deposit_amount, p.name as prop_name
#                 FROM bookings b
#                 JOIN properties p ON b.property_id = p.property_id
#                 WHERE b.booking_id = %s AND b.student_id = %s
#             """, (booking_id, uid))
#             booking = cur.fetchone()
#             if not booking:
#                 raise HTTPException(status_code=404, detail="Booking not found")
#             if booking['status'] != 'payment_pending':
#                 raise HTTPException(status_code=400, detail="Booking is not pending payment")
#
#             total_amount = float(booking['rent_amount'] + (booking['deposit_amount'] or 0))
#
#             cur.execute("""
#                 SELECT gateway_order_id, amount FROM payments
#                 WHERE booking_id = %s AND type = 'booking_advance' AND status = 'Pending'
#             """, (booking_id,))
#             existing_payment = cur.fetchone()
#             if existing_payment and existing_payment['gateway_order_id']:
#                 return {
#                     "order_id": existing_payment['gateway_order_id'],
#                     "amount": int(existing_payment['amount'] * 100),
#                     "currency": "INR",
#                     "key_id": settings.RAZORPAY_KEY_ID
#                 }
#
#             # --- AUTO CONFIRM BYPASS ---
#             cur.execute("""
#                 INSERT INTO payments(booking_id, payer_id, amount, type, mode, gateway_order_id, status)
#                 VALUES (%s, %s, %s, 'booking_advance', 'online', %s, 'Pending')
#                 RETURNING payment_id
#             """, (booking_id, uid, total_amount, f"MOCK_ORDER_{booking_id[:8]}"))
#             new_payment = cur.fetchone()
#             cur.execute("SELECT apply_successful_payment(%s, %s, now())",
#                         (new_payment['payment_id'], f"MOCK_PAY_{booking_id[:8]}"))
#
#             conn.commit()
#             return {
#                 "order_id": f"MOCK_ORDER_{booking_id[:8]}",
#                 "amount": int(total_amount * 100),
#                 "currency": "INR",
#                 "key_id": "mock_key"
#             }
#     except HTTPException:
#         raise
#     except Exception as e:
#         conn.rollback()
#         raise HTTPException(status_code=500, detail=str(e))
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import Query

@router.get("")
def list_bookings(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_db_connection)
):
    claims = _decode_token(request)
    uid = get_uid(claims)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT role FROM users WHERE user_id = %s", (uid,))
        role_row = cur.fetchone()
        if not role_row:
            raise HTTPException(401, "User not found")
        role = role_row['role']
        
        base_query = """
            SELECT b.*, p.name as property_name, p.city, p.address,
                   COALESCE(u.name, 'Student Resident') as student_name,
                   COALESCE(u.email, 'student@example.com') as student_email,
                   COALESCE(u.phone, '—') as student_phone
            FROM bookings b
            JOIN properties p ON b.property_id = p.property_id
            LEFT JOIN users u ON b.student_id = u.user_id
        """
        conds = []
        params = []
        
        if role == 'student':
            conds.append("b.student_id = %s")
            params.append(uid)
        elif role == 'owner':
            conds.append("p.owner_id = %s")
            params.append(uid)
        elif role == 'admin':
            pass # can see all
        else:
            raise HTTPException(403, "Unauthorized")
            
        if status:
            conds.append("b.status = %s")
            params.append(status)
            
        where = "WHERE " + " AND ".join(conds) if conds else ""
        
        count_query = f"SELECT COUNT(*) FROM bookings b JOIN properties p ON b.property_id = p.property_id LEFT JOIN users u ON b.student_id = u.user_id {where}"
        cur.execute(count_query, params)
        total = cur.fetchone()['count']
        
        query = f"{base_query} {where} ORDER BY b.requested_at DESC LIMIT %s OFFSET %s"
        cur.execute(query, params + [page_size, (page - 1) * page_size])
        
        return {
            "bookings": cur.fetchall(),
            "total": total,
            "page": page,
            "page_size": page_size
        }
