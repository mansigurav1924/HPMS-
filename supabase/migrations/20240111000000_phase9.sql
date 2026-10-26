-- ==============================================================================
-- Migration: Phase 9 (Notices, Reviews, Notifications, PII Audit Log)
-- ==============================================================================

-- ─── 1. NOTICES ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS notice_reads (
    notice_id uuid NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    read_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (notice_id, user_id)
);

-- RLS: notices
ALTER TABLE notices ENABLE ROW LEVEL SECURITY;
ALTER TABLE notice_reads ENABLE ROW LEVEL SECURITY;

-- Owner/Warden can create/update/delete
DROP POLICY IF EXISTS "Owner/Warden notices ALL" ON notices;
CREATE POLICY "Owner/Warden notices ALL" ON notices
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM properties p WHERE p.property_id = notices.property_id AND p.owner_id = public.get_auth_uid()
        )
        OR EXISTS (
            SELECT 1 FROM property_staff ps WHERE ps.property_id = notices.property_id AND ps.user_id = public.get_auth_uid()
        )
    );

-- Tenants can read non-expired notices at their property
DROP POLICY IF EXISTS "Tenants notices SELECT" ON notices;
CREATE POLICY "Tenants notices SELECT" ON notices
    FOR SELECT
    USING (
        (expires_at IS NULL OR expires_at > now())
        AND EXISTS (
            SELECT 1 FROM tenancies t
            JOIN bookings b ON t.booking_id = b.booking_id
            WHERE t.property_id = notices.property_id
              AND b.student_id = public.get_auth_uid()
              AND t.status IN ('active', 'notice')
        )
    );

-- Admin sees all
DROP POLICY IF EXISTS "Admin notices ALL" ON notices;
CREATE POLICY "Admin notices ALL" ON notices
    FOR ALL USING (public.get_auth_role() = 'admin');

-- RLS: notice_reads
DROP POLICY IF EXISTS "Users notice_reads ALL" ON notice_reads;
CREATE POLICY "Users notice_reads ALL" ON notice_reads
    FOR ALL USING (user_id = public.get_auth_uid());

DROP POLICY IF EXISTS "Admin notice_reads ALL" ON notice_reads;
CREATE POLICY "Admin notice_reads ALL" ON notice_reads
    FOR ALL USING (public.get_auth_role() = 'admin');


-- ─── 2. REVIEWS & RATINGS ───────────────────────────────────────────────────

ALTER TABLE reviews
ADD COLUMN IF NOT EXISTS hidden_by uuid REFERENCES users(user_id),
ADD COLUMN IF NOT EXISTS hidden_reason text,
ADD COLUMN IF NOT EXISTS hidden_at timestamptz;

-- Add review_count to properties
ALTER TABLE properties
ADD COLUMN IF NOT EXISTS review_count int NOT NULL DEFAULT 0;

-- Function: create_review
CREATE OR REPLACE FUNCTION create_review(
    p_student uuid,
    p_tenancy uuid,
    p_rating int,
    p_comment text
) RETURNS uuid AS $$
DECLARE
    v_property uuid;
    v_status text;
    v_review_id uuid;
BEGIN
    IF p_rating < 1 OR p_rating > 5 THEN
        RAISE EXCEPTION 'Rating must be between 1 and 5';
    END IF;

    IF length(p_comment) > 1000 THEN
        RAISE EXCEPTION 'Comment exceeds 1000 characters';
    END IF;

    SELECT t.property_id, t.status INTO v_property, v_status
    FROM tenancies t
    JOIN bookings b ON t.booking_id = b.booking_id
    WHERE t.tenancy_id = p_tenancy AND b.student_id = p_student;

    IF v_property IS NULL THEN
        RAISE EXCEPTION 'Tenancy not found or does not belong to student';
    END IF;

    IF v_status NOT IN ('active', 'notice', 'checked_out') THEN
        RAISE EXCEPTION 'Review only allowed for active, notice, or checked_out tenancies';
    END IF;

    INSERT INTO reviews (tenancy_id, property_id, student_id, rating, comment)
    VALUES (p_tenancy, v_property, p_student, p_rating, p_comment)
    RETURNING review_id INTO v_review_id;

    RETURN v_review_id;
