filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\routers\complaints.py"
with open(filepath, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    if "request: Request" in line and "request: Request," not in line:
        line = line.replace("request: Request", "request: Request,")
    new_lines.append(line)

with open(filepath, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("Fixed commas")
