import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests\test_phase9.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Fix users insert in test_reviews_avg_rating_update
content = content.replace(
    "cur.execute(\"INSERT INTO users (user_id, email, role) VALUES (%s, %s, 'student')\", (s2, f\"s2_{s2}@t.com\"))",
    "cur.execute(\"INSERT INTO users (user_id, email, role, name, phone) VALUES (%s, %s, 'student', 'Test', '123')\", (s2, f\"s2_{s2}@t.com\"))"
)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed test_phase9.py once and for all")
