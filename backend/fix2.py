code = """
DROP TRIGGER IF EXISTS trg_tenancies_sync ON tenancies;
CREATE TRIGGER trg_tenancies_sync AFTER INSERT OR UPDATE OF status, bed_id ON tenancies
FOR EACH ROW EXECUTE FUNCTION sync_bed_status();
"""
with open('../supabase/migrations/20240106000000_phase6.sql', 'a') as f:
    f.write(code)
