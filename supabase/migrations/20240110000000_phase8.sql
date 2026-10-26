-- =============================================================
-- Phase 8: Complaints & Maintenance Module
-- =============================================================
-- Sections 12.11-12.13 of the HPMS Documentation
-- =============================================================

-- ─── 1. ENUMS ───────────────────────────────────────────────

-- Complaint category enum
DO $$ BEGIN
    CREATE TYPE complaint_category AS ENUM (
        'Electrical', 'Plumbing', 'Cleaning', 'WiFi',
        'Furniture', 'Food', 'Security', 'Noise_Roommate', 'Other'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Complaint priority enum
DO $$ BEGIN
    CREATE TYPE complaint_priority AS ENUM ('low', 'medium', 'high', 'urgent');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Complaint status enum
DO $$ BEGIN
    CREATE TYPE complaint_status AS ENUM (
        'open', 'acknowledged', 'in_progress', 'resolved', 'closed', 'reopened'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ─── 2. COMPLETE THE complaints TABLE ───────────────────────
-- The init migration already created a basic complaints table.
-- We add missing columns required by Phase 8.

-- Cast existing text columns to enum (safe; values must match)
ALTER TABLE complaints
    ALTER COLUMN priority TYPE complaint_priority USING priority::complaint_priority;

ALTER TABLE complaints
    ALTER COLUMN status  TYPE complaint_status  USING status::complaint_status;

ALTER TABLE complaints
    ALTER COLUMN category TYPE complaint_category USING category::complaint_category;

-- Add missing columns
ALTER TABLE complaints
    ADD COLUMN IF NOT EXISTS acknowledged_at    timestamptz,
    ADD COLUMN IF NOT EXISTS reopened_count     int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS escalation_level   int NOT NULL DEFAULT 0,  -- 0=none,1=warden→owner,2=owner→admin
    ADD COLUMN IF NOT EXISTS escalated_at       timestamptz;

-- Set NOT NULL defaults for priority and status after enum conversion
ALTER TABLE complaints
    ALTER COLUMN priority SET DEFAULT 'medium',
    ALTER COLUMN status   SET DEFAULT 'open';

-- ─── 3. COMPLETE complaint_updates TABLE ────────────────────
-- visibility: 'public' (visible to student) or 'internal' (owner/warden only)
ALTER TABLE complaint_updates
    ADD CONSTRAINT chk_cu_visibility CHECK (visibility IN ('public', 'internal'));

-- ─── 4. COMPLETE maintenance_tasks TABLE ────────────────────
-- Add missing vendor field as a text column (vendor name / company)
ALTER TABLE maintenance_tasks
    ADD COLUMN IF NOT EXISTS vendor_name text;

-- Enum-ify status column (text → task_status)
DO $$ BEGIN
    CREATE TYPE task_status AS ENUM ('pending', 'in_progress', 'done', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE maintenance_tasks
    ALTER COLUMN status TYPE task_status USING status::task_status;

ALTER TABLE maintenance_tasks
    ALTER COLUMN status SET DEFAULT 'pending';

-- ─── 5. SLA SETTINGS on properties ─────────────────────────
-- Store per-property SLA config as jsonb (no extra table needed).
-- Schema:
--   { "ack_sla_hours": 24,
--     "resolve_sla_hours": {"urgent":24,"high":48,"medium":72,"low":120},
--     "reopen_window_days": 7,
--     "escalation_l1_multiplier": 1.0,   -- breach at 1× SLA → level 1
--     "escalation_l2_multiplier": 2.0    -- breach at 2× SLA → level 2
--   }
ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS complaint_sla jsonb NOT NULL DEFAULT '{
        "ack_sla_hours":24,
        "resolve_sla_hours":{"urgent":24,"high":48,"medium":72,"low":120},
        "reopen_window_days":7,
        "escalation_l1_multiplier":1.0,
        "escalation_l2_multiplier":2.0
    }'::jsonb;

-- ─── 6. INDEXES ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_complaints_property   ON complaints(property_id);
CREATE INDEX IF NOT EXISTS idx_complaints_status     ON complaints(status);
CREATE INDEX IF NOT EXISTS idx_complaints_student    ON complaints(student_id);
CREATE INDEX IF NOT EXISTS idx_complaints_escalated  ON complaints(escalated) WHERE escalated;
CREATE INDEX IF NOT EXISTS idx_cu_complaint          ON complaint_updates(complaint_id);
CREATE INDEX IF NOT EXISTS idx_mtasks_property       ON maintenance_tasks(property_id);
CREATE INDEX IF NOT EXISTS idx_mtasks_status         ON maintenance_tasks(status);

-- ─── 7. TRIGGER: sync bed status on maintenance_tasks ───────
-- The init trigger only fires on bookings/tenancies. We extend it to also
-- fire when a maintenance_task is inserted/updated so that blocks_availability
-- immediately puts the bed into 'maintenance' and 'done'/'cancelled' releases it.

-- Re-use the existing sync_bed_status() function.
-- Guard: only fire when bed_id IS NOT NULL.

CREATE OR REPLACE FUNCTION sync_bed_status_from_task()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Only apply to bed-level tasks
    IF NEW.bed_id IS NULL THEN
        RETURN NEW;
    END IF;

    UPDATE beds SET status = CASE
        WHEN EXISTS (
            SELECT 1 FROM tenancies t
            WHERE t.bed_id = NEW.bed_id AND t.status IN ('active','notice')
        ) THEN 'occupied'
        WHEN EXISTS (
            SELECT 1 FROM bookings b
            WHERE b.bed_id = NEW.bed_id
              AND b.status IN ('requested','approved','payment_pending','confirmed')
        ) THEN 'reserved'
        WHEN EXISTS (
            SELECT 1 FROM maintenance_tasks m
            WHERE m.bed_id = NEW.bed_id
              AND m.blocks_availability = true
              AND m.status NOT IN ('done', 'cancelled')
        ) THEN 'maintenance'
        ELSE 'available'
    END
    WHERE bed_id = NEW.bed_id;

    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_mtasks_sync ON maintenance_tasks;
CREATE TRIGGER trg_mtasks_sync
AFTER INSERT OR UPDATE OF status, blocks_availability
ON maintenance_tasks
FOR EACH ROW EXECUTE FUNCTION sync_bed_status_from_task();

-- ─── 8. FUNCTION: block_bed_for_maintenance(task_id) ────────
-- Called after inserting a task with blocks_availability=true.
-- Returns 409 BED_IN_USE if the bed is occupied or has an active booking.

CREATE OR REPLACE FUNCTION validate_maintenance_block(p_task uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_task maintenance_tasks%rowtype;
BEGIN
    SELECT * INTO v_task FROM maintenance_tasks WHERE task_id = p_task;
    IF NOT FOUND THEN RAISE EXCEPTION 'TASK_NOT_FOUND'; END IF;

    -- Only validate bed-level tasks
    IF v_task.bed_id IS NULL OR NOT v_task.blocks_availability THEN
        RETURN;
    END IF;

    -- Reject if bed is occupied by an active tenancy
    IF EXISTS (
        SELECT 1 FROM tenancies t
        WHERE t.bed_id = v_task.bed_id AND t.status IN ('active', 'notice')
    ) THEN
        RAISE EXCEPTION 'BED_IN_USE: bed has active tenancy';
    END IF;

    -- Reject if bed has an active booking
    IF EXISTS (
        SELECT 1 FROM bookings b
        WHERE b.bed_id = v_task.bed_id
          AND b.status IN ('requested', 'approved', 'payment_pending', 'confirmed')
    ) THEN
        RAISE EXCEPTION 'BED_IN_USE: bed has active booking';
    END IF;
END $$;

-- ─── 9. FUNCTION: raise_complaint ───────────────────────────
-- Student must have an active or notice tenancy on any of the property's beds.
-- Complaint is linked to that tenancy and the property.

CREATE OR REPLACE FUNCTION raise_complaint(
    p_student     uuid,
    p_category    complaint_category,
    p_description text,
    p_priority    complaint_priority,
    p_photos      jsonb DEFAULT NULL
) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    v_tenancy tenancies%rowtype;
    v_id      uuid;
BEGIN
    -- Find the student's current active/notice tenancy
    SELECT * INTO v_tenancy
    FROM tenancies
    WHERE student_id = p_student
      AND status IN ('active', 'notice')
    ORDER BY check_in_date DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'NO_ACTIVE_TENANCY: student has no active or notice tenancy';
    END IF;

    INSERT INTO complaints (
        tenancy_id, student_id, property_id,
        category, description, priority, status, photos
    ) VALUES (
        v_tenancy.tenancy_id, p_student, v_tenancy.property_id,
        p_category, p_description, p_priority, 'open', p_photos
    ) RETURNING complaint_id INTO v_id;

    -- Seed the first complaint_update (status_change: open)
    INSERT INTO complaint_updates (complaint_id, author_id, visibility, message, status_change)
    VALUES (v_id, p_student, 'public', 'Complaint raised.', 'open');

    RETURN v_id;
END $$;

-- ─── 10. FUNCTION: change_complaint_status ──────────────────
-- Enforces all allowed transitions.
-- Sets timestamps atomically.
-- Inserts a complaint_updates row for every status change.
-- Resolve requires resolution_notes.

CREATE OR REPLACE FUNCTION change_complaint_status(
    p_complaint uuid,
    p_actor     uuid,
    p_new_status complaint_status,
    p_note      text DEFAULT NULL
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_c            complaints%rowtype;
    v_prop         properties%rowtype;
    v_reopen_days  int;
    v_allowed      boolean := false;
BEGIN
    SELECT * INTO v_c FROM complaints WHERE complaint_id = p_complaint FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'COMPLAINT_NOT_FOUND'; END IF;

    SELECT * INTO v_prop FROM properties WHERE property_id = v_c.property_id;
    v_reopen_days := COALESCE(
        (v_prop.complaint_sla->>'reopen_window_days')::int, 7
    );

    -- ── Validate transitions ─────────────────────────────────
    CASE v_c.status::text
        WHEN 'open' THEN
            v_allowed := p_new_status::text = 'acknowledged';

        WHEN 'acknowledged' THEN
            v_allowed := p_new_status::text = 'in_progress';

        WHEN 'in_progress' THEN
            v_allowed := p_new_status::text IN ('resolved', 'closed');

        WHEN 'resolved' THEN
            -- Student can reopen within window; owner/warden can close
            IF p_new_status::text = 'reopened' THEN
                -- Only student can reopen
                IF v_c.student_id != p_actor THEN
                    RAISE EXCEPTION 'INVALID_TRANSITION: only student can reopen';
                END IF;
                -- Must be within reopen window
                IF v_c.resolved_at IS NULL OR
                   now() > v_c.resolved_at + (v_reopen_days || ' days')::interval THEN
                    RAISE EXCEPTION 'REOPEN_WINDOW_EXPIRED: reopen window of % days has passed', v_reopen_days;
                END IF;
                v_allowed := true;
            ELSIF p_new_status::text = 'closed' THEN
                v_allowed := true;
            END IF;

        WHEN 'closed' THEN
            v_allowed := false;  -- terminal state

        WHEN 'reopened' THEN
            v_allowed := p_new_status::text IN ('in_progress', 'acknowledged');

        ELSE
            v_allowed := false;
    END CASE;

    IF NOT v_allowed THEN
        RAISE EXCEPTION 'INVALID_TRANSITION: % → %', v_c.status, p_new_status;
    END IF;

    -- Resolve requires resolution_notes
    IF p_new_status::text = 'resolved' AND (p_note IS NULL OR p_note = '') THEN
        RAISE EXCEPTION 'RESOLUTION_NOTES_REQUIRED';
    END IF;

    -- ── Apply the transition ─────────────────────────────────
    UPDATE complaints SET
        status             = p_new_status,
        -- Set timestamps only here (never in application layer)
        acknowledged_at    = CASE WHEN p_new_status::text = 'acknowledged'
                                  THEN now() ELSE acknowledged_at END,
        resolved_at        = CASE WHEN p_new_status::text = 'resolved'
                                  THEN now()
                                  WHEN p_new_status::text = 'reopened'
                                  THEN NULL           -- clear on reopen
                                  ELSE resolved_at END,
        closed_at          = CASE WHEN p_new_status::text = 'closed'
                                  THEN now() ELSE closed_at END,
        resolution_notes   = CASE WHEN p_new_status::text = 'resolved' AND p_note IS NOT NULL
                                  THEN p_note ELSE resolution_notes END,
        reopened_count     = CASE WHEN p_new_status::text = 'reopened'
                                  THEN reopened_count + 1 ELSE reopened_count END
    WHERE complaint_id = p_complaint;

    -- ── Append update thread entry ───────────────────────────
    INSERT INTO complaint_updates (complaint_id, author_id, visibility, message, status_change)
    VALUES (
        p_complaint,
        p_actor,
        'public',  -- status-change entries are always public
        COALESCE(p_note, 'Status changed to ' || p_new_status::text),
        p_new_status::text
    );
END $$;

-- ─── 11. FUNCTION: rate_complaint ───────────────────────────
-- Only the complaint's own student, only when closed or resolved, 1-5, once.

CREATE OR REPLACE FUNCTION rate_complaint(
    p_complaint uuid,
    p_student   uuid,
    p_rating    int
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_c complaints%rowtype;
BEGIN
    SELECT * INTO v_c FROM complaints WHERE complaint_id = p_complaint FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'COMPLAINT_NOT_FOUND'; END IF;

    IF v_c.student_id != p_student THEN
        RAISE EXCEPTION 'UNAUTHORIZED: only the complaint owner can rate';
    END IF;

    IF v_c.status::text NOT IN ('resolved', 'closed') THEN
        RAISE EXCEPTION 'INVALID_STATUS: can only rate resolved or closed complaints';
    END IF;

    IF v_c.student_rating IS NOT NULL THEN
        RAISE EXCEPTION 'ALREADY_RATED: this complaint has already been rated';
    END IF;

    IF p_rating < 1 OR p_rating > 5 THEN
        RAISE EXCEPTION 'INVALID_RATING: rating must be between 1 and 5';
    END IF;

    UPDATE complaints SET student_rating = p_rating WHERE complaint_id = p_complaint;
END $$;

-- ─── 12. FUNCTION: escalate_overdue_complaints ──────────────
-- Idempotent: runs every 30 min via Celery beat.
-- Level 1: ack or resolve SLA breached once  → notify warden/owner
-- Level 2: still unresolved at 2× SLA breach → notify admin
-- Never decrements escalation_level; never sends duplicate notifications
-- for the same level. Returns a JSONB summary for logging.

CREATE OR REPLACE FUNCTION escalate_overdue_complaints()
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    v_rec       RECORD;
    v_sla       jsonb;
    v_ack_hours int;
    v_res_hours int;
    v_l1_mult   numeric;
    v_l2_mult   numeric;
    v_l1_count  int := 0;
    v_l2_count  int := 0;
BEGIN
    FOR v_rec IN
        SELECT
            c.complaint_id, c.status, c.priority, c.escalation_level,
            c.escalated, c.created_at, c.acknowledged_at, c.property_id,
            c.student_id, c.assigned_to,
            p.complaint_sla,
            p.owner_id
        FROM complaints c
        JOIN properties p ON p.property_id = c.property_id
        WHERE c.status NOT IN ('closed', 'resolved')
    LOOP
        v_sla       := v_rec.complaint_sla;
        v_ack_hours := COALESCE((v_sla->>'ack_sla_hours')::int, 24);
        v_res_hours := COALESCE(
            (v_sla->'resolve_sla_hours'->>v_rec.priority::text)::int,
            CASE v_rec.priority::text
                WHEN 'urgent' THEN 24
                WHEN 'high'   THEN 48
                WHEN 'medium' THEN 72
                ELSE 120
            END
        );
        v_l1_mult := COALESCE((v_sla->>'escalation_l1_multiplier')::numeric, 1.0);
        v_l2_mult := COALESCE((v_sla->>'escalation_l2_multiplier')::numeric, 2.0);

        -- Level 1: still open/acknowledged past v_ack_hours (or unresolved past v_res_hours × l1)
        IF v_rec.escalation_level < 1 THEN
            IF (
                v_rec.status::text = 'open'
                AND now() > v_rec.created_at + (v_ack_hours * v_l1_mult || ' hours')::interval
            ) OR (
                v_rec.status::text IN ('acknowledged', 'in_progress', 'reopened')
                AND now() > v_rec.created_at + (v_res_hours * v_l1_mult || ' hours')::interval
            ) THEN
                UPDATE complaints SET
                    escalated        = true,
                    escalation_level = 1,
                    escalated_at     = now()
                WHERE complaint_id = v_rec.complaint_id;
                v_l1_count := v_l1_count + 1;
            END IF;

        -- Level 2: still unresolved at 2× the resolve SLA
        ELSIF v_rec.escalation_level < 2 THEN
            IF now() > v_rec.created_at + (v_res_hours * v_l2_mult || ' hours')::interval THEN
                UPDATE complaints SET
                    escalation_level = 2,
                    escalated_at     = now()
                WHERE complaint_id = v_rec.complaint_id;
                v_l2_count := v_l2_count + 1;
            END IF;
        END IF;
    END LOOP;

    RETURN jsonb_build_object('l1_escalated', v_l1_count, 'l2_escalated', v_l2_count);
END $$;

-- ─── 13. FUNCTION: auto_close_resolved_complaints ───────────
-- Closes complaints that have been resolved for longer than reopen_window_days
-- and have not been reopened. Idempotent.

CREATE OR REPLACE FUNCTION auto_close_resolved_complaints()
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
    v_count int := 0;
    v_rec   RECORD;
    v_days  int;
BEGIN
    FOR v_rec IN
        SELECT c.complaint_id, c.resolved_at, c.reopened_count,
               p.complaint_sla
        FROM complaints c
        JOIN properties p ON p.property_id = c.property_id
        WHERE c.status::text = 'resolved'
          AND c.resolved_at IS NOT NULL
    LOOP
        v_days := COALESCE(
            (v_rec.complaint_sla->>'reopen_window_days')::int, 7
        );
        IF now() > v_rec.resolved_at + (v_days || ' days')::interval THEN
            UPDATE complaints SET
                status    = 'closed',
                closed_at = now()
            WHERE complaint_id = v_rec.complaint_id;

            INSERT INTO complaint_updates (complaint_id, author_id, visibility, message, status_change)
            SELECT v_rec.complaint_id, student_id, 'public',
                   'Auto-closed: reopen window expired.', 'closed'
            FROM complaints WHERE complaint_id = v_rec.complaint_id;

            v_count := v_count + 1;
        END IF;
    END LOOP;
    RETURN v_count;
END $$;

-- ─── 14. RLS for property_staff, complaint_updates, maintenance_tasks ───

-- Allow staff to see their own property_staff records
ALTER TABLE property_staff ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Staff see own assignments" ON property_staff;
CREATE POLICY "Staff see own assignments" ON property_staff
    FOR SELECT USING (user_id = public.get_auth_uid());

-- Students can only see PUBLIC updates on their own complaints.
-- Owners see all updates (public + internal) for their property's complaints.
-- Wardens see all updates for their assigned property's complaints.
-- Admin sees all.

-- Ensure RLS is enabled (init already did this, but belt-and-suspenders)
ALTER TABLE complaint_updates ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Admin all complaint_updates" ON complaint_updates;
CREATE POLICY "Admin all complaint_updates" ON complaint_updates
    FOR ALL USING (public.get_auth_role() = 'admin');

DROP POLICY IF EXISTS "Student own public updates" ON complaint_updates;
CREATE POLICY "Student own public updates" ON complaint_updates
    FOR SELECT USING (
        public.get_auth_role() = 'student'
        AND visibility = 'public'
        AND EXISTS (
            SELECT 1 FROM complaints c
            WHERE c.complaint_id = complaint_updates.complaint_id
              AND c.student_id = public.get_auth_uid()
        )
    );

-- Students can INSERT public updates (comments) on their own complaints
DROP POLICY IF EXISTS "Student insert own public updates" ON complaint_updates;
CREATE POLICY "Student insert own public updates" ON complaint_updates
    FOR INSERT WITH CHECK (
        public.get_auth_role() = 'student'
        AND visibility = 'public'
        AND author_id = public.get_auth_uid()
        AND EXISTS (
            SELECT 1 FROM complaints c
            WHERE c.complaint_id = complaint_updates.complaint_id
              AND c.student_id = public.get_auth_uid()
        )
    );

DROP POLICY IF EXISTS "Owner all updates own property" ON complaint_updates;
CREATE POLICY "Owner all updates own property" ON complaint_updates
    FOR ALL USING (
        public.get_auth_role() = 'owner'
        AND EXISTS (
            SELECT 1 FROM complaints c
            JOIN properties p ON p.property_id = c.property_id
            WHERE c.complaint_id = complaint_updates.complaint_id
              AND p.owner_id = public.get_auth_uid()
        )
    );

DROP POLICY IF EXISTS "Warden all updates assigned" ON complaint_updates;
CREATE POLICY "Warden all updates assigned" ON complaint_updates
    FOR ALL USING (
        public.get_auth_role() = 'warden'
        AND EXISTS (
            SELECT 1 FROM complaints c
            JOIN property_staff ps ON ps.property_id = c.property_id
            WHERE c.complaint_id = complaint_updates.complaint_id
              AND ps.user_id = public.get_auth_uid()
        )
    );

-- RLS for maintenance_tasks: owner sees costs; warden sees tasks but NOT cost
-- (Cost is filtered in the API layer; RLS just scopes property access)
ALTER TABLE maintenance_tasks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Admin all maintenance_tasks" ON maintenance_tasks;
CREATE POLICY "Admin all maintenance_tasks" ON maintenance_tasks
    FOR ALL USING (public.get_auth_role() = 'admin');

DROP POLICY IF EXISTS "Owner maintenance tasks own property" ON maintenance_tasks;
CREATE POLICY "Owner maintenance tasks own property" ON maintenance_tasks
    FOR ALL USING (
        public.get_auth_role() = 'owner'
        AND EXISTS (
            SELECT 1 FROM properties p
            WHERE p.property_id = maintenance_tasks.property_id
              AND p.owner_id = public.get_auth_uid()
        )
    );

DROP POLICY IF EXISTS "Warden maintenance tasks assigned" ON maintenance_tasks;
CREATE POLICY "Warden maintenance tasks assigned" ON maintenance_tasks
    FOR ALL USING (
        public.get_auth_role() = 'warden'
        AND EXISTS (
            SELECT 1 FROM property_staff ps
            WHERE ps.property_id = maintenance_tasks.property_id
              AND ps.user_id = public.get_auth_uid()
        )
    );

-- ─── 15. NOTIFICATION TEMPLATES (seed) ──────────────────────
-- Idempotent: use ON CONFLICT DO NOTHING

INSERT INTO notification_templates (template_id, name, channel, version, body, is_active)
VALUES
-- Complaint received by student
('complaint_received_student',
 'Complaint Received',
 'email', 1,
 'Hi {{tenant_name}}, your complaint ({{category}}) has been received. Complaint ID: {{complaint_id}}. We will get back to you shortly.',
 true),

-- Complaint received by owner/warden
('complaint_received_owner',
 'New Complaint at Your Property',
 'email', 1,
 'Hi {{owner_name}}, a new complaint has been raised at {{property_name}} by tenant {{tenant_name}}. Category: {{category}}. Priority: {{priority}}. Please review and acknowledge within your SLA.',
 true),

-- Status update to student
('complaint_status_update',
 'Complaint Status Updated',
 'email', 1,
 'Hi {{tenant_name}}, your complaint (ID: {{complaint_id}}) status has changed to: {{new_status}}. {{note}}',
 true),

-- Complaint resolved (with rating prompt)
('complaint_resolved',
 'Complaint Resolved',
 'email', 1,
 'Hi {{tenant_name}}, your complaint (ID: {{complaint_id}}) has been resolved. Resolution: {{resolution_notes}}. Please rate your experience at: {{rating_url}}. You can also reopen within {{reopen_window_days}} days if unsatisfied.',
 true),

-- Escalation to owner (level 1)
('complaint_escalated_owner',
 'Complaint Escalated to You',
 'email', 1,
 'Hi {{owner_name}}, complaint ID {{complaint_id}} at {{property_name}} has breached its SLA ({{priority}} priority). Please take immediate action.',
 true),

-- Escalation to admin (level 2)
('complaint_escalated_admin',
 'Complaint Escalated to Admin',
 'email', 1,
 'Admin alert: complaint ID {{complaint_id}} at property {{property_name}} has been escalated to level 2 (SLA 2× breached). Priority: {{priority}}.',
 true),

-- Complaint reopened
('complaint_reopened',
 'Complaint Reopened',
 'email', 1,
 'Hi {{owner_name}}, tenant {{tenant_name}} has reopened complaint ID {{complaint_id}} at {{property_name}}. Reason: {{note}}.',
 true),

-- SLA reminder to assignee
('complaint_sla_reminder',
 'SLA Breach Warning',
 'email', 1,
 'Hi {{assignee_name}}, complaint ID {{complaint_id}} ({{priority}} priority) is approaching its SLA deadline. Please take action now.',
 true)

ON CONFLICT (template_id) DO NOTHING;