EXCEPTION
    WHEN unique_violation THEN
        RAISE EXCEPTION 'A review already exists for this tenancy';
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- Function/Trigger: update_property_rating
CREATE OR REPLACE FUNCTION update_property_rating() RETURNS TRIGGER AS $$
DECLARE
    v_prop_id uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_prop_id := OLD.property_id;
    ELSE
        v_prop_id := NEW.property_id;
    END IF;

    UPDATE properties
    SET avg_rating = COALESCE((
            SELECT ROUND(AVG(rating)::numeric, 1)
            FROM reviews
            WHERE property_id = v_prop_id AND status = 'Visible'
        ), 0),
        review_count = (
            SELECT COUNT(*)
            FROM reviews
            WHERE property_id = v_prop_id AND status = 'Visible'
        )
    WHERE property_id = v_prop_id;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_property_rating ON reviews;
CREATE TRIGGER trg_update_property_rating
AFTER INSERT OR UPDATE OF rating, status OR DELETE
ON reviews
FOR EACH ROW EXECUTE FUNCTION update_property_rating();


-- ─── 3. NOTIFICATION SYSTEM ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS notification_preferences (
    user_id uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    email_enabled boolean NOT NULL DEFAULT true,
    sms_enabled boolean NOT NULL DEFAULT false,
    in_app_enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS suppression_list (
    email text PRIMARY KEY,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Add email_bounced to users
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_bounced boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS in_app_notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title text NOT NULL,
    body text NOT NULL,
    link text,
    read_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE in_app_notifications ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Users in_app_notifications ALL" ON in_app_notifications;
CREATE POLICY "Users in_app_notifications ALL" ON in_app_notifications
    FOR ALL USING (user_id = public.get_auth_uid());
DROP POLICY IF EXISTS "Admin in_app_notifications ALL" ON in_app_notifications;
CREATE POLICY "Admin in_app_notifications ALL" ON in_app_notifications
    FOR ALL USING (public.get_auth_role() = 'admin');

-- Processed webhooks table for idempotency
CREATE TABLE IF NOT EXISTS processed_webhooks (
    event_id text PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);

-- Seed Notification Templates (Version 1)
-- Excluding payment-only templates per instructions (they are already commented out in use, but we seed them as inactive)
INSERT INTO notification_templates (template_id, name, channel, version, body, is_active) VALUES
('guardian_consent_request', 'Guardian Consent Request', 'email', 1, 'Dear {{guardian_name}}, your ward {{student_name}} requires consent.', true),
('notice_announcement', 'Notice / Announcement', 'email', 1, 'Notice from {{property_name}}: {{notice_title}} - {{notice_body}}', true),
('review_request', 'Review Request', 'email', 1, 'Hi {{student_name}}, please review your stay at {{property_name}}.', true),
('welcome_verification', 'Welcome / Email Verification', 'email', 1, 'Welcome {{name}}! Please verify your email.', true),
('owner_approval', 'Owner Approval / Rejection', 'email', 1, 'Your owner profile status is now {{status}}.', true),
('checkout_settlement', 'Check-out & Deposit Settlement', 'email', 1, 'Check-out complete for {{property_name}}. Deposit refund: {{refund_amount}}.', true),
('payment_receipt', 'Payment Receipt', 'email', 1, 'Receipt for {{amount}}.', false),
('booking_payment_reminder', 'Booking Payment Reminder', 'email', 1, 'Reminder: Booking payment due.', false),
('rent_due_reminder', 'Rent Due Reminder', 'email', 1, 'Rent is due.', false),
('rent_overdue_notice', 'Rent Overdue Notice', 'email', 1, 'Rent is overdue.', false)
ON CONFLICT (template_id) DO UPDATE SET is_active = EXCLUDED.is_active, body = EXCLUDED.body;


-- ─── 4. PII-SAFE APPEND-ONLY AUDIT LOG ──────────────────────────────────────

-- Revoke UPDATE/DELETE on audit_logs
REVOKE UPDATE, DELETE ON audit_logs FROM authenticated;
REVOKE UPDATE, DELETE ON audit_logs FROM anon;
REVOKE UPDATE, DELETE ON audit_logs FROM service_role;

-- Defense in depth trigger
CREATE OR REPLACE FUNCTION prevent_audit_update_delete() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Audit logs are append-only. Updates and deletes are strictly prohibited.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_audit_update_delete ON audit_logs;
CREATE TRIGGER trg_prevent_audit_update_delete
BEFORE UPDATE OR DELETE ON audit_logs
FOR EACH ROW EXECUTE FUNCTION prevent_audit_update_delete();

-- Allow INSERT to audit_logs for authenticated users
DROP POLICY IF EXISTS "Allow insert audit" ON audit_logs;
CREATE POLICY "Allow insert audit" ON audit_logs FOR INSERT WITH CHECK (true);
