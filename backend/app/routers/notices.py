from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from psycopg.rows import dict_row
import bleach
import logging
from ..db.session import get_db_connection, require_role, get_uid
from ..services.audit import log_action
from ..services.tasks import send_notice_fanout

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notices"])

class CreateNoticeRequest(BaseModel):
    property_id: str
    title: str = Field(..., max_length=100)
    body: str = Field(..., max_length=2000)
    expires_at: Optional[datetime] = None

class UpdateNoticeRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=100)
    body: Optional[str] = Field(None, max_length=2000)
    expires_at: Optional[datetime] = None

def _check_notice_access(conn, notice_id: str, uid: str, role: str) -> dict:
    if role == "admin":
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM notices WHERE notice_id = %s", (notice_id,))
            return cur.fetchone()

    with conn.cursor(row_factory=dict_row) as cur:
        # For owner/warden, check property access
        if role in ("owner", "warden"):
            cur.execute("""
                SELECT n.* FROM notices n
                LEFT JOIN properties p ON p.property_id = n.property_id AND p.owner_id = %s
                LEFT JOIN property_staff ps ON ps.property_id = n.property_id AND ps.user_id = %s
                WHERE n.notice_id = %s AND (p.property_id IS NOT NULL OR ps.property_id IS NOT NULL)
            """, (uid, uid, notice_id))
            return cur.fetchone()
        
        # For student, check active/notice tenancy at property
        if role == "student":
            cur.execute("""
                SELECT n.* FROM notices n
                JOIN tenancies t ON t.property_id = n.property_id
                JOIN bookings b ON t.booking_id = b.booking_id
                WHERE n.notice_id = %s 
                  AND b.student_id = %s 
                  AND t.status IN ('active', 'notice')
                  AND (n.expires_at IS NULL OR n.expires_at > now())
            """, (notice_id, uid))
            return cur.fetchone()
            
    return None

@router.post("/notices", status_code=201)
def create_notice(
    request: Request,
    data: CreateNoticeRequest,
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "owner", "warden", "admin")
    uid = get_uid(claims)
    role = claims.get("role")
    
    # Sanitize body
    safe_body = bleach.clean(data.body, tags=[], strip=True)
    if not safe_body:
        raise HTTPException(400, "Body is empty after sanitization")

    with conn.cursor(row_factory=dict_row) as cur:
        # Verify ownership
        if role != "admin":
            cur.execute("""
                SELECT 1 FROM properties p 
                LEFT JOIN property_staff ps ON ps.property_id = p.property_id AND ps.user_id = %s
                WHERE p.property_id = %s AND (p.owner_id = %s OR ps.property_id IS NOT NULL)
            """, (uid, data.property_id, uid))
            if not cur.fetchone():
                raise HTTPException(403, "Access denied to property")

        cur.execute("""
            INSERT INTO notices (property_id, title, body, posted_by, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (data.property_id, data.title, safe_body, uid, data.expires_at))
        notice = cur.fetchone()
        
        log_action(conn, uid, role, "create", "notice", notice["notice_id"], None, {"title": notice["title"]})
        
        # Trigger Celery fan-out task
        send_notice_fanout.delay(
            notice_id=str(notice["notice_id"]), 
            property_id=data.property_id,
            title=data.title, 
            body=safe_body
        )
        return notice

@router.get("/notices")
def get_notices(
    request: Request,
    property_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid = get_uid(claims)
    role = claims.get("role")
    
    with conn.cursor(row_factory=dict_row) as cur:
        conditions = []
        params = []
        
        base_select = """
            SELECT n.*, u.name as posted_by_name,
                   EXISTS(SELECT 1 FROM notice_reads nr WHERE nr.notice_id = n.notice_id AND nr.user_id = %s) as is_read
            FROM notices n
            JOIN users u ON u.user_id = n.posted_by
        """
        params.append(uid)
        
        if role == "student":
            conditions.append("""
                n.property_id IN (
                    SELECT t.property_id FROM tenancies t 
                    JOIN bookings b ON t.booking_id = b.booking_id 
                    WHERE b.student_id = %s AND t.status IN ('active', 'notice')
                )
            """)
            params.append(uid)
            conditions.append("(n.expires_at IS NULL OR n.expires_at > now())")
        elif role == "owner":
            conditions.append("""
                n.property_id IN (
                    SELECT property_id FROM properties WHERE owner_id = %s
                )
            """)
            params.append(uid)
        elif role == "warden":
            conditions.append("""
                n.property_id IN (
                    SELECT property_id FROM property_staff WHERE user_id = %s
                )
            """)
            params.append(uid)
            
        if property_id:
            conditions.append("n.property_id = %s")
            params.append(property_id)
            
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        
        cur.execute(f"SELECT COUNT(*) FROM notices n {where_clause}", params[1:])
        total = cur.fetchone()["count"]
        
        query = f"{base_select} {where_clause} ORDER BY n.posted_at DESC LIMIT %s OFFSET %s"
        params.extend([page_size, (page - 1) * page_size])
        
        cur.execute(query, params)
        notices = cur.fetchall()
        
        return {"notices": notices, "total": total, "page": page, "page_size": page_size}

@router.patch("/notices/{notice_id}")
def update_notice(
    request: Request,
    notice_id: str,
    data: UpdateNoticeRequest,
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "owner", "warden", "admin")
    uid = get_uid(claims)
    role = claims.get("role")
    
    notice = _check_notice_access(conn, notice_id, uid, role)
    if not notice:
        raise HTTPException(404, "Notice not found or access denied")
        
    updates = []
    params = []
    if data.title is not None:
        updates.append("title = %s")
        params.append(data.title)
    if data.body is not None:
        safe_body = bleach.clean(data.body, tags=[], strip=True)
        updates.append("body = %s")
        params.append(safe_body)
    if data.expires_at is not None:
        updates.append("expires_at = %s")
        params.append(data.expires_at)
        
    if not updates:
        return notice
        
    params.append(notice_id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"UPDATE notices SET {', '.join(updates)} WHERE notice_id = %s RETURNING *", params)
        updated = cur.fetchone()
        log_action(conn, uid, role, "update", "notice", notice_id, notice, updated)
        return updated

@router.delete("/notices/{notice_id}", status_code=204)
def delete_notice(
    request: Request,
    notice_id: str,
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "owner", "warden", "admin")
    uid = get_uid(claims)
    role = claims.get("role")
    
    notice = _check_notice_access(conn, notice_id, uid, role)
    if not notice:
        raise HTTPException(404, "Notice not found or access denied")
        
    with conn.cursor() as cur:
        cur.execute("DELETE FROM notices WHERE notice_id = %s", (notice_id,))
        log_action(conn, uid, role, "delete", "notice", notice_id, notice, None)

@router.post("/notices/{notice_id}/read", status_code=204)
def mark_notice_read(
    request: Request,
    notice_id: str,
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid = get_uid(claims)
    role = claims.get("role")
    
    notice = _check_notice_access(conn, notice_id, uid, role)
    if not notice:
        raise HTTPException(404, "Notice not found or expired")
        
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO notice_reads (notice_id, user_id) VALUES (%s, %s)
            ON CONFLICT (notice_id, user_id) DO NOTHING
        """, (notice_id, uid))
