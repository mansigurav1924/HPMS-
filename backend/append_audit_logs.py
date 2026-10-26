import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\routers\admin.py"

append_code = """
import csv
from io import StringIO
from fastapi import Query
from fastapi.responses import StreamingResponse

@router.get("/audit-logs")
def get_audit_logs(
    request: Request,
    user: Optional[str] = None,
    role: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    export: bool = False,
    conn = Depends(get_db_connection)
):
    require_role(request, "admin")
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        conds = []
        params = []
        if user:
            conds.append("user_id = %s")
            params.append(user)
        if role:
            conds.append("role = %s")
            params.append(role)
        if action:
            conds.append("action = %s")
            params.append(action)
        if entity_type:
            conds.append("entity_type = %s")
            params.append(entity_type)
        if date_from:
            conds.append("timestamp >= %s")
            params.append(date_from)
        if date_to:
            conds.append("timestamp <= %s")
            params.append(date_to)
            
        where = "WHERE " + " AND ".join(conds) if conds else ""
        
        if export:
            # CSV export capped at 5000 rows
            cur.execute(f"SELECT * FROM audit_logs {where} ORDER BY timestamp DESC LIMIT 5000", params)
            rows = cur.fetchall()
            
            output = StringIO()
            writer = csv.writer(output)
            if rows:
                writer.writerow(rows[0].keys())
                for row in rows:
                    writer.writerow(row.values())
            
            output.seek(0)
            return StreamingResponse(
                iter([output.getvalue()]), 
                media_type="text/csv", 
                headers={"Content-Disposition": "attachment; filename=audit_logs.csv"}
            )
            
        cur.execute(f"SELECT COUNT(*) FROM audit_logs {where}", params)
        total = cur.fetchone()["count"]
        
        cur.execute(f"SELECT * FROM audit_logs {where} ORDER BY timestamp DESC LIMIT %s OFFSET %s", params + [page_size, (page - 1) * page_size])
        return {"logs": cur.fetchall(), "total": total, "page": page, "page_size": page_size}
"""

with open(filepath, "a", encoding="utf-8") as f:
    f.write(append_code)

print("Appended audit-logs to admin.py")
