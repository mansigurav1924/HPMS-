import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests\test_phase9.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Fix seed_tenancy rooms and beds
content = content.replace(
    "cur.execute(\"INSERT INTO rooms (room_id, property_id, room_number) VALUES (%s, %s, '101')\", (room_id, prop_id))",
    "cur.execute(\"INSERT INTO rooms (room_id, property_id, room_number, room_type, capacity, monthly_rent) VALUES (%s, %s, '101', 'shared', 2, 1000)\", (room_id, prop_id))"
)

content = content.replace(
    "cur.execute(\"INSERT INTO beds (bed_id, room_id, bed_number) VALUES (%s, %s, '101A')\", (bed_id, room_id))",
    "cur.execute(\"INSERT INTO beds (bed_id, room_id, bed_label, status) VALUES (%s, %s, '101A', 'occupied')\", (bed_id, room_id))"
)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed test_phase9.py again and again and again")
