-- Phase 4: Booking Module Migrations

-- 1. Create missing tables
CREATE TABLE IF NOT EXISTS refunds (
    refund_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    original_payment_id uuid, -- no strict FK for now just in case
    amount numeric(12,2) NOT NULL,
    status text NOT NULL DEFAULT 'Pending',
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 2. Add missing columns to bookings
ALTER TABLE bookings 
ADD COLUMN IF NOT EXISTS payment_due_at timestamptz,
ADD COLUMN IF NOT EXISTS manual_review boolean DEFAULT false;

-- 3. Fix create_booking_request (cast enum and set payment_due_at for instant)
CREATE OR REPLACE FUNCTION create_booking_request(
    p_student uuid, p_bed uuid, p_move_in date, p_months int)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    v_bed beds%rowtype; v_room rooms%rowtype; v_prop properties%rowtype;
    v_id uuid; v_due timestamptz := now() + interval '24 hours';
BEGIN
    SELECT * INTO v_bed FROM beds WHERE bed_id = p_bed FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'BED_NOT_FOUND'; END IF;
    SELECT * INTO v_room FROM rooms WHERE room_id = v_bed.room_id;
    SELECT * INTO v_prop FROM properties WHERE property_id = v_room.property_id;
    
    IF v_prop.status <> 'Approved' OR v_room.status <> 'Active' THEN
        RAISE EXCEPTION 'NOT_BOOKABLE'; 
    END IF;
    
    IF v_bed.status <> 'available' THEN 
        RAISE EXCEPTION 'BED_UNAVAILABLE'; 
    END IF;
    
    INSERT INTO bookings(student_id, property_id, room_id, bed_id, move_in_date,
        duration_months, rent_amount, deposit_amount, status, requested_at,
        owner_response_due_at, payment_due_at, hold_expires_at)
    VALUES (p_student, v_prop.property_id, v_room.room_id, p_bed, p_move_in, p_months,
        v_room.monthly_rent, v_room.deposit,
        CAST(CASE WHEN v_prop.booking_mode = 'instant'::booking_mode THEN 'payment_pending' ELSE 'requested' END AS booking_status),
        now(), 
        CASE WHEN v_prop.booking_mode = 'instant'::booking_mode THEN NULL ELSE v_due END,
        CASE WHEN v_prop.booking_mode = 'instant'::booking_mode THEN v_due ELSE NULL END,
        v_due)
    RETURNING booking_id INTO v_id;
    
    RETURN v_id;
END $$;

-- 4. respond_to_booking
CREATE OR REPLACE FUNCTION respond_to_booking(
    p_booking uuid, p_owner uuid, p_approve bool, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_booking bookings%rowtype;
    v_prop properties%rowtype;
    v_due timestamptz := now() + interval '24 hours';
BEGIN
    SELECT * INTO v_booking FROM bookings WHERE booking_id = p_booking FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'BOOKING_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_booking.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;
    
    IF v_booking.status <> 'requested' THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;
    
    IF p_approve THEN
        UPDATE bookings SET 
            status = 'payment_pending', 
            responded_at = now(), 
            responded_by = p_owner,
            payment_due_at = v_due,
            hold_expires_at = v_due
        WHERE booking_id = p_booking;
    ELSE
        IF p_reason IS NULL OR p_reason = '' THEN
            RAISE EXCEPTION 'REASON_REQUIRED';
        END IF;
        UPDATE bookings SET 
            status = 'rejected', 
            responded_at = now(), 
            responded_by = p_owner,
            rejection_reason = p_reason
        WHERE booking_id = p_booking;
    END IF;
END $$;

-- 5. record_offline_advance
CREATE OR REPLACE FUNCTION record_offline_advance(
    p_booking uuid, p_owner uuid, p_amount numeric, p_mode text, p_reference_no text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_booking bookings%rowtype;
    v_prop properties%rowtype;
BEGIN
    SELECT * INTO v_booking FROM bookings WHERE booking_id = p_booking FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'BOOKING_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_booking.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;
    
    IF v_booking.status <> 'payment_pending' THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;
    IF p_reference_no IS NULL OR p_reference_no = '' THEN RAISE EXCEPTION 'REFERENCE_REQUIRED'; END IF;
    IF p_amount <= 0 OR p_amount > (v_booking.rent_amount + COALESCE(v_booking.deposit_amount, 0)) THEN
        RAISE EXCEPTION 'INVALID_AMOUNT';
    END IF;

    -- Insert payment
    INSERT INTO payments(payment_id, booking_id, payer_id, amount, type, mode, status, reference_no, recorded_by, paid_at)
    VALUES (gen_random_uuid(), p_booking, v_booking.student_id, p_amount, 'booking_advance', p_mode, 'Success', p_reference_no, p_owner, now());

    -- Update booking
    UPDATE bookings SET 
        status = 'confirmed',
        advance_paid = p_amount
    WHERE booking_id = p_booking;
END $$;

-- 6. cancel_booking
CREATE OR REPLACE FUNCTION cancel_booking(
    p_booking uuid, p_actor uuid, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_booking bookings%rowtype;
    v_prop properties%rowtype;
    v_days_before int;
    v_refund_amount numeric := 0;
    v_policy_type text;
BEGIN
    SELECT * INTO v_booking FROM bookings WHERE booking_id = p_booking FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'BOOKING_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_booking.property_id;
    IF p_actor <> v_booking.student_id AND p_actor <> v_prop.owner_id THEN 
        RAISE EXCEPTION 'NOT_AUTHORIZED'; 
    END IF;
    
    IF v_booking.status NOT IN ('requested', 'approved', 'payment_pending', 'confirmed') THEN 
        RAISE EXCEPTION 'INVALID_STATUS'; 
    END IF;
    
    v_days_before := v_booking.move_in_date - current_date;
    
    IF v_booking.status = 'confirmed' AND v_booking.advance_paid > 0 THEN
        v_policy_type := v_prop.cancellation_policy->>'type';
        IF v_policy_type IS NULL THEN 
            v_policy_type := v_prop.cancellation_policy#>>'{}'; 
        END IF;

        IF v_policy_type = 'full' THEN
            v_refund_amount := v_booking.advance_paid;
        ELSIF v_policy_type = 'partial' THEN
            IF v_days_before >= 15 THEN
                v_refund_amount := v_booking.advance_paid;
            ELSIF v_days_before >= 7 THEN
                v_refund_amount := v_booking.advance_paid / 2;
            END IF;
        END IF;
        
        IF v_refund_amount > 0 THEN
            INSERT INTO refunds(refund_id, original_payment_id, amount, status, reason, created_at)
            SELECT gen_random_uuid(), p.payment_id, v_refund_amount, 'Pending', p_reason, now()
            FROM payments p
            WHERE p.payer_id = v_booking.student_id AND p.booking_id = v_booking.booking_id 
              AND p.type = 'booking_advance' AND p.status = 'Success'
            LIMIT 1;
        END IF;
    END IF;

    UPDATE bookings SET 
        status = 'cancelled',
        cancelled_at = now(),
        cancel_reason = p_reason
    WHERE booking_id = p_booking;
END $$;

-- 7. expire_overdue_holds
CREATE OR REPLACE FUNCTION expire_overdue_holds()
RETURNS int LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_count int;
BEGIN
    UPDATE bookings 
    SET status = 'expired'
    WHERE status IN ('requested', 'approved', 'payment_pending')
      AND hold_expires_at < now();
      
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END $$;
