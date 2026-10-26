import json
import logging
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from psycopg.rows import dict_row

from ..db.session import get_db_connection, require_role, get_uid
from ..services.audit import log_action
from ..core.rate_limit import limiter
from ..config import settings
from ..services.razorpay_service import razorpay_service
from ..services.tasks import generate_receipt_pdf

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])

@router.post("/webhooks/payment")
async def payment_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature")
    
    if not signature or not razorpay_service.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
        
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
        
    event_id = request.headers.get("x-razorpay-event-id")
    if not event_id:
        event_id = payload.get("account_id", "") + "_" + payload.get("event", "") + "_" + str(payload.get("created_at", ""))
        
    event = payload.get("event")
    
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # Idempotency / Replay protection
            try:
                cur.execute("""
                    INSERT INTO webhook_events(event_id, provider, payload)
                    VALUES (%s, 'razorpay', %s)
                """, (event_id, json.dumps(payload)))
            except psycopg.errors.UniqueViolation:
                return {"status": "already processed"}
                
            try:
                if event in ["payment.captured", "order.paid"]:
                    payment_data = payload.get("payload", {}).get("payment", {}).get("entity", {})
                    if not payment_data and event == "order.paid":
                        payment_data = payload.get("payload", {}).get("order", {}).get("entity", {})
                    
                    gateway_order_id = payment_data.get("order_id")
                    gateway_payment_id = payment_data.get("id")
                    
                    if gateway_order_id:
                        cur.execute("SELECT * FROM payments WHERE gateway_order_id = %s", (gateway_order_id,))
                        payment = cur.fetchone()
                        
                        if payment:
                            amount_in_db = int(payment["amount"] * 100)
                            amount_in_gw = payment_data.get("amount")
                            currency = payment_data.get("currency")
                            
                            if amount_in_db != amount_in_gw or currency != "INR":
                                cur.execute("""
                                    UPDATE webhook_events SET error = 'Amount/Currency mismatch', processed_at = now()
                                    WHERE event_id = %s
                                """, (event_id,))
                                return {"status": "flagged"}
                                
                            cur.execute("SELECT apply_successful_payment(%s, %s, now())", (payment["payment_id"], gateway_payment_id))
                            generate_receipt_pdf.delay(str(payment["payment_id"]))
                        
                elif event == "payment.failed":
                    payment_data = payload.get("payload", {}).get("payment", {}).get("entity", {})
                    gateway_order_id = payment_data.get("order_id")
                    if gateway_order_id:
                        cur.execute("UPDATE payments SET status = 'Failed' WHERE gateway_order_id = %s AND status = 'Pending'", (gateway_order_id,))
                    
                elif event == "refund.processed":
                    refund_data = payload.get("payload", {}).get("refund", {}).get("entity", {})
                    payment_id_gw = refund_data.get("payment_id")
                    gateway_refund_id = refund_data.get("id")
                    if payment_id_gw:
                        cur.execute("UPDATE refunds SET status = 'processed', gateway_refund_id = %s WHERE payment_id = (SELECT payment_id FROM payments WHERE gateway_payment_id = %s LIMIT 1) AND status = 'pending'", (gateway_refund_id, payment_id_gw))
                    
                elif event == "refund.failed":
                    refund_data = payload.get("payload", {}).get("refund", {}).get("entity", {})
                    payment_id_gw = refund_data.get("payment_id")
                    if payment_id_gw:
                        cur.execute("UPDATE refunds SET status = 'failed' WHERE payment_id = (SELECT payment_id FROM payments WHERE gateway_payment_id = %s LIMIT 1) AND status = 'pending'", (payment_id_gw,))
                    
                cur.execute("UPDATE webhook_events SET processed_at = now() WHERE event_id = %s", (event_id,))
                
            except Exception as e:
                cur.execute("UPDATE webhook_events SET error = %s WHERE event_id = %s", (str(e), event_id))
                raise HTTPException(status_code=500, detail=str(e))
                
    return {"status": "ok"}

