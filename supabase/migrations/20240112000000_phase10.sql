-- ==========================================
-- PHASE 10: Dashboards, Reports, Retention
-- ==========================================

DROP MATERIALIZED VIEW IF EXISTS owner_dashboard_stats CASCADE;
DROP MATERIALIZED VIEW IF EXISTS admin_dashboard_stats CASCADE;
DROP TABLE IF EXISTS retention_policies CASCADE;
DROP TABLE IF EXISTS data_access_requests CASCADE;

-- ─── 1. EXTENSIONS & INDEXES ─────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_users_name_trgm ON users USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_users_email_trgm ON users USING gin (email gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_properties_name_trgm ON properties USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_properties_city_trgm ON properties USING gin (city gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_rooms_number_trgm ON rooms USING gin (room_number gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_complaints_desc_trgm ON complaints USING gin (description gin_trgm_ops);

-- ─── 2. DASHBOARD MAT VIEWS ──────────────────────────────────────────────────

CREATE MATERIALIZED VIEW owner_dashboard_stats AS
SELECT 
    p.property_id,
    p.owner_id,
    p.name as property_name,
    COUNT(b.bed_id) AS total_beds,
    COUNT(b.bed_id) FILTER (WHERE b.status = 'vacant' OR b.status = 'available') AS vacant_beds,
    COUNT(b.bed_id) FILTER (WHERE b.status = 'occupied') AS occupied_beds,
    COUNT(b.bed_id) FILTER (WHERE b.status = 'reserved') AS reserved_beds,
    COUNT(b.bed_id) FILTER (WHERE b.status = 'maintenance') AS maintenance_beds,
    (
        SELECT COUNT(*) FROM complaints c
        WHERE c.property_id = p.property_id AND c.status IN ('open', 'in_progress', 'reopened', 'acknowledged')
    ) AS open_complaints,
    (
        SELECT COUNT(*) FROM bookings bk
        WHERE bk.property_id = p.property_id AND bk.status = 'requested'
    ) AS pending_booking_requests,
    (
        SELECT COUNT(*) FROM tenancies t
        WHERE t.property_id = p.property_id AND t.status IN ('active', 'notice')
        AND t.expected_checkout_date <= (CURRENT_DATE + interval '30 days')
    ) AS upcoming_checkouts,
    (
        SELECT COUNT(*) FROM bookings bk
        WHERE bk.property_id = p.property_id AND bk.status = 'no_show'
    ) AS flagged_no_shows
FROM properties p
LEFT JOIN rooms r ON p.property_id = r.property_id
LEFT JOIN beds b ON r.room_id = b.room_id
GROUP BY p.property_id, p.owner_id, p.name;

CREATE UNIQUE INDEX idx_owner_dashboard_stats_prop_id ON owner_dashboard_stats(property_id);

CREATE MATERIALIZED VIEW admin_dashboard_stats AS
SELECT
    1 as id,
    (SELECT COALESCE(jsonb_object_agg(role, count), '{}'::jsonb) FROM (SELECT role, COUNT(*) as count FROM users GROUP BY role) sub) as users_by_role,
    (SELECT COALESCE(jsonb_object_agg(status, count), '{}'::jsonb) FROM (SELECT status, COUNT(*) as count FROM properties GROUP BY status) sub) as properties_by_status,
    (SELECT COALESCE(jsonb_object_agg(status, count), '{}'::jsonb) FROM (SELECT status, COUNT(*) as count FROM bookings GROUP BY status) sub) as bookings_by_status,
    (SELECT COUNT(*) FROM owner_profiles WHERE verification_status = 'Pending Verification') as pending_owner_verifications,
    (SELECT COUNT(*) FROM properties WHERE status = 'Pending Approval') as pending_property_verifications,
    (SELECT COUNT(*) FROM complaints WHERE escalated = true AND status NOT IN ('resolved', 'closed')) as escalated_complaints,
    (SELECT COUNT(*) FROM reviews WHERE status = 'Flagged') as flagged_reviews,
    (SELECT COUNT(*) FROM bookings WHERE manual_review = true) as manual_review_gender_cases,
    (
        SELECT jsonb_build_object(
            'failure_rate', COALESCE((SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*), 0)) * 100, 0),
            'bounce_rate', COALESCE((SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*), 0)) * 100, 0)
        ) FROM notification_logs
    ) as notification_rates,
    (
        SELECT COALESCE(jsonb_object_agg(city, supply_demand), '{}'::jsonb) FROM (
            SELECT 
                p.city, 
                jsonb_build_object('supply', COUNT(b.bed_id), 'demand', (SELECT COUNT(*) FROM bookings bk JOIN properties p2 ON bk.property_id = p2.property_id WHERE p2.city = p.city)) as supply_demand
            FROM properties p
            LEFT JOIN rooms r ON p.property_id = r.property_id
            LEFT JOIN beds b ON r.room_id = b.room_id
            GROUP BY p.city
        ) sub
    ) as city_supply_demand;

CREATE UNIQUE INDEX idx_admin_dashboard_stats_id ON admin_dashboard_stats(id);

-- ─── 3. RETENTION AUTOMATION ──────────────────────────────────────────────────

CREATE TABLE retention_policies (
    record_type text PRIMARY KEY,
    retention_months int NOT NULL,
    action text NOT NULL CHECK (action IN ('purge', 'anonymize', 'flag')),
    is_active boolean NOT NULL DEFAULT true
);

