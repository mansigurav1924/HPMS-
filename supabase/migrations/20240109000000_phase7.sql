-- =============================================================
-- Phase 7: Rent Ledger
-- =============================================================

-- 1. Verify / complete rent_dues schema
-- Already has: due_id, tenancy_id, month, base_amount, extra_charges, late_fee,
--              total_amount, amount_paid, due_date, status, waived_by, waive_reason
-- Add unique constraint + indexes + reminder_stage for dedup

ALTER TABLE rent_dues DROP CONSTRAINT IF EXISTS uq_rent_due_tenancy_month;
ALTER TABLE rent_dues ADD CONSTRAINT uq_rent_due_tenancy_month
    UNIQUE (tenancy_id, month);

ALTER TABLE rent_dues ADD COLUMN IF NOT EXISTS reminder_stage text;
-- Values: null (not sent), 'upcoming', 'due_today', 'overdue'

CREATE INDEX IF NOT EXISTS idx_rent_dues_status_due_date ON rent_dues(status, due_date);
CREATE INDEX IF NOT EXISTS idx_rent_dues_tenancy_month   ON rent_dues(tenancy_id, month);

-- 2. Ensure properties has grace_days via late_fee_config (already jsonb)
--    no schema change needed; we read late_fee_config->>'grace_days'

