import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests\test_phase9.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Fix seed_tenancy again
content = content.replace(
    "'pg', 'Add', 'City', 'manual', 'active', 1, 'monthly'",
    "'boys_hostel', 'Add', 'City', 'instant', 'Approved', 1, 'calendar'"
)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed test_phase9.py again and again")
