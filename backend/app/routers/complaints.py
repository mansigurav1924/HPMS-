"""
Phase 8: Complaints & Maintenance router

Endpoints
─────────
Complaints (student / owner / warden / admin):
  POST   /complaints                    raise a complaint (student, rate-limited)
  GET    /complaints                    list, role-scoped, paginated, filtered
  GET    /complaints/{id}               detail + update thread (visibility-scoped)
  PATCH  /complaints/{id}               acknowledge / assign / status change (owner/warden)
  POST   /complaints/{id}/updates       add comment (public or internal)
  POST   /complaints/{id}/reopen        reopen (student only, within window)
  POST   /complaints/{id}/rate          rate 1-5 (student, once, resolved/closed only)
  POST   /complaints/{id}/close         close (student or owner/warden)
  POST   /complaints/{id}/upload-photo  upload complaint photo (student)
  POST   /complaints/{id}/upload-proof  upload resolution proof (owner/warden)

Maintenance tasks (owner / warden):
  POST   /maintenance-tasks             create task
  GET    /maintenance-tasks             list, filtered by status/property
  PATCH  /maintenance-tasks/{id}        update task (status, cost, completed_on)
"""

import json
import logging
from datetime import date
from typing import List, Optional

import psycopg
import psycopg
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, validator

from ..db.session import get_db_connection, get_uid, require_role
from ..services.audit import log_action
from ..services.notifications import send_notification
from ..services.storage import (
    ALLOWED_IMAGE_TYPES,
    MAX_SIZE_BYTES,
    get_signed_url,
    upload_file,
    validate_file,
    secure_filename,
)
from ..core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["complaints"])


# ─── Pydantic models ────────────────────────────────────────────────────────

VALID_CATEGORIES = {
    "Electrical", "Plumbing", "Cleaning", "WiFi",
    "Furniture", "Food", "Security", "Noise_Roommate", "Other"
}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
VALID_STATUSES   = {"open", "acknowledged", "in_progress", "resolved", "closed", "reopened"}

COMPLAINT_BUCKET = "complaint-media"
MAX_PHOTOS       = 5


class RaiseComplaintRequest(BaseModel):
    category:    str
    description: str
    priority:    str = "medium"
    # photos are uploaded separately via /upload-photo

    @validator("category")
    def cat_valid(cls, v):
        if v not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category; must be one of {VALID_CATEGORIES}")
        return v

    @validator("priority")
    def pri_valid(cls, v):
        if v not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority; must be one of {VALID_PRIORITIES}")
        return v


class PatchComplaintRequest(BaseModel):
    """Owner/warden can change status, assign, set resolution date, add notes."""
    status:                  Optional[str] = None
    assigned_to:             Optional[str] = None  # user_id of staff/warden
    expected_resolution_date: Optional[date] = None
    resolution_notes:        Optional[str] = None
    note:                    Optional[str] = None  # shown in update thread


class AddUpdateRequest(BaseModel):
    message:    str
    visibility: str = "public"  # 'public' | 'internal'

    @validator("visibility")
    def vis_valid(cls, v):
        if v not in ("public", "internal"):
            raise ValueError("visibility must be 'public' or 'internal'")
        return v


class ReopenRequest(BaseModel):
    note: Optional[str] = None


class RateRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)


class CreateMaintenanceTaskRequest(BaseModel):
    property_id:         str
    room_id:             Optional[str] = None
    bed_id:              Optional[str] = None
    title:               str
    description:         Optional[str] = None
    vendor_name:         Optional[str] = None
    cost:                Optional[float] = None
    scheduled_on:        Optional[str] = None  # ISO datetime
    blocks_availability: bool = False


class PatchMaintenanceTaskRequest(BaseModel):
    status:       Optional[str]   = None  # pending | in_progress | done | cancelled
    cost:         Optional[float] = None
    completed_on: Optional[str]   = None  # ISO datetime
    vendor_name:  Optional[str]   = None


# ─── Helpers ────────────────────────────────────────────────────────────────