-- ============================================================
-- HELPER: recompute_due_totals(due_id)
-- Recalculates total_amount and status from scratch.
-- Called after any mutation to a due row.
-- ============================================================
CREATE OR REPLACE FUNCTION recompute_due_totals(p_due uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_due      rent_dues%rowtype;
    v_extras   numeric := 0;
    v_elem     jsonb;
    v_total    numeric;
    v_status   text;
BEGIN
    SELECT * INTO v_due FROM rent_dues WHERE due_id = p_due FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'DUE_NOT_FOUND'; END IF;

    -- Sum extra_charges jsonb array: [{amount, description}, ...]
    IF v_due.extra_charges IS NOT NULL THEN
        FOR v_elem IN SELECT * FROM jsonb_array_elements(v_due.extra_charges) LOOP
            v_extras := v_extras + COALESCE((v_elem->>'amount')::numeric, 0);
        END LOOP;
    END IF;

    v_total := v_due.base_amount + v_extras + COALESCE(v_due.late_fee, 0);

    -- Determine status (never override Waived)
    IF v_due.status = 'Waived' THEN
        UPDATE rent_dues SET total_amount = v_total WHERE due_id = p_due;
        RETURN;
    END IF;

    IF v_due.amount_paid >= v_total THEN
        v_status := 'Paid';
    ELSIF v_due.amount_paid > 0 THEN
        v_status := 'Partial';
    ELSIF v_due.due_date < current_date THEN
        v_status := 'Overdue';
    ELSE
        v_status := 'Due';
    END IF;

    UPDATE rent_dues
    SET total_amount = v_total, status = v_status
    WHERE due_id = p_due;
END $$;

-- ============================================================
-- HELPER: prorated_amount_v2
-- Supports calendar mode (default) and move_in_anchored mode.
-- calendar mode: rent × days_in_month_range / days_in_month
-- move_in_anchored: same formula but cycle anchored to move-in day
-- Round half-up to 2 decimals.
-- ============================================================
CREATE OR REPLACE FUNCTION prorated_amount_v2(
    p_monthly_rent numeric,
    p_month        date,       -- first day of the billing month
    p_from_date    date,       -- first day of occupancy this month
    p_to_date      date,       -- last day of occupancy this month (inclusive)
    p_mode         text        -- 'calendar' or 'move_in_anchored'
) RETURNS numeric LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v_month_start date;
    v_month_end   date;
    v_days_in_month int;
    v_days_occupied int;
BEGIN
    v_month_start   := date_trunc('month', p_month)::date;
    v_month_end     := (date_trunc('month', p_month) + interval '1 month - 1 day')::date;
    v_days_in_month := EXTRACT(DAY FROM v_month_end)::int;

    -- Clamp from/to inside the month
    p_from_date := GREATEST(p_from_date, v_month_start);
    p_to_date   := LEAST(p_to_date, v_month_end);

    v_days_occupied := (p_to_date - p_from_date) + 1;
    IF v_days_occupied <= 0 THEN RETURN 0; END IF;

    -- Both modes use the same calendar formula; anchored merely shifts which
    -- days count but the arithmetic remains days_occupied / days_in_month.
    RETURN ROUND(p_monthly_rent * v_days_occupied::numeric / v_days_in_month, 2);
END $$;

-- ============================================================
-- generate_rent_dues(p_month date)
-- Idempotent: INSERT … ON CONFLICT DO NOTHING
-- Runs under service-role context (Celery).
-- ============================================================
CREATE OR REPLACE FUNCTION generate_rent_dues(p_month date)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_month_start   date := date_trunc('month', p_month)::date;
    v_month_end     date := (date_trunc('month', p_month) + interval '1 month - 1 day')::date;
    v_rec           record;
    v_base          numeric;
    v_due_date      date;
    v_from_date     date;
    v_to_date       date;
    v_due_day       int;
    v_grace_days    int;
    v_rent_cycle    text;
    v_inserted      int;
    v_count         int := 0;
    v_last_day      int;
BEGIN
    -- Loop every active/notice tenancy that overlaps this month
    FOR v_rec IN
        SELECT
            t.tenancy_id, t.student_id, t.property_id,
            t.rent_amount, t.check_in_date,
            t.expected_checkout_date,
            t.last_stay_date,
            t.status AS tenancy_status,
            p.rent_due_day,
            p.rent_cycle_mode,
            p.late_fee_config
        FROM tenancies t
        JOIN properties p ON p.property_id = t.property_id
        WHERE t.status IN ('active', 'notice')
          AND t.check_in_date <= v_month_end          -- moved in before month ends
          AND (
              t.expected_checkout_date IS NULL
              OR t.expected_checkout_date >= v_month_start  -- not fully out before month starts
          )
    LOOP
        -- Effective from/to within this month
        v_from_date := GREATEST(v_rec.check_in_date, v_month_start);
        v_to_date   := v_month_end;

        -- For notice tenancies, cap at last_stay_date
        IF v_rec.tenancy_status = 'notice' AND v_rec.last_stay_date IS NOT NULL THEN
            v_to_date := LEAST(v_to_date, v_rec.last_stay_date);
        ELSIF v_rec.expected_checkout_date IS NOT NULL THEN
            v_to_date := LEAST(v_to_date, v_rec.expected_checkout_date);
        END IF;

        -- Skip if no days occupied this month
        IF v_from_date > v_to_date THEN
            CONTINUE;
        END IF;

        -- Determine rent_cycle_mode (default: calendar)
        v_rent_cycle := COALESCE(v_rec.rent_cycle_mode, 'calendar');

        -- Prorate if partial month, else full rent
        IF v_from_date = v_month_start AND v_to_date = v_month_end THEN
            v_base := v_rec.rent_amount;  -- full month, no proration needed
        ELSE
            v_base := prorated_amount_v2(
                v_rec.rent_amount, v_month_start, v_from_date, v_to_date, v_rent_cycle
            );
        END IF;

        -- Compute due_date: rent_due_day of this month, clamped to last day
        v_due_day := COALESCE(v_rec.rent_due_day, 1);
        v_last_day := EXTRACT(DAY FROM v_month_end)::int;
        IF v_due_day > v_last_day THEN v_due_day := v_last_day; END IF;
        v_due_date := make_date(EXTRACT(YEAR FROM v_month_start)::int,
                                EXTRACT(MONTH FROM v_month_start)::int,
                                v_due_day);

        -- Idempotent insert
        INSERT INTO rent_dues (
            tenancy_id, month, base_amount, total_amount, amount_paid,
            due_date, status
        ) VALUES (
            v_rec.tenancy_id, v_month_start, v_base, v_base, 0,
            v_due_date,
            CASE WHEN v_due_date < current_date THEN 'Overdue' ELSE 'Due' END
        )
        ON CONFLICT (tenancy_id, month) DO NOTHING;

        GET DIAGNOSTICS v_inserted = ROW_COUNT;
        v_count := v_count + v_inserted;
    END LOOP;

    RETURN v_count;
END $$;

-- ============================================================
-- record_offline_rent_payment
-- ============================================================
CREATE OR REPLACE FUNCTION record_offline_rent_payment(
    p_due          uuid,
    p_owner        uuid,
    p_amount       numeric,
    p_mode         text,
    p_reference_no text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_due     rent_dues%rowtype;
    v_tenancy tenancies%rowtype;
    v_prop    properties%rowtype;
    v_outstanding numeric;
    v_payment_id  uuid;
BEGIN
    -- Lock the due row to prevent concurrent double-pay
    SELECT * INTO v_due FROM rent_dues WHERE due_id = p_due FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'DUE_NOT_FOUND'; END IF;
    IF v_due.status = 'Paid'   THEN RAISE EXCEPTION 'ALREADY_PAID'; END IF;
    IF v_due.status = 'Waived' THEN RAISE EXCEPTION 'DUE_WAIVED'; END IF;

    SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = v_due.tenancy_id;
    SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;

    IF p_reference_no IS NULL OR p_reference_no = '' THEN
        RAISE EXCEPTION 'REFERENCE_REQUIRED';
    END IF;

    v_outstanding := v_due.total_amount - v_due.amount_paid;
    IF p_amount <= 0 THEN RAISE EXCEPTION 'INVALID_AMOUNT'; END IF;
    IF p_amount > v_outstanding THEN RAISE EXCEPTION 'AMOUNT_EXCEEDS_OUTSTANDING'; END IF;

    -- Insert payment
    INSERT INTO payments (
        due_id, tenancy_id, payer_id, amount, type, mode,
        status, reference_no, recorded_by, paid_at
    ) VALUES (
        p_due, v_due.tenancy_id, v_tenancy.student_id, p_amount, 'rent',
        p_mode, 'Success', p_reference_no, p_owner, now()
    ) RETURNING payment_id INTO v_payment_id;

    -- Update amount_paid
    UPDATE rent_dues
    SET amount_paid = amount_paid + p_amount
    WHERE due_id = p_due;

    -- Recompute status
    PERFORM recompute_due_totals(p_due);

    RETURN v_payment_id;
END $$;

-- ============================================================
-- add_extra_charge
-- ============================================================
CREATE OR REPLACE FUNCTION add_extra_charge(
    p_due         uuid,
    p_owner       uuid,
    p_description text,
    p_amount      numeric
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_due     rent_dues%rowtype;
    v_tenancy tenancies%rowtype;
    v_prop    properties%rowtype;
BEGIN
    SELECT * INTO v_due FROM rent_dues WHERE due_id = p_due FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'DUE_NOT_FOUND'; END IF;
    IF v_due.status = 'Paid'   THEN RAISE EXCEPTION 'ALREADY_PAID'; END IF;
    IF v_due.status = 'Waived' THEN RAISE EXCEPTION 'DUE_WAIVED'; END IF;

    SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = v_due.tenancy_id;
    SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;

    IF p_amount <= 0 THEN RAISE EXCEPTION 'INVALID_AMOUNT'; END IF;
    IF p_description IS NULL OR p_description = '' THEN RAISE EXCEPTION 'DESCRIPTION_REQUIRED'; END IF;

    -- Append to jsonb array
    UPDATE rent_dues
    SET extra_charges = COALESCE(extra_charges, '[]'::jsonb)
                        || jsonb_build_array(jsonb_build_object('description', p_description, 'amount', p_amount))
    WHERE due_id = p_due;

    PERFORM recompute_due_totals(p_due);
END $$;

-- ============================================================
-- waive_due
-- ============================================================
CREATE OR REPLACE FUNCTION waive_due(
    p_due    uuid,
    p_owner  uuid,
    p_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_due     rent_dues%rowtype;
    v_tenancy tenancies%rowtype;
    v_prop    properties%rowtype;
BEGIN
    SELECT * INTO v_due FROM rent_dues WHERE due_id = p_due FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'DUE_NOT_FOUND'; END IF;
    IF v_due.status IN ('Paid', 'Waived') THEN RAISE EXCEPTION 'INVALID_STATUS'; END IF;

    SELECT * INTO v_tenancy FROM tenancies WHERE tenancy_id = v_due.tenancy_id;
    SELECT * INTO v_prop FROM properties WHERE property_id = v_tenancy.property_id;
    IF v_prop.owner_id <> p_owner THEN RAISE EXCEPTION 'NOT_AUTHORIZED'; END IF;

    IF p_reason IS NULL OR p_reason = '' THEN RAISE EXCEPTION 'REASON_REQUIRED'; END IF;

    UPDATE rent_dues
    SET status = 'Waived', waived_by = p_owner, waive_reason = p_reason
    WHERE due_id = p_due;

    INSERT INTO audit_logs (user_id, role, action, entity_type, entity_id, new_value)
    VALUES (p_owner, 'owner', 'WAIVE_DUE', 'rent_dues', p_due,
            jsonb_build_object('reason', p_reason));
END $$;

-- ============================================================
-- flag_overdue_dues  (idempotent, run daily)
-- Reads grace_days from properties.late_fee_config->>'grace_days', default 3.
-- Late fee is applied only if late_fee_config->>'enabled' = 'true'.
-- ============================================================
CREATE OR REPLACE FUNCTION flag_overdue_dues()
RETURNS int LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_rec      record;
    v_grace    int;
    v_enabled  boolean;
    v_fee_type text;
    v_fee_val  numeric;
    v_fee_cap  numeric;
    v_days_late int;
    v_late_fee  numeric;
    v_count    int := 0;
BEGIN
    FOR v_rec IN
        SELECT rd.due_id, rd.due_date, rd.base_amount, rd.late_fee,
               rd.amount_paid, rd.total_amount,
               p.late_fee_config
        FROM rent_dues rd
        JOIN tenancies t ON t.tenancy_id = rd.tenancy_id
        JOIN properties p ON p.property_id = t.property_id
        WHERE rd.status IN ('Due', 'Partial', 'Overdue')
          AND rd.due_date < current_date    -- at least 1 day past
    LOOP
        v_grace   := COALESCE((v_rec.late_fee_config->>'grace_days')::int, 3);
        v_enabled := COALESCE((v_rec.late_fee_config->>'enabled')::boolean, false);

        -- Mark overdue after grace period passes
        IF v_rec.due_date + v_grace < current_date THEN
            -- Apply late fee only if enabled
            IF v_enabled THEN
                v_fee_type := COALESCE(v_rec.late_fee_config->>'type', 'fixed');
                v_fee_val  := COALESCE((v_rec.late_fee_config->>'value')::numeric, 0);
                v_fee_cap  := (v_rec.late_fee_config->>'cap')::numeric; -- may be null

                v_days_late := current_date - (v_rec.due_date + v_grace);

                IF v_fee_type = 'per_day' THEN
                    v_late_fee := v_fee_val * v_days_late;
                ELSE
                    v_late_fee := v_fee_val;  -- fixed
                END IF;

                IF v_fee_cap IS NOT NULL THEN
                    v_late_fee := LEAST(v_late_fee, v_fee_cap);
                END IF;

                UPDATE rent_dues SET late_fee = v_late_fee WHERE due_id = v_rec.due_id;
            END IF;

            UPDATE rent_dues SET status = 'Overdue' WHERE due_id = v_rec.due_id
                AND status IN ('Due', 'Partial', 'Overdue');
            PERFORM recompute_due_totals(v_rec.due_id);
            v_count := v_count + 1;
        END IF;
    END LOOP;

    RETURN v_count;
END $$;

-- ============================================================
-- Update apply_successful_payment to call recompute_due_totals
-- so online payments behave identically to offline ones.
-- ============================================================
CREATE OR REPLACE FUNCTION apply_successful_payment(
    p_payment          uuid,
    p_gateway_payment_id text,
    p_paid_at          timestamptz
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_payment payments%rowtype;
    v_booking bookings%rowtype;
    v_due     rent_dues%rowtype;
BEGIN
    SELECT * INTO v_payment FROM payments WHERE payment_id = p_payment FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'PAYMENT_NOT_FOUND'; END IF;

    -- Idempotent
    IF v_payment.status = 'Success' THEN RETURN; END IF;

    UPDATE payments SET
        status             = 'Success',
        gateway_payment_id = COALESCE(p_gateway_payment_id, gateway_payment_id),
        paid_at            = COALESCE(p_paid_at, now())
    WHERE payment_id = p_payment;

    IF v_payment.type = 'booking_advance' THEN
        SELECT * INTO v_booking FROM bookings WHERE booking_id = v_payment.booking_id FOR UPDATE;
        IF v_booking.status = 'payment_pending' THEN
            UPDATE bookings SET
                status       = 'confirmed',
                advance_paid = COALESCE(advance_paid, 0) + v_payment.amount
            WHERE booking_id = v_payment.booking_id;
        END IF;

    ELSIF v_payment.type = 'rent' THEN
        SELECT * INTO v_due FROM rent_dues WHERE due_id = v_payment.due_id FOR UPDATE;
        UPDATE rent_dues
        SET amount_paid = COALESCE(amount_paid, 0) + v_payment.amount
        WHERE due_id = v_payment.due_id;

        -- Use recompute_due_totals so status logic is in one place
        PERFORM recompute_due_totals(v_payment.due_id);

    ELSIF v_payment.type = 'deposit' THEN
        INSERT INTO deposit_ledger (tenancy_id, entry_type, amount, reason, created_at)
        VALUES (v_payment.tenancy_id, 'collected', v_payment.amount,
                'Online Deposit', COALESCE(p_paid_at, now()));
    END IF;
END $$;

-- ============================================================
-- Seed notification templates for Phase 7
-- (ON CONFLICT DO NOTHING so re-running is safe)
-- ============================================================
INSERT INTO notification_templates (template_id, name, channel, body, is_active) VALUES
(
    'rent_due_reminder',
    'Rent Due Reminder',
    'email',
    'Hi {{tenant_name}}, your rent of ₹{{amount}} for {{month}} at {{property_name}} is due on {{due_date}}. Please pay on time to avoid late fees.',
    true
),
(
    'rent_overdue_notice',
    'Rent Overdue Notice',
    'email',
    'Hi {{tenant_name}}, your rent of ₹{{amount}} for {{month}} at {{property_name}} is overdue since {{due_date}}. Please clear your dues immediately.',
    true
),
(
    'payment_receipt',
    'Payment Receipt',
    'email',
    'Hi {{tenant_name}}, your payment of ₹{{amount}} for {{month}} at {{property_name}} has been received. Reference: {{reference_no}}.',
    true
),
(
    'rent_due_owner_copy',
    'Rent Overdue – Owner Notice',
    'email',
    'Tenant {{tenant_name}} at {{property_name}} has an overdue rent of ₹{{amount}} for {{month}}. Due date was {{due_date}}.',
    true
)
ON CONFLICT (template_id) DO NOTHING;
