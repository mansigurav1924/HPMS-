code = """

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
"""
with open('../supabase/migrations/20240106000000_phase6.sql', 'a') as f:
    f.write(code)
