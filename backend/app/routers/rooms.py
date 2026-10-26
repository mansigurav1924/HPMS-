"""
Rooms & Beds router.
"""
from fastapi import APIRouter, Request, HTTPException, Depends, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import psycopg
import json

from ..db.session import get_db_connection, require_role, get_uid
from ..services.audit import log_action
from ..services.storage import validate_file, upload_file, get_public_url, secure_filename

router = APIRouter(tags=["rooms"])


# ─────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────
class RoomCreate(BaseModel):
    room_number: str
    floor: Optional[str] = None
    room_type: str
    capacity: int
    is_ac: bool = False
    attached_bath: bool = False
    monthly_rent: float
    deposit: Optional[float] = None


class RoomUpdate(BaseModel):
    floor: Optional[str] = None
    room_type: Optional[str] = None
    capacity: Optional[int] = None
    is_ac: Optional[bool] = None
    attached_bath: Optional[bool] = None
    monthly_rent: Optional[float] = None
    deposit: Optional[float] = None


class BedStatusPatch(BaseModel):
    action: str   # "block" | "unblock"
    reason: Optional[str] = "Maintenance"


# ─────────────────────────────────────────────────────────────
# Room CRUD
# ─────────────────────────────────────────────────────────────
@router.post("/properties/{property_id}/rooms", status_code=201)
def create_room(
    property_id: str,
    body: RoomCreate,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    # Verify ownership
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM properties WHERE property_id = %s AND owner_id = %s",
            (property_id, uid)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Property not found or not yours")

        try:
            cur.execute(
                """INSERT INTO rooms
                   (property_id, room_number, floor, room_type, capacity,
                    is_ac, attached_bath, monthly_rent, deposit)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING room_id""",
                (property_id, body.room_number, body.floor, body.room_type,
                 body.capacity, body.is_ac, body.attached_bath,
                 body.monthly_rent, body.deposit)
            )
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            raise HTTPException(409, f"Room number '{body.room_number}' already exists in this property")

        room_id = str(cur.fetchone()[0])

        # Auto-create beds (Bed 1…n)
        for i in range(1, body.capacity + 1):
            cur.execute(
                "INSERT INTO beds (room_id, bed_label) VALUES (%s, %s)",
                (room_id, f"Bed {i}")
            )
    conn.commit()

    log_action(conn, uid, "owner", "ROOM_CREATED", "rooms", room_id,
               None, {"room_number": body.room_number, "capacity": body.capacity})

    return {"room_id": room_id, "beds_created": body.capacity}


@router.get("/properties/{property_id}/rooms")
def list_rooms(
    property_id: str,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM rooms WHERE property_id = %s ORDER BY room_number",
            (property_id,)
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.put("/rooms/{room_id}")
def update_room(
    room_id: str,
    body: RoomUpdate,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [room_id]

    with conn.cursor() as cur:
        # Verify ownership via join
        cur.execute(
            f"""UPDATE rooms r SET {set_clause}
                FROM properties p
                WHERE r.room_id = %s
                  AND r.property_id = p.property_id
                  AND p.owner_id = %s
                RETURNING r.room_id""",
            values + [uid]
        )
        if not cur.fetchone():
            raise HTTPException(404, "Room not found or not yours")
    conn.commit()

    return {"message": "Room updated"}


@router.get("/rooms/{room_id}/beds")
def list_beds(room_id: str, conn: psycopg.Connection = Depends(get_db_connection)):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM beds WHERE room_id = %s ORDER BY bed_label",
            (room_id,)
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ─────────────────────────────────────────────────────────────
# Bed block / unblock (via maintenance_tasks trigger)
# ─────────────────────────────────────────────────────────────
@router.patch("/beds/{bed_id}/status")
def patch_bed_status(
    bed_id: str,
    body: BedStatusPatch,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    if body.action not in ("block", "unblock"):
        raise HTTPException(422, "action must be block or unblock")

    with conn.cursor() as cur:
        # Verify ownership
        cur.execute(
            """SELECT b.bed_id, r.property_id FROM beds b
               JOIN rooms r ON r.room_id = b.room_id
               JOIN properties p ON p.property_id = r.property_id
               WHERE b.bed_id = %s AND p.owner_id = %s""",
            (bed_id, uid)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Bed not found or not yours")
        property_id = row[1]

        if body.action == "block":
            # Create a maintenance task with blocks_availability=true
            # The trigger will update bed.status to 'maintenance'
            cur.execute(
                """INSERT INTO maintenance_tasks
                   (property_id, bed_id, title, status, blocks_availability)
                   VALUES (%s, %s, %s, 'open', true)
                   RETURNING task_id""",
                (property_id, bed_id, body.reason or "Maintenance")
            )
            task_id = str(cur.fetchone()[0])
        else:
            # Close the open maintenance task for this bed
            cur.execute(
                """UPDATE maintenance_tasks
                   SET status = 'done', completed_on = now()
                   WHERE bed_id = %s AND blocks_availability AND status = 'open'
                   RETURNING task_id""",
                (bed_id,)
            )
            if not cur.fetchone():
                raise HTTPException(404, "No active maintenance block found for this bed")

    conn.commit()

    return {"message": f"Bed {body.action}ed successfully"}


# ─────────────────────────────────────────────────────────────
# Room photo upload
# ─────────────────────────────────────────────────────────────
@router.post("/rooms/{room_id}/photos")
async def upload_room_photo(
    room_id: str,
    request: Request,
    photo: UploadFile = File(...),
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    data = await photo.read()
    try:
        validate_file(data, len(data))
    except ValueError as e:
        raise HTTPException(422, str(e))

    path = f"{uid}/rooms/{room_id}/{secure_filename(photo.filename)}"
    upload_file("room-photos", path, data, photo.content_type)
    url = get_public_url("room-photos", path)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE rooms r
               SET photos = coalesce(r.photos, '[]'::jsonb) || %s::jsonb
               FROM properties p
               WHERE r.room_id = %s
                 AND r.property_id = p.property_id
                 AND p.owner_id = %s
               RETURNING r.room_id""",
            (json.dumps([url]), room_id, uid)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Room not found or not yours")
    conn.commit()

    return {"url": url}
