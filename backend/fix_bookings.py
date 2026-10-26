
class PayResponse(BaseModel):
    order_id: str
    amount: int
    currency: str
    key_id: str

@router.post("/{booking_id}/pay", response_model=PayResponse)
@limiter.limit("5/minute")
def pay_booking(req: Request, booking_id: str):
    conn = get_db_connection(req)
    uid = require_role(req, "student")
    
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # check if booking is payment_pending
            cur.execute("""
                SELECT b.status, b.rent_amount, b.deposit_amount, p.name as prop_name
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                WHERE b.booking_id = %s AND b.student_id = %s
            """, (booking_id, uid))
            booking = cur.fetchone()
            if not booking:
                raise HTTPException(status_code=404, detail="Booking not found")
            if booking['status'] != 'payment_pending':
                raise HTTPException(status_code=400, detail="Booking is not pending payment")
            
            # calculate amount
            total_amount = float(booking['rent_amount'] + (booking['deposit_amount'] or 0))
            
            # check if a pending payment already exists
            cur.execute("""
                SELECT gateway_order_id, amount FROM payments
                WHERE booking_id = %s AND type = 'booking_advance' AND status = 'Pending'
            """, (booking_id,))
            existing_payment = cur.fetchone()
            if existing_payment and existing_payment['gateway_order_id']:
                return {
                    "order_id": existing_payment['gateway_order_id'],
                    "amount": int(existing_payment['amount'] * 100),
                    "currency": "INR",
                    "key_id": settings.RAZORPAY_KEY_ID
                }
            
            # Create Razorpay order
            receipt_no = f"BK_{booking_id[:8]}"
            order = razorpay_service.create_order(
                amount=total_amount, 
                receipt=receipt_no,
                notes={"booking_id": booking_id, "type": "booking_advance"}
            )
            
            # Insert payments row
            cur.execute("""
                INSERT INTO payments(booking_id, payer_id, amount, type, mode, gateway_order_id, status)
                VALUES (%s, %s, %s, 'booking_advance', 'online', %s, 'Pending')
            """, (booking_id, uid, total_amount, order['id']))
            
            conn.commit()
            return {
                "order_id": order['id'],
                "amount": order['amount'],
                "currency": order['currency'],
                "key_id": settings.RAZORPAY_KEY_ID
            }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
