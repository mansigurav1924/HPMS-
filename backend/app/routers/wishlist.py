"""
Wishlist router for students (compare moved to properties.py).
"""
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
import psycopg
import json

from ..db.session import get_db_connection, get_public_db_connection, require_role, get_uid
from ..services.audit import log_action

router = APIRouter(tags=["wishlist"])


class WishlistAddRequest(BaseModel):
    property_id: str


# ─────────────────────────────────────────────────────────────
# Wishlist Endpoints (Student Only)
# ─────────────────────────────────────────────────────────────
@router.post("/wishlist", status_code=201)
def add_to_wishlist(
    body: WishlistAddRequest,
    request: Request,
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    claims = require_role(request, "student")
    uid = get_uid(claims)

    with conn.cursor() as cur:
        # Verify property exists
        cur.execute(
            """SELECT p.property_id FROM properties p
               JOIN owner_profiles op ON op.owner_id = p.owner_id
               WHERE p.property_id = %s""",
            (body.property_id,)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Property not found")

        # Check if already in wishlist
        cur.execute(
            "SELECT 1 FROM wishlists WHERE user_id = %s AND property_id = %s",
            (uid, body.property_id)
        )
        if cur.fetchone():
            return {"message": "Already in wishlist"}

        cur.execute(
            "INSERT INTO wishlists (user_id, property_id) VALUES (%s, %s)",
            (uid, body.property_id)
        )
    conn.commit()

    log_action(conn, uid, "student", "WISHLIST_ADD", "wishlists", body.property_id, None, None)
    return {"message": "Added to wishlist"}


@router.delete("/wishlist/{property_id}", status_code=200)
def remove_from_wishlist(
    property_id: str,
    request: Request,
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    claims = require_role(request, "student")
    uid = get_uid(claims)

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM wishlists WHERE user_id = %s AND property_id = %s RETURNING property_id",
            (uid, property_id)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Property not in wishlist")
    conn.commit()

    log_action(conn, uid, "student", "WISHLIST_REMOVE", "wishlists", property_id, None, None)
    return {"message": "Removed from wishlist"}


@router.get("/wishlist")
def get_wishlist(
    request: Request,
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    claims = require_role(request, "student")
    uid = get_uid(claims)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.property_id, p.name, p.type, p.city, p.area, p.address,
                      p.avg_rating, p.photos, p.facilities,
                      COALESCE(MIN(r.monthly_rent), 0) AS min_rent,
                      COALESCE(MAX(r.monthly_rent), 0) AS max_rent,
                      COUNT(b.bed_id) FILTER (WHERE b.status = 'available') AS available_beds_count
               FROM wishlists w
               JOIN properties p ON p.property_id = w.property_id
               JOIN owner_profiles op ON op.owner_id = p.owner_id
               LEFT JOIN rooms r ON r.property_id = p.property_id AND r.status = 'Active'
               LEFT JOIN beds b ON b.room_id = r.room_id
               WHERE w.user_id = %s
               GROUP BY p.property_id, w.added_at
               ORDER BY w.added_at DESC""",
            (uid,)
        )
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
        results = []
        for r in rows:
            item = dict(zip(cols, r))
            photos = item.get("photos")
            if isinstance(photos, str):
                try: photos = json.loads(photos)
                except Exception: photos = []
            item["cover_photo"] = photos[0] if (photos and len(photos) > 0) else None
            results.append(item)
        return results