@router.get("/payments/{payment_id}")
def get_payment_status(request: Request, payment_id: str):
    """Poll a payment's status. Useful for the frontend confirm page."""
    conn = get_db_connection(request)
    claims = require_role(request, "student", "owner", "admin")
    uid = get_uid(claims)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT p.payment_id, p.status, p.type, p.amount, p.mode,
                   p.gateway_order_id, p.receipt_url, p.paid_at,
                   p.payer_id, p.recorded_by
            FROM payments p
            WHERE p.payment_id = %s
        """, (payment_id,))
        payment = cur.fetchone()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        # Students may only see their own
        if claims.get("role") == "student" and payment["payer_id"] != uid:
            raise HTTPException(403, "Unauthorized")
        return payment


@router.get("/payments/{payment_id}/receipt")
def get_payment_receipt(request: Request, payment_id: str):
    """Return the signed URL of a payment receipt PDF."""
    conn = get_db_connection(request)
    claims = require_role(request, "student", "owner", "admin")
    uid = get_uid(claims)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT receipt_url, payer_id FROM payments WHERE payment_id = %s", (payment_id,))
        payment = cur.fetchone()
        if not payment or not payment["receipt_url"]:
            raise HTTPException(status_code=404, detail="Receipt not found")
        if claims.get("role") == "student" and payment["payer_id"] != uid:
            raise HTTPException(403, "Unauthorized")
        return {"receipt_url": payment["receipt_url"]}


@router.get("/payments")
def list_payments(
    request: Request,
    tenancy_id:  str  = None,
    property_id: str  = None,
    status:      str  = None,
    mode:        str  = None,
    from_date:   str  = None,
    to_date:     str  = None,
    page:        int  = Query(1, ge=1),
    page_size:   int  = Query(20, ge=1, le=100),
):
    """
    Payment history filtered by role:
    - student → their own payments
    - owner   → payments for their properties
    - admin   → all
    """
    conn = get_db_connection(request)
    claims = require_role(request, "student", "owner", "admin")
    uid = get_uid(claims)
    role = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        conditions, params = [], []

        if role == "student":
            conditions.append("p.payer_id = %s")
            params.append(uid)
        elif role == "owner":
            conditions.append("""
                p.tenancy_id IN (
                    SELECT t.tenancy_id FROM tenancies t
                    JOIN properties pr ON pr.property_id = t.property_id
                    WHERE pr.owner_id = %s
                )
            """)
            params.append(uid)

        if tenancy_id:
            conditions.append("p.tenancy_id = %s"); params.append(tenancy_id)
        if property_id:
            conditions.append("""
                p.tenancy_id IN (
                    SELECT tenancy_id FROM tenancies WHERE property_id = %s
                )
            """); params.append(property_id)
        if status:
            conditions.append("p.status = %s"); params.append(status)
        if mode:
            conditions.append("p.mode = %s"); params.append(mode)
        if from_date:
            conditions.append("p.paid_at >= %s"); params.append(from_date)
        if to_date:
            conditions.append("p.paid_at <= %s"); params.append(to_date)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        cur.execute(f"SELECT COUNT(*) FROM payments p {where}", params)
        total = cur.fetchone()["count"]

        cur.execute(f"""
            SELECT payment_id, amount, type, mode, status, reference_no,
                   gateway_payment_id, paid_at, receipt_url, tenancy_id, payer_id
            FROM payments p {where}
            ORDER BY paid_at DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])
        rows = cur.fetchall()

    return {"total": total, "page": page, "page_size": page_size, "payments": rows}


# ─── OFFLINE PAYMENT (extended to support rent) ──────────────────────────────

class OfflinePaymentBody(dict):
    """
    Generic offline payment body.
    For type=booking_advance: {type, booking_id, amount, mode, reference_no}
    For type=rent:            {type, due_id, amount, mode, reference_no}
    """
    pass


from pydantic import BaseModel

class OfflinePaymentRequest(BaseModel):
    type:         str           # "booking_advance" | "rent"
    booking_id:   str = None    # for booking_advance
    due_id:       str = None    # for rent
    amount:       float
    mode:         str           # cash | bank_transfer | upi_offline
    reference_no: str


@router.post("/payments/offline")
@limiter.limit("10/minute")
def record_offline_payment(request: Request, data: OfflinePaymentRequest):
    """
    Record an offline payment.
    - type=booking_advance → calls record_offline_advance (Phase 4)
    - type=rent            → calls record_offline_rent_payment (Phase 7)
    Both paths require owner role.
    """
    conn = get_db_connection(request)
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            if data.type == "booking_advance":
                if not data.booking_id:
                    raise HTTPException(422, "booking_id required for booking_advance")
                cur.execute("SELECT record_offline_advance(%s, %s, %s, %s, %s)",
                            (data.booking_id, uid, data.amount, data.mode, data.reference_no))
                log_action(conn, uid, "owner", "OFFLINE_ADVANCE", "bookings", data.booking_id,
                           new_val={"amount": data.amount, "mode": data.mode})
                conn.commit()
                return {"status": "success", "message": "Advance recorded, booking confirmed"}

            elif data.type == "rent":
                if not data.due_id:
                    raise HTTPException(422, "due_id required for rent")
                cur.execute("SELECT record_offline_rent_payment(%s, %s, %s, %s, %s)",
                            (data.due_id, uid, data.amount, data.mode, data.reference_no))
                payment_id = cur.fetchone()["record_offline_rent_payment"]
                log_action(conn, uid, "owner", "OFFLINE_RENT", "rent_dues", data.due_id,
                           new_val={"amount": data.amount, "mode": data.mode})
                # Trigger receipt PDF generation
                generate_receipt_pdf.delay(str(payment_id))
                conn.commit()
                return {"status": "success", "payment_id": payment_id}

            else:
                raise HTTPException(422, f"Unknown payment type: {data.type}")

    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "ALREADY_PAID" in msg:   raise HTTPException(409, "Due is already fully paid")
        if "DUE_WAIVED" in msg:     raise HTTPException(409, "Due is waived")
        if "AMOUNT_EXCEEDS" in msg: raise HTTPException(422, "Amount exceeds outstanding balance")
        if "NOT_AUTHORIZED" in msg: raise HTTPException(403, "Not authorized")
        raise HTTPException(400, msg)
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