INSERT INTO retention_policies (record_type, retention_months, action, is_active) VALUES
('id_proof', 6, 'purge', true),
('inactive_account', 24, 'flag', true),
('notification_log', 12, 'purge', true),
('complaint_media', 12, 'purge', true),
('expired_notice', 6, 'purge', true),
('webhook_payload', 3, 'purge', true)
ON CONFLICT (record_type) DO NOTHING;

ALTER TABLE notification_logs ADD COLUMN IF NOT EXISTS retention_expires_at timestamptz;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS retention_expires_at timestamptz;
ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS retention_expires_at timestamptz;

-- Set defaults on creation where possible:
CREATE OR REPLACE FUNCTION set_retention_expires_at() RETURNS TRIGGER AS $$
DECLARE
    v_months int;
BEGIN
    SELECT retention_months INTO v_months FROM retention_policies WHERE record_type = TG_ARGV[0] AND is_active = true;
    IF v_months IS NOT NULL THEN
        NEW.retention_expires_at = now() + (v_months || ' months')::interval;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_retention_notif_log ON notification_logs;
CREATE TRIGGER trg_retention_notif_log BEFORE INSERT ON notification_logs FOR EACH ROW EXECUTE FUNCTION set_retention_expires_at('notification_log');

DROP TRIGGER IF EXISTS trg_retention_webhook ON webhook_events;
CREATE TRIGGER trg_retention_webhook BEFORE INSERT ON webhook_events FOR EACH ROW EXECUTE FUNCTION set_retention_expires_at('webhook_payload');

-- Triggers for complaints (on close)
CREATE OR REPLACE FUNCTION set_complaint_retention() RETURNS TRIGGER AS $$
DECLARE
    v_months int;
BEGIN
    IF NEW.status = 'closed' AND (OLD.status IS DISTINCT FROM 'closed') THEN
        SELECT retention_months INTO v_months FROM retention_policies WHERE record_type = 'complaint_media' AND is_active = true;
        IF v_months IS NOT NULL THEN
            NEW.retention_expires_at = now() + (v_months || ' months')::interval;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_retention_complaint ON complaints;
CREATE TRIGGER trg_retention_complaint BEFORE UPDATE ON complaints FOR EACH ROW EXECUTE FUNCTION set_complaint_retention();

-- Triggers for ID proofs (on tenancy checkout/rejection/cancel)
-- Actually, ID proofs are on `student_profiles` directly and not linked to a specific tenancy natively unless we update it.
-- The user is considered "checked out" when they have no active tenancies. We can just run a job for that instead of a trigger.

-- ─── 4. ACCESS & ERASURE ──────────────────────────────────────────────────────

CREATE TABLE data_access_requests (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(user_id),
    request_type text NOT NULL CHECK (request_type IN ('Access', 'Erasure')),
    status text NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Fulfilled', 'Rejected')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    fulfilled_at timestamptz,
    fulfilled_by uuid REFERENCES users(user_id),
    export_file_url text,
    rejection_reason text
);

ALTER TABLE data_access_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can read own data requests" ON data_access_requests FOR SELECT USING (user_id = public.get_auth_uid());
CREATE POLICY "Users can insert own data requests" ON data_access_requests FOR INSERT WITH CHECK (user_id = public.get_auth_uid());
CREATE POLICY "Admins can read all data requests" ON data_access_requests FOR SELECT USING (public.get_auth_role() = 'admin');
CREATE POLICY "Admins can update data requests" ON data_access_requests FOR UPDATE USING (public.get_auth_role() = 'admin');

-- Erase user function
CREATE OR REPLACE FUNCTION erase_user(p_user uuid, p_admin uuid) RETURNS void AS $$
DECLARE
    v_active_tenancies int;
    v_open_holds int;
    v_open_complaints int;
BEGIN
    IF public.get_auth_role() IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'Only admins can execute erasure';
    END IF;

    -- Block conditions
    SELECT COUNT(*) INTO v_active_tenancies FROM tenancies WHERE student_id = p_user AND status IN ('active', 'notice');
    IF v_active_tenancies > 0 THEN
        RAISE EXCEPTION 'Cannot erase user with active tenancies';
    END IF;

    SELECT COUNT(*) INTO v_open_holds FROM bookings WHERE student_id = p_user AND status IN ('requested', 'approved', 'payment_pending', 'confirmed');
    IF v_open_holds > 0 THEN
        RAISE EXCEPTION 'Cannot erase user with open bookings';
    END IF;

    SELECT COUNT(*) INTO v_open_complaints FROM complaints WHERE student_id = p_user AND status NOT IN ('resolved', 'closed');
    IF v_open_complaints > 0 THEN
        RAISE EXCEPTION 'Cannot erase user with open complaints';
    END IF;

    -- Update users table
    UPDATE users SET 
        name = 'Anonymized User',
        email = 'anon_' || p_user::text || '@erased.local',
        phone = NULL,
        dob = NULL,
        gender = NULL,
        is_minor = NULL,
        status = 'Erased'
    WHERE user_id = p_user;

    -- Update student_profiles if exists
    UPDATE student_profiles SET
        college = NULL,
        course = NULL,
        guardian_name = NULL,
        guardian_phone = NULL,
        id_proof_url = NULL,
        home_city = NULL,
        emergency_contact = NULL
    WHERE student_id = p_user;

    -- Anonymize notification recipients (if any specific column has PII, else skip)
    -- Reviews - we keep them but they are anonymous now since user is anonymized.
    
    -- Note: ID/KYC storage files and auth.users disabling are handled by the backend task.
    -- (Postgres cannot delete from storage or auth.users cleanly without elevated privileges and net pg_net)
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