def _notify_complaint_parties(
    conn, complaint_id: str, template_id: str, context: dict, cur
):
    """
    Fetch complaint + property + student info and fire the notification.
    Silently skips if template not found.
    """
    cur.execute("""
        SELECT c.student_id, c.property_id,
               u.email AS student_email, u.name AS tenant_name,
               p.owner_id, ou.email AS owner_email, ou.name AS owner_name,
               p.name AS property_name
        FROM complaints c
        JOIN users  u  ON u.user_id  = c.student_id
        JOIN properties p ON p.property_id = c.property_id
        JOIN users  ou ON ou.user_id = p.owner_id
        WHERE c.complaint_id = %s
    """, (complaint_id,))
    row = cur.fetchone()
    if not row:
        return

    ctx = {**context, **{k: v for k, v in row.items()}}

    if template_id in ("complaint_received_student", "complaint_status_update",
                       "complaint_resolved"):
        send_notification(conn, row["student_id"], template_id,
                          row["student_email"], ctx)
    elif template_id in ("complaint_received_owner", "complaint_reopened"):
        send_notification(conn, row["owner_id"], template_id,
                          row["owner_email"], ctx)


def _photo_path(property_id: str, complaint_id: str, filename: str) -> str:
    """Generate a namespaced private-bucket path."""
    return f"properties/{property_id}/complaints/{complaint_id}/{filename}"


# ─── POST /complaints ────────────────────────────────────────────────────────

