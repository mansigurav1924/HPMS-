import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests\test_phase9.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Fix seed_tenancy tenancies
content = content.replace(
    "INSERT INTO tenancies (tenancy_id, property_id, booking_id, student_id, status, start_date)\n            VALUES (%s, %s, %s, %s, %s, current_date - interval '10 days')",
    "INSERT INTO tenancies (tenancy_id, property_id, booking_id, student_id, bed_id, status, check_in_date, expected_checkout_date, rent_amount)\n            VALUES (%s, %s, %s, %s, %s, %s, current_date - interval '10 days', current_date + interval '6 months', 1000)"
)
content = content.replace(
    "(tenancy_id, prop_id, booking_id, student_id, status)",
    "(tenancy_id, prop_id, booking_id, student_id, bed_id, status)"
)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed test_phase9.py again and again and again and again and again")
