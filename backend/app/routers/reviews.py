from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Optional
from psycopg.rows import dict_row
from ..db.session import get_db_connection, require_role, get_uid, get_public_db_connection
from ..services.audit import log_action

router = APIRouter(tags=["reviews"])

class CreateReviewRequest(BaseModel):
    tenancy_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=1000)

class ReplyReviewRequest(BaseModel):
    owner_reply: str = Field(..., max_length=1000)

class ModerateReviewRequest(BaseModel):
    status: str = Field(..., pattern="^(Visible|Hidden)$")
    reason: str = Field(..., min_length=1)

@router.post("/reviews", status_code=201)
def create_review(
    request: Request,
    data: CreateReviewRequest,
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "student")
    uid = get_uid(claims)
    
    with conn.cursor(row_factory=dict_row) as cur:
        try:
            cur.execute(
                "SELECT create_review(%s, %s, %s, %s)",
                (uid, data.tenancy_id, data.rating, data.comment)
            )
            review_id = cur.fetchone()["create_review"]
            
            cur.execute("SELECT * FROM reviews WHERE review_id = %s", (review_id,))
            review = cur.fetchone()
            log_action(conn, uid, "student", "create", "review", str(review_id), None, {"rating": data.rating})
            return review
        except Exception as e:
            err = str(e)
            if "A review already exists" in err:
                raise HTTPException(409, err)
            raise HTTPException(400, err)

@router.get("/properties/{property_id}/reviews")
def get_property_reviews(
    property_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn = Depends(get_public_db_connection)
):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT COUNT(*) FROM reviews WHERE property_id = %s AND status = 'Visible'",
            (property_id,)
        )
        total = cur.fetchone()["count"]
        
        cur.execute("""
            SELECT r.review_id, r.rating, r.comment, r.owner_reply, r.created_at,
                   u.name as full_name
            FROM reviews r
            JOIN users u ON r.student_id = u.user_id
            WHERE r.property_id = %s AND r.status = 'Visible'
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s
        """, (property_id, page_size, (page - 1) * page_size))
        
        reviews = cur.fetchall()
        for r in reviews:
            # Shorten name to first name + initial
            parts = r["full_name"].split()
            if len(parts) > 1:
                r["student_name"] = f"{parts[0]} {parts[-1][0]}."
            else:
                r["student_name"] = parts[0]
            del r["full_name"]
            
        return {"reviews": reviews, "total": total, "page": page, "page_size": page_size}

@router.post("/reviews/{review_id}/reply")
def reply_to_review(
    request: Request,
    review_id: str,
    data: ReplyReviewRequest,
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT r.* FROM reviews r
            JOIN properties p ON r.property_id = p.property_id
            WHERE r.review_id = %s AND p.owner_id = %s
        """, (review_id, uid))
        review = cur.fetchone()
        
        if not review:
            raise HTTPException(403, "Not authorized or review not found")
            
        cur.execute("UPDATE reviews SET owner_reply = %s WHERE review_id = %s RETURNING *", (data.owner_reply, review_id))
        updated = cur.fetchone()
        log_action(conn, uid, "owner", "reply", "review", review_id, review, updated)
        return updated

# Moderation

@router.patch("/admin/reviews/{review_id}/moderate")
def moderate_review(
    request: Request,
    review_id: str,
    data: ModerateReviewRequest,
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "admin")
    uid = get_uid(claims)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM reviews WHERE review_id = %s", (review_id,))
        review = cur.fetchone()
        if not review:
            raise HTTPException(404, "Review not found")
            
        cur.execute("""
            UPDATE reviews 
            SET status = %s, hidden_by = %s, hidden_reason = %s, hidden_at = now()
            WHERE review_id = %s
            RETURNING *
        """, (data.status, uid, data.reason, review_id))
        updated = cur.fetchone()
        
        log_action(conn, uid, "admin", "moderate", "review", review_id, review, updated)
        return updated

@router.get("/admin/reviews")
def list_reviews_admin(
    request: Request,
    status: Optional[str] = None,
    rating: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn = Depends(get_db_connection)
):
    require_role(request, "admin")
    with conn.cursor(row_factory=dict_row) as cur:
        conds = []
        params = []
        if status:
            conds.append("status = %s")
            params.append(status)
        if rating:
            conds.append("rating = %s")
            params.append(rating)
            
        where = "WHERE " + " AND ".join(conds) if conds else ""
        
        cur.execute(f"SELECT COUNT(*) FROM reviews {where}", params)
        total = cur.fetchone()["count"]
        
        cur.execute(f"SELECT * FROM reviews {where} ORDER BY created_at DESC LIMIT %s OFFSET %s", params + [page_size, (page - 1) * page_size])
        return {"reviews": cur.fetchall(), "total": total, "page": page, "page_size": page_size}

# Report Review
class ReportReviewRequest(BaseModel):
    reason: str = Field(..., max_length=500)

@router.post("/reviews/{review_id}/report", status_code=201)
def report_review(
    request: Request,
    review_id: str,
    data: ReportReviewRequest,
    conn = Depends(get_db_connection)
):
    # Any logged in user
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid = get_uid(claims)
    
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM reviews WHERE review_id = %s", (review_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Review not found")
            
        # In a real app we'd have a reports table, but requirement says "feeds an admin queue"
        # We can just use audit_logs as the queue, or create a simple table.
        # But wait, there was no reported_reviews table in the DB schema verified.
        # Let's log it to in_app_notifications for admin? 
        # Actually, let's create a review_reports table if it doesn't exist, or just log_action.
        cur.execute("CREATE TABLE IF NOT EXISTS review_reports (id serial primary key, review_id uuid references reviews(review_id), reported_by uuid, reason text, created_at timestamptz default now())")
        cur.execute("INSERT INTO review_reports (review_id, reported_by, reason) VALUES (%s, %s, %s)", (review_id, uid, data.reason))
        
        return {"status": "reported"}

@router.get("/reviews/mine")
def get_my_reviews(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn = Depends(get_db_connection)
):
    claims = require_role(request, "student")
    uid = get_uid(claims)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COUNT(*) FROM reviews WHERE student_id = %s", (uid,))
        total = cur.fetchone()["count"]
        
        cur.execute("""
            SELECT r.*, p.name as property_name
            FROM reviews r
            JOIN properties p ON r.property_id = p.property_id
            WHERE r.student_id = %s
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s
        """, (uid, page_size, (page - 1) * page_size))
        
        return {
            "reviews": cur.fetchall(),
            "total": total,
            "page": page,
            "page_size": page_size
        }
