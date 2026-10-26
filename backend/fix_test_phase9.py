import re
import os

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\tests\test_phase9.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace(r'\"\"\"', '\"\"\"')

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed syntax in test_phase9.py")