@router.post("/complaints", status_code=201)
@limiter.limit("10/hour")
def raise_complaint(request: Request, data: RaiseComplaintRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Student raises a complaint.
    Requires an active or notice tenancy (enforced in DB function raise_complaint).
    Rate-limited to 10 per hour to prevent spam.
    """
    claims = require_role(request, "student")
    uid    = get_uid(claims)

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT raise_complaint(%s, %s::complaint_category, %s, %s::complaint_priority, NULL)",
                (uid, data.category, data.description, data.priority)
            )
            complaint_id = cur.fetchone()["raise_complaint"]

            # Notify the student
            _notify_complaint_parties(
                conn, str(complaint_id), "complaint_received_student",
                {"complaint_id": str(complaint_id), "category": data.category}, cur
            )
            # Notify the owner
            _notify_complaint_parties(
                conn, str(complaint_id), "complaint_received_owner",
                {"complaint_id": str(complaint_id), "category": data.category,
                 "priority": data.priority}, cur
            )

        log_action(conn, uid, "student", "RAISE_COMPLAINT", "complaints",
                   str(complaint_id), new_val={"category": data.category,
                                               "priority": data.priority})
        conn.commit()
        return {"status": "success", "complaint_id": str(complaint_id)}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "NO_ACTIVE_TENANCY" in msg:
            raise HTTPException(403, "You must have an active tenancy to raise a complaint")
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── GET /complaints ─────────────────────────────────────────────────────────

@router.get("/complaints")
def list_complaints(
    request: Request,
    status:      Optional[str]  = None,
    category:    Optional[str]  = None,
    priority:    Optional[str]  = None,
    property_id: Optional[str]  = None,
    escalated:   Optional[bool] = None,
    date_from:   Optional[str]  = None,
    date_to:     Optional[str]  = None,
    page:        int = Query(1, ge=1),
    page_size:   int = Query(20, ge=1, le=100), conn: psycopg.Connection = Depends(get_db_connection)):
    """
    List complaints scoped by role:
    - student  → own complaints only
    - owner    → complaints for their properties
    - warden   → complaints for assigned properties (no financial data)
    - admin    → all
    """
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid    = get_uid(claims)
    role   = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        base = """
            SELECT
                c.complaint_id, c.category, c.description, c.priority, c.status,
                c.escalated, c.escalation_level, c.student_rating,
                c.created_at, c.acknowledged_at, c.resolved_at, c.closed_at,
                c.expected_resolution_date, c.assigned_to, c.reopened_count,
                u.name  AS tenant_name,
                p.name  AS property_name,
                p.property_id
            FROM complaints c
            JOIN users      u ON u.user_id      = c.student_id
            JOIN properties p ON p.property_id  = c.property_id
        """
        conditions, params = [], []

        # Role-based scoping
        if role == "student":
            conditions.append("c.student_id = %s");   params.append(uid)
        elif role == "owner":
            conditions.append("p.owner_id = %s");     params.append(uid)
        elif role == "warden":
            conditions.append(
                "p.property_id IN (SELECT property_id FROM property_staff WHERE user_id = %s)"
            );  params.append(uid)
        # admin: no scope

        # Optional filters
        if status:
            conditions.append("c.status = %s::complaint_status"); params.append(status)
        if category:
            conditions.append("c.category = %s::complaint_category"); params.append(category)
        if priority:
            conditions.append("c.priority = %s::complaint_priority"); params.append(priority)
        if property_id:
            conditions.append("c.property_id = %s"); params.append(property_id)
        if escalated is not None:
            conditions.append("c.escalated = %s"); params.append(escalated)
        if date_from:
            conditions.append("c.created_at >= %s::timestamptz"); params.append(date_from)
        if date_to:
            conditions.append("c.created_at <= %s::timestamptz"); params.append(date_to)

        where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        cur.execute(
            f"SELECT COUNT(*) FROM complaints c JOIN users u ON u.user_id = c.student_id "
            f"JOIN properties p ON p.property_id = c.property_id {where}",
            params
        )
        total = cur.fetchone()["count"]

        cur.execute(
            f"{base} {where} ORDER BY c.created_at DESC LIMIT %s OFFSET %s",
            params + [page_size, offset]
        )
        rows = cur.fetchall()

    return {"total": total, "page": page, "page_size": page_size, "complaints": rows}


# ─── GET /complaints/{id} ────────────────────────────────────────────────────

@router.get("/complaints/{complaint_id}")
def get_complaint(request: Request, complaint_id: str, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Returns complaint detail + update thread.
    Students see only PUBLIC updates; owners/wardens/admin see all.
    """
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid    = get_uid(claims)
    role   = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
                c.*,
                u.name  AS tenant_name,  u.email AS tenant_email,
                p.name  AS property_name, p.owner_id,
                p.complaint_sla
            FROM complaints c
            JOIN users      u ON u.user_id     = c.student_id
            JOIN properties p ON p.property_id = c.property_id
            WHERE c.complaint_id = %s
        """, (complaint_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Complaint not found")

        # Authorization
        if role == "student"  and row["student_id"] != uid:
            raise HTTPException(403, "Unauthorized")
        if role == "owner"    and row["owner_id"]   != uid:
            raise HTTPException(403, "Unauthorized")
        if role == "warden":
            cur.execute(
                "SELECT 1 FROM property_staff WHERE property_id = %s AND user_id = %s",
                (row["property_id"], uid)
            )
            if not cur.fetchone():
                raise HTTPException(403, "Unauthorized")

        # Fetch updates — students see only public
        vis_filter = "AND visibility = 'public'" if role == "student" else ""
        cur.execute(f"""
            SELECT cu.*, u.name AS author_name
            FROM complaint_updates cu
            JOIN users u ON u.user_id = cu.author_id
            WHERE cu.complaint_id = %s {vis_filter}
            ORDER BY cu.created_at
        """, (complaint_id,))
        updates = cur.fetchall()

        # Resolve signed URLs for photos
        photos = []
        if row.get("photos"):
            raw = row["photos"]
            paths = raw if isinstance(raw, list) else json.loads(raw)
            for p in paths:
                try:
                    photos.append(get_signed_url(COMPLAINT_BUCKET, p))
                except Exception:
                    photos.append(p)  # fall back to path if signing fails

        # Resolve resolution_proof signed URLs
        proofs = []
        if row.get("resolution_proof"):
            raw = row["resolution_proof"]
            paths = raw if isinstance(raw, list) else json.loads(raw)
            for p in paths:
                try:
                    proofs.append(get_signed_url(COMPLAINT_BUCKET, p))
                except Exception:
                    proofs.append(p)

    return {
        **row,
        "photos": photos,
        "resolution_proof": proofs,
        "updates": updates,
    }


# ─── PATCH /complaints/{id} ──────────────────────────────────────────────────

@router.patch("/complaints/{complaint_id}")
def patch_complaint(request: Request, complaint_id: str, data: PatchComplaintRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Owner / warden: change status, assign staff, set expected resolution date.
    Calls the DB function change_complaint_status for any status transitions.
    """
    claims = require_role(request, "owner", "warden", "admin")
    uid    = get_uid(claims)
    role   = claims.get("role")

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # Fetch complaint first to verify ownership
            cur.execute("""
                SELECT c.property_id, p.owner_id
                FROM complaints c
                JOIN properties p ON p.property_id = c.property_id
                WHERE c.complaint_id = %s
            """, (complaint_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Complaint not found")

            if role == "owner" and row["owner_id"] != uid:
                raise HTTPException(403, "Unauthorized")
            if role == "warden":
                cur.execute(
                    "SELECT 1 FROM property_staff WHERE property_id = %s AND user_id = %s",
                    (row["property_id"], uid)
                )
                if not cur.fetchone():
                    raise HTTPException(403, "Unauthorized")

            # Status change via the enforced DB function
            if data.status:
                note = data.resolution_notes or data.note
                cur.execute(
                    "SELECT change_complaint_status(%s, %s, %s::complaint_status, %s)",
                    (complaint_id, uid, data.status, note)
                )

            # Non-status field updates
            updates = {}
            if data.assigned_to is not None:
                updates["assigned_to"] = data.assigned_to
            if data.expected_resolution_date is not None:
                updates["expected_resolution_date"] = data.expected_resolution_date

            if updates:
                set_clause = ", ".join(f"{k} = %s" for k in updates)
                cur.execute(
                    f"UPDATE complaints SET {set_clause} WHERE complaint_id = %s",
                    list(updates.values()) + [complaint_id]
                )

            # Notify student of status change
            if data.status:
                _notify_complaint_parties(
                    conn, complaint_id, "complaint_status_update",
                    {"complaint_id": complaint_id, "new_status": data.status,
                     "note": data.note or ""}, cur
                )
                # Special resolved notification with rating prompt
                if data.status == "resolved":
                    cur.execute(
                        "SELECT complaint_sla->>'reopen_window_days', resolution_notes "
                        "FROM complaints c JOIN properties p ON p.property_id = c.property_id "
                        "WHERE c.complaint_id = %s",
                        (complaint_id,)
                    )
                    sla_row = cur.fetchone()
                    _notify_complaint_parties(
                        conn, complaint_id, "complaint_resolved",
                        {"complaint_id": complaint_id,
                         "resolution_notes": data.resolution_notes or "",
                         "rating_url": f"/complaints/{complaint_id}/rate",
                         "reopen_window_days": sla_row[0] or 7}, cur
                    )

        log_action(conn, uid, role, "PATCH_COMPLAINT", "complaints",
                   complaint_id, new_val=data.dict(exclude_none=True))
        conn.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "INVALID_TRANSITION"      in msg: raise HTTPException(409, msg)
        if "REOPEN_WINDOW_EXPIRED"   in msg: raise HTTPException(403, msg)
        if "RESOLUTION_NOTES_REQUIRED" in msg: raise HTTPException(422, msg)
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── POST /complaints/{id}/updates ───────────────────────────────────────────

@router.post("/complaints/{complaint_id}/updates", status_code=201)
def add_update(request: Request, complaint_id: str, data: AddUpdateRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Add a comment thread entry.
    Internal visibility is only allowed for owner / warden / admin.
    Students can only post 'public' updates on their own complaints.
    """
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid    = get_uid(claims)
    role   = claims.get("role")

    # Students can only post public comments
    if role == "student" and data.visibility == "internal":
        raise HTTPException(403, "Students cannot post internal updates")

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT student_id, property_id FROM complaints WHERE complaint_id = %s",
                (complaint_id,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Complaint not found")

            if role == "student" and row["student_id"] != uid:
                raise HTTPException(403, "Unauthorized")

            cur.execute("""
                INSERT INTO complaint_updates
                    (complaint_id, author_id, visibility, message)
                VALUES (%s, %s, %s, %s)
                RETURNING update_id
            """, (complaint_id, uid, data.visibility, data.message))
            update_id = cur.fetchone()["update_id"]

        conn.commit()
        return {"status": "success", "update_id": str(update_id)}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── POST /complaints/{id}/reopen ────────────────────────────────────────────

@router.post("/complaints/{complaint_id}/reopen")
def reopen_complaint(request: Request, complaint_id: str, data: ReopenRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Student reopens a resolved complaint within the reopen window.
    The DB function validates the window and raises REOPEN_WINDOW_EXPIRED if late.
    """
    claims = require_role(request, "student")
    uid    = get_uid(claims)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT change_complaint_status(%s, %s, 'reopened'::complaint_status, %s)",
                (complaint_id, uid, data.note)
            )
            # Notify owner
            _notify_complaint_parties(
                conn, complaint_id, "complaint_reopened",
                {"complaint_id": complaint_id, "note": data.note or ""},
                cur
            )

        log_action(conn, uid, "student", "REOPEN_COMPLAINT", "complaints", complaint_id)
        conn.commit()
        return {"status": "reopened"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "REOPEN_WINDOW_EXPIRED" in msg: raise HTTPException(403, msg)
        if "INVALID_TRANSITION"    in msg: raise HTTPException(409, msg)
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── POST /complaints/{id}/rate ──────────────────────────────────────────────

@router.post("/complaints/{complaint_id}/rate")
def rate_complaint(request: Request, complaint_id: str, data: RateRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Student rates a resolved/closed complaint 1-5, once only.
    422 for invalid rating value (enforced by Pydantic Field(ge=1,le=5) + DB).
    """
    claims = require_role(request, "student")
    uid    = get_uid(claims)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rate_complaint(%s, %s, %s)",
                (complaint_id, uid, data.rating)
            )
        log_action(conn, uid, "student", "RATE_COMPLAINT", "complaints",
                   complaint_id, new_val={"rating": data.rating})
        conn.commit()
        return {"status": "rated"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "ALREADY_RATED"    in msg: raise HTTPException(409, msg)
        if "INVALID_STATUS"   in msg: raise HTTPException(409, msg)
        if "INVALID_RATING"   in msg: raise HTTPException(422, msg)
        if "UNAUTHORIZED"     in msg: raise HTTPException(403, msg)
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── POST /complaints/{id}/close ─────────────────────────────────────────────

@router.post("/complaints/{complaint_id}/close")
def close_complaint(request: Request, complaint_id: str, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Student closes their own complaint, or owner/warden/admin closes it.
    """
    claims = require_role(request, "student", "owner", "warden", "admin")
    uid    = get_uid(claims)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT change_complaint_status(%s, %s, 'closed'::complaint_status, NULL)",
                (complaint_id, uid)
            )
        log_action(conn, uid, claims.get("role"), "CLOSE_COMPLAINT",
                   "complaints", complaint_id)
        conn.commit()
        return {"status": "closed"}
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "INVALID_TRANSITION" in msg: raise HTTPException(409, msg)
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── POST /complaints/{id}/upload-photo ──────────────────────────────────────

@router.post("/complaints/{complaint_id}/upload-photo")
async def upload_complaint_photo(
    request: Request,
    complaint_id: str,
    file:         UploadFile = File(...), conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Upload one complaint photo to the private 'complaint-media' bucket.
    - Images only (jpg / png / webp), max 5 MB
    - Max 5 photos per complaint
    - Returns a signed URL (1 h expiry) for immediate preview
    """
    claims = require_role(request, "student")
    uid    = get_uid(claims)

    content  = await file.read()
    try:
        validate_file(content, len(content), allow_pdf=False)
    except ValueError as e:
        raise HTTPException(422, str(e))

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT photos, property_id, student_id FROM complaints WHERE complaint_id = %s",
            (complaint_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Complaint not found")
        if row["student_id"] != uid:
            raise HTTPException(403, "Unauthorized")

        existing = row["photos"] or []
        if isinstance(existing, str):
            existing = json.loads(existing)

        if len(existing) >= MAX_PHOTOS:
            raise HTTPException(400, f"Maximum {MAX_PHOTOS} photos per complaint")

        # Upload
        filename = f"{len(existing)}_{secure_filename(file.filename)}"
        path     = _photo_path(str(row["property_id"]), complaint_id, filename)
        upload_file(COMPLAINT_BUCKET, path, content, file.content_type)

        # Append path to photos jsonb
        existing.append(path)
        cur.execute(
            "UPDATE complaints SET photos = %s WHERE complaint_id = %s",
            (json.dumps(existing), complaint_id)
        )

    conn.commit()
    signed = get_signed_url(COMPLAINT_BUCKET, path)
    return {"status": "uploaded", "path": path, "signed_url": signed}


# ─── POST /complaints/{id}/upload-proof ──────────────────────────────────────

@router.post("/complaints/{complaint_id}/upload-proof")
async def upload_resolution_proof(
    request: Request,
    complaint_id: str,
    file:         UploadFile = File(...), conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Owner / warden uploads resolution proof photo.
    Same validation as complaint photos. Appended to resolution_proof jsonb.
    """
    claims = require_role(request, "owner", "warden", "admin")
    uid    = get_uid(claims)

    content = await file.read()
    try:
        validate_file(content, len(content), allow_pdf=False)
    except ValueError as e:
        raise HTTPException(422, str(e))

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT resolution_proof, property_id FROM complaints WHERE complaint_id = %s",
            (complaint_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Complaint not found")

        existing = row["resolution_proof"] or []
        if isinstance(existing, str):
            existing = json.loads(existing)
        if len(existing) >= MAX_PHOTOS:
            raise HTTPException(400, f"Maximum {MAX_PHOTOS} proof files per complaint")

        filename = f"proof_{len(existing)}_{secure_filename(file.filename)}"
        path     = _photo_path(str(row["property_id"]), complaint_id, filename)
        upload_file(COMPLAINT_BUCKET, path, content, file.content_type)

        existing.append(path)
        cur.execute(
            "UPDATE complaints SET resolution_proof = %s WHERE complaint_id = %s",
            (json.dumps(existing), complaint_id)
        )

    conn.commit()
    signed = get_signed_url(COMPLAINT_BUCKET, path)
    return {"status": "uploaded", "path": path, "signed_url": signed}


# ─── POST /maintenance-tasks ─────────────────────────────────────────────────

@router.post("/maintenance-tasks", status_code=201)
def create_maintenance_task(
    request: Request, data: CreateMaintenanceTaskRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Owner or warden creates a maintenance task.
    If blocks_availability=True on a bed, the DB validates it isn't occupied.
    Room-level and property-level tasks do not affect bed availability.
    """
    claims = require_role(request, "owner", "warden", "admin")
    uid    = get_uid(claims)
    role   = claims.get("role")

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # Verify property access
            cur.execute("SELECT owner_id FROM properties WHERE property_id = %s",
                        (data.property_id,))
            prop = cur.fetchone()
            if not prop:
                raise HTTPException(404, "Property not found")
            if role == "owner" and prop["owner_id"] != uid:
                raise HTTPException(403, "Unauthorized")
            if role == "warden":
                cur.execute(
                    "SELECT 1 FROM property_staff WHERE property_id = %s AND user_id = %s",
                    (data.property_id, uid)
                )
                if not cur.fetchone():
                    raise HTTPException(403, "Unauthorized")

            cur.execute("""
                INSERT INTO maintenance_tasks
                    (property_id, room_id, bed_id, title, description,
                     vendor_name, cost, scheduled_on, blocks_availability, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                RETURNING task_id
            """, (
                data.property_id, data.room_id, data.bed_id,
                data.title, data.description, data.vendor_name,
                data.cost, data.scheduled_on, data.blocks_availability
            ))
            task_id = cur.fetchone()["task_id"]

            # Validate bed availability AFTER insert (trigger fires too, but
            # we want a clear 409 before commit)
            if data.bed_id and data.blocks_availability:
                try:
                    cur.execute("SELECT validate_maintenance_block(%s)", (task_id,))
                except psycopg.errors.RaiseException as e:
                    raise  # re-raise; caught below

        log_action(conn, uid, role, "CREATE_MAINTENANCE_TASK", "maintenance_tasks",
                   str(task_id), new_val=data.dict(exclude_none=True))
        conn.commit()
        return {"status": "created", "task_id": str(task_id)}
    except HTTPException:
        raise
    except psycopg.errors.RaiseException as e:
        conn.rollback()
        msg = str(e)
        if "BED_IN_USE" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(400, msg)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ─── GET /maintenance-tasks ───────────────────────────────────────────────────

@router.get("/maintenance-tasks")
def list_maintenance_tasks(
    request: Request,
    property_id: Optional[str] = None,
    status:      Optional[str] = None,
    page:        int = Query(1, ge=1),
    page_size:   int = Query(20, ge=1, le=100), conn: psycopg.Connection = Depends(get_db_connection)):
    """
    List maintenance tasks scoped by role.
    Wardens can see and create tasks but cost is hidden (null) for them.
    """
    claims = require_role(request, "owner", "warden", "admin")
    uid    = get_uid(claims)
    role   = claims.get("role")

    with conn.cursor(row_factory=dict_row) as cur:
        # Wardens do not see cost (financial data)
        cost_col = "NULL::numeric AS cost" if role == "warden" else "mt.cost"

        base = f"""
            SELECT
                mt.task_id, mt.property_id, mt.room_id, mt.bed_id,
                mt.title, mt.description, mt.vendor_name,
                {cost_col},
                mt.status, mt.scheduled_on, mt.completed_on,
                mt.blocks_availability,
                p.name AS property_name
            FROM maintenance_tasks mt
            JOIN properties p ON p.property_id = mt.property_id
        """
        conditions, params = [], []

        if role == "owner":
            conditions.append("p.owner_id = %s"); params.append(uid)
        elif role == "warden":
            conditions.append(
                "mt.property_id IN (SELECT property_id FROM property_staff WHERE user_id = %s)"
            );  params.append(uid)

        if property_id:
            conditions.append("mt.property_id = %s"); params.append(property_id)
        if status:
            conditions.append("mt.status = %s::task_status"); params.append(status)

        where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        cur.execute(
            f"SELECT COUNT(*) FROM maintenance_tasks mt "
            f"JOIN properties p ON p.property_id = mt.property_id {where}",
            params
        )
        total = cur.fetchone()["count"]

        cur.execute(
            f"{base} {where} ORDER BY mt.scheduled_on DESC NULLS LAST LIMIT %s OFFSET %s",
            params + [page_size, offset]
        )
        rows = cur.fetchall()

    return {"total": total, "page": page, "page_size": page_size, "tasks": rows}


# ─── PATCH /maintenance-tasks/{id} ───────────────────────────────────────────

@router.patch("/maintenance-tasks/{task_id}")
def patch_maintenance_task(
    request: Request, task_id: str, data: PatchMaintenanceTaskRequest, conn: psycopg.Connection = Depends(get_db_connection)):
    """
    Update task status, cost, or completed_on.
    Completing a task (status=done) restores the bed's derived availability via trigger.
    """
    claims = require_role(request, "owner", "warden", "admin")
    uid    = get_uid(claims)
    role   = claims.get("role")

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT mt.property_id, p.owner_id "
                "FROM maintenance_tasks mt JOIN properties p ON p.property_id = mt.property_id "
                "WHERE task_id = %s",
                (task_id,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Task not found")

            if role == "owner" and row["owner_id"] != uid:
                raise HTTPException(403, "Unauthorized")
            if role == "warden":
                cur.execute(
                    "SELECT 1 FROM property_staff WHERE property_id = %s AND user_id = %s",
                    (row["property_id"], uid)
                )
                if not cur.fetchone():
                    raise HTTPException(403, "Unauthorized")

                # Wardens cannot set cost
                if data.cost is not None:
                    raise HTTPException(403, "Wardens cannot set task cost")

            updates = {}
            if data.status:
                updates["status"] = data.status  # trigger will sync bed
            if data.cost is not None:
                updates["cost"] = data.cost
            if data.completed_on is not None:
                updates["completed_on"] = data.completed_on
            if data.vendor_name is not None:
                updates["vendor_name"] = data.vendor_name

            if not updates:
                return {"status": "no_changes"}

            set_clause = ", ".join(f"{k} = %s" for k in updates)
            # Cast status to enum
            if "status" in updates:
                set_clause = set_clause.replace(
                    "status = %s", "status = %s::task_status"
                )

            cur.execute(
                f"UPDATE maintenance_tasks SET {set_clause} WHERE task_id = %s",
                list(updates.values()) + [task_id]
            )

        log_action(conn, uid, role, "PATCH_MAINTENANCE_TASK", "maintenance_tasks",
                   task_id, new_val=data.dict(exclude_none=True))
        conn.commit()
        return {"status": "updated"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
