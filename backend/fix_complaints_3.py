import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\routers\complaints.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# I will fix the invalid syntax `, conn = Depends...):`
content = content.replace(", conn = Depends(get_db_connection)):", "):")
content = content.replace("conn = Depends(get_db_connection)):", "):")
content = content.replace("conn: psycopg.Connection = Depends(get_db_connection)", "")
content = content.replace("request: Request, ,", "request: Request,")
content = content.replace("request: Request,", "request: Request")

# Restore everything to normal first:
content = re.sub(r'request:\s*Request\s*,\s*conn\s*=\s*Depends\(get_db_connection\)', r'request: Request', content)
content = re.sub(r'request:\s*Request\s*,\s*conn:\s*psycopg\.Connection\s*=\s*Depends\(get_db_connection\)', r'request: Request', content)

# Remove stray "conn = Depends(get_db_connection)"
content = content.replace(", conn = Depends(get_db_connection)", "")

# Now I will carefully insert the dependency at the end of every function arg list that has `request: Request`
def fix_args(match):
    start = match.group(1)
    args = match.group(2)
    end = match.group(3)
    
    if "request: Request" in args or "request:     Request" in args:
        # Some args have a trailing comma before the parenthesis
        args = args.rstrip()
        if args.endswith(','):
            args += " conn: psycopg.Connection = Depends(get_db_connection)"
        else:
            args += ", conn: psycopg.Connection = Depends(get_db_connection)"
            
    return f"{start}{args}{end}"

pattern = re.compile(r'(def\s+\w+\()(.*?)(\):)', re.DOTALL)
content = pattern.sub(fix_args, content)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed syntax again")
