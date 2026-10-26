from fastapi import APIRouter, Depends, HTTPException, Query, Request
import psycopg
from app.db.session import get_db_connection, require_role, get_uid
from pydantic import BaseModel
import uuid

router = APIRouter(prefix="/data-access-requests", tags=["data_access"])

class DataAccessRequestCreate(BaseModel):
    request_type: str

class DataAccessRequestReject(BaseModel):
    rejection_reason: str

@router.post("")
def create_data_access_request(
    request: Request,
    req: DataAccessRequestCreate,
    db: psycopg.Connection = Depends(get_db_connection)
):
    if req.request_type not in ("Access", "Erasure"):
        raise HTTPException(status_code=422, detail="Invalid request type")
        
    claims = require_role(request, "student", "owner", "admin")
    uid = get_uid(claims)
    with db.cursor() as cur:
        # Check if one already open
        cur.execute("""
            SELECT request_id FROM data_access_requests 
            WHERE user_id = %s AND request_type = %s AND status = 'Pending'
        """, (uid, req.request_type))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Duplicate open request")
            
        cur.execute("""
            INSERT INTO data_access_requests (user_id, request_type)
            VALUES (%s, %s) RETURNING request_id
        """, (uid, req.request_type))
        req_id = cur.fetchone()[0]
    db.commit()
    return {"request_id": req_id}

@router.get("")
def list_data_access_requests(
    request: Request,
    db: psycopg.Connection = Depends(get_db_connection)
):
    claims = require_role(request, "student", "owner", "admin")
    uid = get_uid(claims)
    role = claims.get("role") or claims.get("user_metadata", {}).get("role") or claims.get("app_metadata", {}).get("role")
    with db.cursor() as cur:
        if role == "admin":
            cur.execute("SELECT * FROM data_access_requests ORDER BY requested_at DESC")
        else:
            cur.execute("SELECT * FROM data_access_requests WHERE user_id = %s ORDER BY requested_at DESC", (uid,))
            
        columns = [desc[0] for desc in cur.description]
        results = [dict(zip(columns, row)) for row in cur.fetchall()]
        return {"data": results}

@router.post("/{request_id}/fulfill")
def fulfill_data_access_request(
    request: Request,
    request_id: str,
    db: psycopg.Connection = Depends(get_db_connection)
):
    claims = require_role(request, "admin")
    uid = get_uid(claims)
    with db.cursor() as cur:
        cur.execute("SELECT user_id, request_type, status FROM data_access_requests WHERE request_id = %s", (request_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
            
        user_id, req_type, status = row
        if status != "Pending":
            raise HTTPException(status_code=400, detail="Not pending")
            
        if req_type == "Erasure":
            # invoke erase_user
            try:
                cur.execute("SELECT erase_user(%s, %s)", (user_id, uid))
            except Exception as e:
                db.rollback()
                raise HTTPException(status_code=400, detail=str(e))
                
        # update request
        cur.execute("""
            UPDATE data_access_requests 
            SET status = 'Fulfilled', fulfilled_at = now(), fulfilled_by = %s, export_file_url = %s
            WHERE request_id = %s
        """, (uid, "https://storage.supabase.com/mock" if req_type == 'Access' else None, request_id))
    db.commit()
    return {"status": "success"}

@router.post("/{request_id}/reject")
def reject_data_access_request(
    request: Request,
    request_id: str,
    req: DataAccessRequestReject,
    db: psycopg.Connection = Depends(get_db_connection)
):
    claims = require_role(request, "admin")
    uid = get_uid(claims)
    with db.cursor() as cur:
        cur.execute("""
            UPDATE data_access_requests 
            SET status = 'Rejected', rejection_reason = %s, fulfilled_at = now(), fulfilled_by = %s
            WHERE request_id = %s AND status = 'Pending'
        """, (req.rejection_reason, uid, request_id))
    db.commit()
    return {"status": "success"}
