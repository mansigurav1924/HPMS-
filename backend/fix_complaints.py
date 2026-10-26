import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\routers\complaints.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Add Depends to imports if not there
if "Depends" not in content[:500]:
    content = content.replace("from fastapi import APIRouter", "from fastapi import APIRouter, Depends", 1)

# Function regex
# def my_func(request: Request, ...)
#     conn   = get_db_connection(request)

import builtins

def replacer(match):
    # match.group(1) is the 'def funcname('
    # match.group(2) is the arguments ending with '):'
    # match.group(3) is the body starting with 'conn   = get_db_connection(request)'
    
    args = match.group(2)
    # We want to add `conn: psycopg.Connection = Depends(get_db_connection)` to the args
    
    body = match.group(3)
    # We remove the `conn   = get_db_connection(request)` line from the body
    
    # insert before Request or at the end
    # Actually, simpler: replace `request: Request,` with `request: Request, conn=Depends(get_db_connection),`
    
    if "request: Request" in args:
        # FastAPI might complain if Depends doesn't have type hint? No, it's fine.
        # But let's add the type hint psycopg.Connection
        new_args = args.replace("request: Request", "request: Request, conn: psycopg.Connection = Depends(get_db_connection)")
    elif "request:     Request" in args:
        new_args = args.replace("request:     Request", "request: Request, conn: psycopg.Connection = Depends(get_db_connection)")
    else:
        new_args = args
        
    new_body = re.sub(r'^[ \t]*conn\s*=\s*get_db_connection\(request\)\n', '', body, flags=re.MULTILINE)
    
    return f"{match.group(1)}{new_args}{new_body}"

# This regex matches a def, its args, and the body up to the end of the get_db_connection line
pattern = re.compile(r'(def\s+\w+\()(.*?\):)\s*("""[\s\S]*?""")?\s*conn\s*=\s*get_db_connection\(request\)\n', re.DOTALL)

def full_replacer(match):
    func_start = match.group(1)
    args = match.group(2)
    docstring = match.group(3) or ""
    
    if "request: Request" in args:
        new_args = args.replace("request: Request", "request: Request, conn = Depends(get_db_connection)")
    elif "request:     Request" in args:
        new_args = args.replace("request:     Request", "request: Request, conn = Depends(get_db_connection)")
    elif "request: \tRequest" in args:
        new_args = args.replace("request: \tRequest", "request: Request, conn = Depends(get_db_connection)")
    elif "request: \n" in args or "request:\n" in args:
         new_args = re.sub(r'request:\s*Request', r'request: Request, conn = Depends(get_db_connection)', args)
    else:
        new_args = re.sub(r'request:\s*Request', r'request: Request, conn = Depends(get_db_connection)', args)

    out = f"{func_start}{new_args}\n"
    if docstring:
        out += f"    {docstring}\n"
    
    return out


new_content = pattern.sub(full_replacer, content)

# Check if psycopg is imported
if "import psycopg" not in new_content[:500]:
    new_content = new_content.replace("from fastapi", "import psycopg\nfrom fastapi", 1)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Replaced!")
