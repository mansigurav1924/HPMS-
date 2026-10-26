import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\routers\complaints.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Remove the broken `conn = Depends(get_db_connection)` from the middle of the args
# that my previous script added
content = re.sub(r'request:\s*Request,\s*conn\s*=\s*Depends\(get_db_connection\)', r'request: Request', content)

# Function regex
# We want to match `def something(..., something):`
# and add `, conn = Depends(get_db_connection)` right before the `):` or `)`

def replacer(match):
    func_start = match.group(1) # e.g. "def foo("
    args = match.group(2) # e.g. "request: Request, param: str"
    rest = match.group(3) # e.g. "):\n    docstring..."
    
    # Check if this function originally had `conn = get_db_connection` removed by my previous run
    # Wait, my previous run actually REMOVED `conn = get_db_connection(request)` from the body.
    # So I can't rely on finding it in the body.
    # But all endpoint functions in complaints.py that take `request: Request` need `conn`.
    
    if "request: Request" in args and "Depends(get_db_connection)" not in args:
        # insert at the end of args
        if args.endswith(','):
            new_args = args + " conn = Depends(get_db_connection)"
        else:
            new_args = args + ", conn = Depends(get_db_connection)"
    else:
        new_args = args

    return f"{func_start}{new_args}{rest}"

# Match `def funcname(` then `any args` then `):`
pattern = re.compile(r'(def\s+\w+\()(.*?)(\):)', re.DOTALL)
new_content = pattern.sub(replacer, content)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Fixed syntax errors!")
