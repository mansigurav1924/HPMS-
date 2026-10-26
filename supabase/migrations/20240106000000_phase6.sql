-- Phase 6: Tenancy Module Migrations

-- 1. Alter tenancies table
ALTER TABLE tenancies ADD COLUMN IF NOT EXISTS last_stay_date date;

-- 2. Create deposit_ledger table
CREATE TABLE IF NOT EXISTS deposit_ledger (
    ledger_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenancy_id uuid NOT NULL REFERENCES tenancies(tenancy_id),
    type text NOT NULL CHECK (type IN ('collected', 'deduction', 'refund')),
    amount numeric(12,2) NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 3. Alter refunds table
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS tenancy_id uuid REFERENCES tenancies(tenancy_id);
ALTER TABLE refunds ALTER COLUMN payment_id DROP NOT NULL;

-- 4. check_in
CREATE OR REPLACE FUNCTION check_in(
    p_booking uuid, p_owner uuid, p_check_in_date date, p_deposit_collected numeric, p_mode text, p_reference_no text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_booking bookings%rowtype;
    v_prop properties%rowtype;
    v_student_profile student_profiles%rowtype;
    v_tenancy_id uuid;
BEGIN
    SELECT * INTO v_booking FROM bookings WHERE booking_id = p_booking FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'BOOKING_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_booking.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;
    
    IF v_booking.status <> 'confirmed' THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;
    
    SELECT * INTO v_student_profile FROM student_profiles WHERE student_id = v_booking.student_id;
    IF v_student_profile.id_proof_url IS NULL OR v_student_profile.id_proof_url = '' THEN
        RAISE EXCEPTION 'ID_PROOF_MISSING';
    END IF;

    -- Create tenancy
    INSERT INTO tenancies(
        booking_id, student_id, property_id, bed_id, 
        check_in_date, expected_checkout_date, rent_amount, deposit_paid, status
    ) VALUES (
        p_booking, v_booking.student_id, v_booking.property_id, v_booking.bed_id,
        p_check_in_date, p_check_in_date + (v_booking.duration_months || ' months')::interval,
        v_booking.rent_amount, p_deposit_collected, 'active'
    ) RETURNING tenancy_id INTO v_tenancy_id;

    -- Deposit ledger and payment
    INSERT INTO deposit_ledger(tenancy_id, entry_type, amount, reason)
    VALUES (v_tenancy_id, 'collected', p_deposit_collected, 'Initial deposit');
    
    IF p_deposit_collected > 0 THEN
        INSERT INTO payments(booking_id, payer_id, amount, type, mode, status, reference_no, recorded_by, paid_at)
        VALUES (p_booking, v_booking.student_id, p_deposit_collected, 'deposit', p_mode, 'Success', p_reference_no, p_owner, now());
    END IF;

    -- Update booking
    UPDATE bookings SET status = 'checked_in' WHERE booking_id = p_booking;

    RETURN v_tenancy_id;
END $$;

-- 5. give_notice
CREATE OR REPLACE FUNCTION give_notice(
    p_tenancy uuid, p_actor uuid, p_notice_date date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_tenancy tenancies%rowtype;
    v_prop properties%rowtype;
BEGIN
    SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = p_tenancy FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'TENANCY_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
    IF p_actor <> v_tenancy.student_id AND p_actor <> v_prop.owner_id THEN 
        RAISE EXCEPTION 'NOT_AUTHORIZED'; 
    END IF;
    
    IF v_tenancy.status <> 'active' THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;

    UPDATE tenancies SET 
        status = 'notice',
        notice_given_on = p_notice_date,
        notice_given_by = p_actor,
        last_stay_date = p_notice_date + (COALESCE(v_prop.notice_period_days, 30) || ' days')::interval
    WHERE tenancy_id = p_tenancy;
END $$;

-- 6. room_transfer
CREATE OR REPLACE FUNCTION room_transfer(
    p_tenancy uuid, p_owner uuid, p_new_bed uuid, p_effective_date date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_tenancy tenancies%rowtype;
    v_prop properties%rowtype;
    v_new_bed beds%rowtype;
    v_new_room rooms%rowtype;
BEGIN
    SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = p_tenancy;
    IF NOT FOUND THEN RAISE EXCEPTION 'TENANCY_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;
    IF v_tenancy.status NOT IN ('active', 'notice') THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;

    -- Lock beds in order
    IF v_tenancy.bed_id < p_new_bed THEN
        PERFORM * FROM beds WHERE bed_id = v_tenancy.bed_id FOR UPDATE;
        SELECT * INTO v_new_bed FROM beds WHERE bed_id = p_new_bed FOR UPDATE;
    ELSE
        SELECT * INTO v_new_bed FROM beds WHERE bed_id = p_new_bed FOR UPDATE;
        PERFORM * FROM beds WHERE bed_id = v_tenancy.bed_id FOR UPDATE;
    END IF;

    IF v_new_bed.status <> 'available' THEN RAISE EXCEPTION 'BED_UNAVAILABLE'; END IF;
    
    SELECT * INTO v_new_room FROM rooms WHERE room_id = v_new_bed.room_id;
    
    -- Update tenancy
    UPDATE tenancies SET 
        bed_id = p_new_bed,
        rent_amount = v_new_room.monthly_rent
    WHERE tenancy_id = p_tenancy;
    
    -- Logging
    INSERT INTO audit_logs(user_id, role, action, entity_type, entity_id, new_value)
    VALUES (p_owner, 'owner', 'ROOM_TRANSFER', 'tenancies', p_tenancy, jsonb_build_object('old_bed', v_tenancy.bed_id, 'new_bed', p_new_bed, 'effective_date', p_effective_date));
END $$;

-- 7. check_out
CREATE OR REPLACE FUNCTION check_out(
    p_tenancy uuid, p_owner uuid, p_checkout_date date, p_deductions jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_tenancy tenancies%rowtype;
    v_prop properties%rowtype;
    v_deduction jsonb;
    v_total_deductions numeric := 0;
    v_refund numeric;
BEGIN
    SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = p_tenancy FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'TENANCY_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;
    
    IF v_tenancy.status NOT IN ('active', 'notice') THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;
    
    IF p_deductions IS NOT NULL AND jsonb_array_length(p_deductions) > 0 THEN
        FOR v_deduction IN SELECT * FROM jsonb_array_elements(p_deductions) LOOP
            v_total_deductions := v_total_deductions + (v_deduction->>'amount')::numeric;
            INSERT INTO deposit_ledger(tenancy_id, entry_type, amount, reason)
            VALUES (p_tenancy, 'deduction', (v_deduction->>'amount')::numeric, v_deduction->>'reason');
        END LOOP;
    END IF;

    v_refund := COALESCE(v_tenancy.deposit_paid, 0) - v_total_deductions;
    
    IF v_refund < 0 THEN
        RAISE EXCEPTION 'DEDUCTIONS_EXCEED_DEPOSIT';
    END IF;

    IF v_refund > 0 THEN
        INSERT INTO deposit_ledger(tenancy_id, entry_type, amount, reason)
        VALUES (p_tenancy, 'refund', v_refund, 'Deposit refund');
        
        INSERT INTO refunds(tenancy_id, amount, status, reason)
        VALUES (p_tenancy, v_refund, 'pending', 'Checkout deposit refund');
    END IF;

    UPDATE tenancies SET 
        status = 'checked_out',
        actual_checkout_date = p_checkout_date
    WHERE tenancy_id = p_tenancy;
    
    UPDATE bookings SET status = 'closed' WHERE booking_id = v_tenancy.booking_id;
END $$;

-- 8. evict
CREATE OR REPLACE FUNCTION evict(
    p_tenancy uuid, p_owner uuid, p_reason text, p_deductions jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_tenancy tenancies%rowtype;
    v_prop properties%rowtype;
    v_deduction jsonb;
    v_total_deductions numeric := 0;
    v_refund numeric;
BEGIN
    SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = p_tenancy FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'TENANCY_NOT_FOUND'; END IF;
    
    SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;
    IF p_reason IS NULL OR p_reason = '' THEN RAISE EXCEPTION 'REASON_REQUIRED'; END IF;
    
    IF v_tenancy.status NOT IN ('active', 'notice') THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;
    
    IF p_deductions IS NOT NULL AND jsonb_array_length(p_deductions) > 0 THEN
        FOR v_deduction IN SELECT * FROM jsonb_array_elements(p_deductions) LOOP
            v_total_deductions := v_total_deductions + (v_deduction->>'amount')::numeric;
            INSERT INTO deposit_ledger(tenancy_id, entry_type, amount, reason)
            VALUES (p_tenancy, 'deduction', (v_deduction->>'amount')::numeric, v_deduction->>'reason');
        END LOOP;
    END IF;

    v_refund := COALESCE(v_tenancy.deposit_paid, 0) - v_total_deductions;
    IF v_refund < 0 THEN RAISE EXCEPTION 'DEDUCTIONS_EXCEED_DEPOSIT'; END IF;

    IF v_refund > 0 THEN
        INSERT INTO deposit_ledger(tenancy_id, entry_type, amount, reason)
        VALUES (p_tenancy, 'refund', v_refund, 'Deposit refund');
        INSERT INTO refunds(tenancy_id, amount, status, reason)
        VALUES (p_tenancy, v_refund, 'pending', 'Eviction deposit refund');
    END IF;

    UPDATE tenancies SET status = 'evicted', actual_checkout_date = current_date WHERE tenancy_id = p_tenancy;
    UPDATE bookings SET status = 'closed' WHERE booking_id = v_tenancy.booking_id;
    
    INSERT INTO audit_logs(user_id, role, action, entity_type, entity_id, new_value)
    VALUES (p_owner, 'admin', 'EVICT_TENANT', 'tenancies', p_tenancy, jsonb_build_object('reason', p_reason));
END $$;

-- 9. mark_refund_processed
CREATE OR REPLACE FUNCTION mark_refund_processed(
    p_refund uuid, p_owner uuid, p_reference_no text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_refund refunds%rowtype;
    v_tenancy tenancies%rowtype;
    v_prop properties%rowtype;
BEGIN
    SELECT * INTO v_refund FROM refunds WHERE refund_id = p_refund FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'REFUND_NOT_FOUND'; END IF;
    
    -- Need to authorize via tenancy
    IF v_refund.tenancy_id IS NOT NULL THEN
        SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = v_refund.tenancy_id;
        SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
        IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;
    END IF;
    
    UPDATE refunds SET status = 'Processed', reason = p_reference_no WHERE refund_id = p_refund;
END $$;

-- 10. Proration helper function
CREATE OR REPLACE FUNCTION prorated_amount(
    monthly_rent numeric, from_date date, to_date_or_month_end date)
RETURNS numeric LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v_days_in_month numeric;
    v_remaining_days numeric;
BEGIN
    -- using calendar month of the from_date
    v_days_in_month := EXTRACT(DAY FROM (date_trunc('month', from_date) + interval '1 month - 1 day'));
    v_remaining_days := (to_date_or_month_end - from_date) + 1;
    IF v_remaining_days < 0 THEN v_remaining_days := 0; END IF;
    IF v_remaining_days > v_days_in_month THEN v_remaining_days := v_days_in_month; END IF;
    
    RETURN ROUND(monthly_rent * v_remaining_days / v_days_in_month, 2);
END $$;


-- 10. Fix trigger
DROP TRIGGER IF EXISTS trg_tenancies_sync ON tenancies;
CREATE TRIGGER trg_tenancies_sync AFTER INSERT OR UPDATE OF status, bed_id ON tenancies
FOR EACH ROW EXECUTE FUNCTION sync_bed_status();


CREATE OR REPLACE FUNCTION sync_bed_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.bed_id IS NOT NULL THEN
        UPDATE beds SET status = CASE
        WHEN EXISTS (SELECT 1 FROM tenancies t WHERE t.bed_id = NEW.bed_id AND t.status IN ('active','notice')) THEN 'occupied'
        WHEN EXISTS (SELECT 1 FROM bookings b WHERE b.bed_id = NEW.bed_id AND b.status IN ('requested','approved','payment_pending','confirmed')) THEN 'reserved'
        WHEN EXISTS (SELECT 1 FROM maintenance_tasks m WHERE m.bed_id = NEW.bed_id AND m.blocks_availability AND m.status <> 'done') THEN 'maintenance'
        ELSE 'available' END
        WHERE bed_id = NEW.bed_id;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.bed_id IS DISTINCT FROM NEW.bed_id AND OLD.bed_id IS NOT NULL THEN
        UPDATE beds SET status = CASE
        WHEN EXISTS (SELECT 1 FROM tenancies t WHERE t.bed_id = OLD.bed_id AND t.status IN ('active','notice')) THEN 'occupied'
        WHEN EXISTS (SELECT 1 FROM bookings b WHERE b.bed_id = OLD.bed_id AND b.status IN ('requested','approved','payment_pending','confirmed')) THEN 'reserved'
        WHEN EXISTS (SELECT 1 FROM maintenance_tasks m WHERE m.bed_id = OLD.bed_id AND m.blocks_availability AND m.status <> 'done') THEN 'maintenance'
        ELSE 'available' END
        WHERE bed_id = OLD.bed_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_tenancies_sync ON tenancies;
CREATE TRIGGER trg_tenancies_sync AFTER INSERT OR UPDATE OF status, bed_id ON tenancies
FOR EACH ROW EXECUTE FUNCTION sync_bed_status();
