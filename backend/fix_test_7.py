import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests\test_phase9.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Fix seed_tenancy bookings
content = content.replace(
    "INSERT INTO bookings (booking_id, property_id, student_id, bed_id, status, move_in_date, move_out_date, rent_amount)\n            VALUES (%s, %s, %s, %s, 'confirmed', current_date - interval '10 days', current_date + interval '6 months', 1000)",
    "INSERT INTO bookings (booking_id, property_id, student_id, room_id, bed_id, status, move_in_date, rent_amount)\n            VALUES (%s, %s, %s, %s, %s, 'confirmed', current_date - interval '10 days', 1000)"
)
content = content.replace(
    "(booking_id, prop_id, student_id, bed_id)",
    "(booking_id, prop_id, student_id, room_id, bed_id)"
)

# Fix test_reviews_avg_rating_update booking insert
content = content.replace(
    "INSERT INTO bookings (booking_id, property_id, student_id, bed_id, status, move_in_date, move_out_date, rent_amount)\n            VALUES (%s, %s, %s, %s, 'confirmed', current_date, current_date, 1000)",
    "INSERT INTO bookings (booking_id, property_id, student_id, bed_id, status, move_in_date, rent_amount)\n            VALUES (%s, %s, %s, %s, 'confirmed', current_date, 1000)"
)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed test_phase9.py again and again and again and again")
