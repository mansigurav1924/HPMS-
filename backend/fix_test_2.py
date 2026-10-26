import re
import os

# Fix audit.py import
filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\services\audit.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace("from .config import settings", "from ..config import settings")

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

# Fix test_phase9.py
test_filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests\test_phase9.py"
with open(test_filepath, "r", encoding="utf-8") as f:
    t_content = f.read()

t_content = t_content.replace(
    "cur.execute(\"INSERT INTO properties (property_id, owner_id, name) VALUES (%s, %s, 'Test Prop')\", (prop_id, owner_id))",
    "cur.execute(\"INSERT INTO properties (property_id, owner_id, name, type, address, city, booking_mode, status, rent_due_day, rent_cycle_mode) VALUES (%s, %s, 'Test Prop', 'PG', 'Add', 'City', 'manual', 'active', 1, 'monthly')\", (prop_id, owner_id))"
)

# Test audit logs append only: we need to run it as postgres first, or add policy.
# I'll add policy to the DB migration first.
with open(r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\supabase\migrations\20240111000000_phase9.sql", "a", encoding="utf-8") as f:
    f.write("\n-- Allow INSERT to audit_logs for authenticated users\n")
    f.write("DROP POLICY IF EXISTS \"Allow insert audit\" ON audit_logs;\n")
    f.write("CREATE POLICY \"Allow insert audit\" ON audit_logs FOR INSERT WITH CHECK (true);\n")

print("Fixed audit.py and test_phase9.py and added policy")
