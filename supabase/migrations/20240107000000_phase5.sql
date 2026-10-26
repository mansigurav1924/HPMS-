-- Phase 5: Online payments with Razorpay

-- 1. Complete payments table
ALTER TABLE payments ADD CONSTRAINT uq_gateway_payment UNIQUE (gateway_payment_id);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS created_at timestamptz not null default now();
ALTER TABLE payments ADD COLUMN IF NOT EXISTS expires_at timestamptz;

-- 2. Index for reconciliation job
CREATE INDEX IF NOT EXISTS idx_payments_status_created ON payments(status, created_at);

-- 3. apply_successful_payment
CREATE OR REPLACE FUNCTION apply_successful_payment(
    p_payment uuid, 
    p_gateway_payment_id text, 
    p_paid_at timestamptz
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_payment payments%rowtype;
    v_booking bookings%rowtype;
    v_due rent_dues%rowtype;
BEGIN
    SELECT * INTO v_payment FROM payments WHERE payment_id = p_payment FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'PAYMENT_NOT_FOUND'; END IF;
    
    -- Idempotent check
    IF v_payment.status = 'Success' THEN RETURN; END IF;
    
    -- Update payment
    UPDATE payments SET 
        status = 'Success', 
        gateway_payment_id = COALESCE(p_gateway_payment_id, gateway_payment_id),
        paid_at = COALESCE(p_paid_at, now())
    WHERE payment_id = p_payment;
    
    IF v_payment.type = 'booking_advance' THEN
        SELECT * INTO v_booking FROM bookings WHERE booking_id = v_payment.booking_id FOR UPDATE;
        IF v_booking.status = 'payment_pending' THEN
            UPDATE bookings SET 
                status = 'confirmed',
                advance_paid = COALESCE(advance_paid, 0) + v_payment.amount
            WHERE booking_id = v_payment.booking_id;
        END IF;
    ELSIF v_payment.type = 'rent' THEN
        SELECT * INTO v_due FROM rent_dues WHERE due_id = v_payment.due_id FOR UPDATE;
        UPDATE rent_dues SET 
            amount_paid = COALESCE(amount_paid, 0) + v_payment.amount,
            status = CASE WHEN (COALESCE(amount_paid, 0) + v_payment.amount) >= total_amount THEN 'Paid' ELSE 'Partial' END
        WHERE due_id = v_payment.due_id;
    ELSIF v_payment.type = 'deposit' THEN
        INSERT INTO deposit_ledger(entry_id, tenancy_id, entry_type, amount, reason, created_at)
        VALUES (gen_random_uuid(), v_payment.tenancy_id, 'collected', v_payment.amount, 'Online Deposit', COALESCE(p_paid_at, now()));
    END IF;
END $$;
