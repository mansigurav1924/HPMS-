from fastapi import APIRouter, Depends
import psycopg
from ..db.session import get_db_connection

router = APIRouter(prefix="/users", tags=["users"])

from typing import Optional

@router.get("/me")
def get_me(conn: psycopg.Connection = Depends(get_db_connection)):
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, name, email, role, phone FROM users WHERE user_id = (select nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid)")
        row = cur.fetchone()
        if not row:
            return {"error": "User not found"}
        return {"user_id": row[0], "name": row[1], "email": row[2], "role": row[3], "phone": row[4]}

@router.put("/me")
def update_me(name: str, phone: Optional[str] = None, conn: psycopg.Connection = Depends(get_db_connection)):
    with conn.cursor() as cur:
        if phone is not None:
            cur.execute("UPDATE users SET name = %s, phone = %s WHERE user_id = (select nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid) RETURNING user_id", (name, phone))
        else:
            cur.execute("UPDATE users SET name = %s WHERE user_id = (select nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid) RETURNING user_id", (name,))
        conn.commit()
        return {"status": "updated"}
