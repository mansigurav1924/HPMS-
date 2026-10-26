-- Phase 11: Security Hardening

-- Restrict audit_logs INSERT to authenticated users only
DROP POLICY IF EXISTS "Allow insert audit" ON audit_logs;
CREATE POLICY "Allow insert audit" ON audit_logs FOR INSERT WITH CHECK (auth.role() = 'authenticated');
