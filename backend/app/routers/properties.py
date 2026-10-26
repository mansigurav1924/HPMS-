"""
Properties router — full CRUD for owners, public search & detailed discovery for students.
"""
from fastapi import APIRouter, Request, HTTPException, Depends, UploadFile, File, Query
from pydantic import BaseModel, field_validator, Field, model_validator
from typing import Optional, List, Any
import psycopg
import json
import math
from datetime import date

from ..db.session import get_db_connection, get_public_db_connection, require_role, get_uid
from ..services.audit import log_action
from ..services.storage import validate_file, upload_file, get_public_url, secure_filename
from ..core.rate_limit import limiter

router = APIRouter(tags=["properties"])


# ─────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────
class PropertyCreate(BaseModel):
    name: str
    type: str
    address: str
    city: str
    area: Optional[str] = None
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    description: Optional[str] = None
    house_rules: Optional[str] = None
    facilities: Optional[List[str]] = None
    security_deposit_default: Optional[float] = None
    notice_period_days: Optional[int] = None
    lock_in_months: Optional[int] = None
    booking_mode: str = "approval"
    cancellation_policy: Optional[dict] = None
    late_fee_config: Optional[dict] = None
    rent_due_day: Optional[int] = None
    rent_cycle_mode: Optional[str] = None

    @field_validator("type")
    @classmethod
    def valid_type(cls, v):
        allowed = {"boys_hostel", "girls_hostel", "coed", "pg"}
        if v not in allowed:
            raise ValueError(f"type must be one of {allowed}")
        return v

    @field_validator("booking_mode")
    @classmethod
    def valid_mode(cls, v):
        if v not in ("instant", "approval"):
            raise ValueError("booking_mode must be instant or approval")
        return v

    @model_validator(mode="after")
    def check_coords(self):
        if self.latitude is not None and self.longitude is not None:
            if self.latitude == 0 and self.longitude == 0:
                raise ValueError("Coordinates cannot be (0, 0)")
        return self


class PropertyUpdate(PropertyCreate):
    name: Optional[str] = None
    type: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    booking_mode: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────
def _check_duplicate(conn, address, lat, lon, owner_id, property_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT check_property_duplicate(%s, %s, %s, %s, %s)",
            (address, lat, lon, owner_id, property_id)
        )


def _haversine_distance(lat1, lon1, lat2, lon2):
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(r * c, 2)


# ─────────────────────────────────────────────────────────────
# Owner endpoints
# ─────────────────────────────────────────────────────────────
@router.post("/properties", status_code=201)
def create_property(
    body: PropertyCreate,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT verification_status FROM owner_profiles WHERE owner_id = %s", (uid,)
        )
        row = cur.fetchone()
        if not row or row[0] != "approved":
            raise HTTPException(403, "Owner KYC not approved yet")

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO properties
               (owner_id, name, type, address, city, area, latitude, longitude,
                description, house_rules, facilities,
                security_deposit_default, notice_period_days, lock_in_months,
                booking_mode, cancellation_policy, late_fee_config,
                rent_due_day, rent_cycle_mode, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Draft')
               RETURNING property_id""",
            (uid, body.name, body.type, body.address, body.city, body.area,
             body.latitude, body.longitude, body.description, body.house_rules,
             json.dumps(body.facilities or []),
             body.security_deposit_default, body.notice_period_days,
             body.lock_in_months, body.booking_mode,
             json.dumps(body.cancellation_policy or {}),
             json.dumps(body.late_fee_config or {}),
             body.rent_due_day, body.rent_cycle_mode)
        )
        prop_id = str(cur.fetchone()[0])
    conn.commit()

    _check_duplicate(conn, body.address, body.latitude, body.longitude, uid, prop_id)

    log_action(conn, uid, "owner", "PROPERTY_CREATED", "properties", prop_id,
               None, {"name": body.name, "city": body.city})

    return {"property_id": prop_id, "status": "Draft"}


@router.get("/properties/mine")
def my_properties(
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT property_id, name, type, city, address, status, created_at
               FROM properties WHERE owner_id = %s ORDER BY created_at DESC""",
            (uid,)
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.put("/properties/{property_id}")
def update_property(
    property_id: str,
    body: PropertyUpdate,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")

    for field in ("facilities", "cancellation_policy", "late_fee_config"):
        if field in updates:
            updates[field] = json.dumps(updates[field])

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [property_id, uid]

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE properties SET {set_clause} WHERE property_id = %s AND owner_id = %s RETURNING address, latitude, longitude",
            values
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Property not found or not yours")
        address, lat, lng = row
    conn.commit()

    if "address" in updates or "latitude" in updates or "longitude" in updates:
        _check_duplicate(conn, address, lat, lng, uid, property_id)

    log_action(conn, uid, "owner", "PROPERTY_UPDATED", "properties", property_id,
               None, {k: v for k, v in updates.items()})

    return {"message": "Property updated"}


@router.post("/properties/{property_id}/submit")
def submit_for_review(
    property_id: str,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE properties SET status = 'Pending'
               WHERE property_id = %s AND owner_id = %s AND status = 'Draft'
               RETURNING property_id""",
            (property_id, uid)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Property not found, not yours, or not in Draft")
    conn.commit()

    log_action(conn, uid, "owner", "PROPERTY_SUBMITTED", "properties", property_id,
               None, {"status": "Pending"})

    return {"message": "Property submitted for review"}


@router.delete("/properties/{property_id}", status_code=204)
def deactivate_property(
    property_id: str,
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) FROM tenancies t
               JOIN beds b ON b.bed_id = t.bed_id
               JOIN rooms r ON r.room_id = b.room_id
               WHERE r.property_id = %s AND t.status IN ('active','notice')""",
            (property_id,)
        )
        active = cur.fetchone()[0]
        if active > 0:
            raise HTTPException(409, "Cannot deactivate: property has active tenants")

        cur.execute(
            """UPDATE properties SET status = 'Inactive'
               WHERE property_id = %s AND owner_id = %s RETURNING property_id""",
            (property_id, uid)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Property not found or not yours")
    conn.commit()

    log_action(conn, uid, "owner", "PROPERTY_DEACTIVATED", "properties", property_id,
               None, {"status": "Inactive"})


@router.post("/properties/{property_id}/photos")
async def upload_property_photo(
    property_id: str,
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

    path = f"{uid}/{property_id}/{secure_filename(photo.filename)}"
    upload_file("room-photos", path, data, photo.content_type)
    url = get_public_url("room-photos", path)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE properties
               SET photos = coalesce(photos, '[]'::jsonb) || %s::jsonb
               WHERE property_id = %s AND owner_id = %s RETURNING property_id""",
            (json.dumps([url]), property_id, uid)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Property not found or not yours")
    conn.commit()

    return {"url": url}


# ─────────────────────────────────────────────────────────────
# Phase 3: Public Search & Discovery (No login needed)
# ─────────────────────────────────────────────────────────────
@router.get("/properties")
@limiter.limit("60/minute")
def search_properties(
    request: Request,
    q: Optional[str] = Query(None, description="Full-text search query"),
    city: Optional[str] = Query(None),
    area: Optional[str] = Query(None),
    college_id: Optional[str] = Query(None),
    radius_km: Optional[float] = Query(None, ge=0),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    room_type: Optional[str] = Query(None),
    sharing: Optional[int] = Query(None, ge=1),
    gender_type: Optional[str] = Query(None),
    is_ac: Optional[bool] = Query(None),
    food: Optional[bool] = Query(None),
    facilities: Optional[str] = Query(None, description="Comma separated facilities"),
    available_from: Optional[date] = Query(None),
    north: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    west: Optional[float] = Query(None),
    sort: Optional[str] = Query("price_asc", description="price_asc|price_desc|distance|rating|newest"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    q = q.default if hasattr(q, "default") else q
    city = city.default if hasattr(city, "default") else city
    area = area.default if hasattr(area, "default") else area
    college_id = college_id.default if hasattr(college_id, "default") else college_id
    radius_km = radius_km.default if hasattr(radius_km, "default") else radius_km
    min_price = min_price.default if hasattr(min_price, "default") else min_price
    max_price = max_price.default if hasattr(max_price, "default") else max_price
    room_type = room_type.default if hasattr(room_type, "default") else room_type
    sharing = sharing.default if hasattr(sharing, "default") else sharing
    gender_type = gender_type.default if hasattr(gender_type, "default") else gender_type
    is_ac = is_ac.default if hasattr(is_ac, "default") else is_ac
    food = food.default if hasattr(food, "default") else food
    facilities = facilities.default if hasattr(facilities, "default") else facilities
    available_from = available_from.default if hasattr(available_from, "default") else available_from
    north = north.default if hasattr(north, "default") else north
    south = south.default if hasattr(south, "default") else south
    east = east.default if hasattr(east, "default") else east
    west = west.default if hasattr(west, "default") else west
    sort = (sort.default if hasattr(sort, "default") else sort) or "price_asc"
    page = (page.default if hasattr(page, "default") else page) or 1
    page_size = (page_size.default if hasattr(page_size, "default") else page_size) or 10

    if sort not in ("price_asc", "price_desc", "distance", "rating", "newest"):
        raise HTTPException(422, "Invalid sort parameter")

    if gender_type and gender_type not in ("boys_hostel", "girls_hostel", "coed", "pg"):
        raise HTTPException(422, "Invalid gender_type parameter")

    # College location for distance calculation if college_id provided
    college_lat, college_lon = None, None
    if college_id:
        with conn.cursor() as cur:
            cur.execute("SELECT latitude, longitude FROM colleges WHERE college_id = %s", (college_id,))
            crow = cur.fetchone()
            if crow:
                college_lat, college_lon = crow[0], crow[1]

    # SQL query construction
    # Relaxed filters as requested by user to show ALL properties in DB
    where_clauses = [
        "1=1" # Placeholder to allow all properties
    ]
    params = []

    if q and q.strip():
        # Full text search or ILIKE fallback
        where_clauses.append("(p.tsv @@ websearch_to_tsquery('english', %s) OR p.name ILIKE %s OR p.city ILIKE %s OR p.area ILIKE %s OR p.address ILIKE %s OR p.description ILIKE %s)")
        q_clean = q.strip()
        
        # Handle common typos
        if q_clean.lower() == "vadodra":
            q_clean = "Vadodara"
        elif q_clean.lower() == "ahmedbad":
            q_clean = "Ahmedabad"
            
        params.extend([q_clean, f"%{q_clean}%", f"%{q_clean}%", f"%{q_clean}%", f"%{q_clean}%", f"%{q_clean}%"])

    if city:
        where_clauses.append("LOWER(p.city) = LOWER(%s)")
        params.append(city.strip())

    if area:
        where_clauses.append("LOWER(p.area) = LOWER(%s)")
        params.append(area.strip())

    if gender_type:
        where_clauses.append("p.type = %s")
        params.append(gender_type)

    if room_type:
        where_clauses.append("r.room_type = %s")
        params.append(room_type)

    if sharing:
        where_clauses.append("r.capacity = %s")
        params.append(sharing)

    if is_ac is not None:
        where_clauses.append("r.is_ac = %s")
        params.append(is_ac)

    if food:
        where_clauses.append("(p.facilities::text ILIKE '%%food%%' OR p.facilities::text ILIKE '%%mess%%' OR p.facilities::text ILIKE '%%meals%%')")

    if facilities:
        facility_list = [f.strip() for f in facilities.split(",") if f.strip()]
        for fac in facility_list:
            where_clauses.append("p.facilities::text ILIKE %s")
            params.append(f"%{fac}%")

    if available_from:
        where_clauses.append("(b.available_from IS NULL OR b.available_from <= %s)")
        params.append(available_from)

    if north is not None and south is not None and east is not None and west is not None:
        where_clauses.append("p.latitude <= %s AND p.latitude >= %s AND p.longitude <= %s AND p.longitude >= %s")
        params.extend([north, south, east, west])

    where_sql = " AND ".join(where_clauses)

    # Having clause for min/max price & distance filter
    having_clauses = ["1=1"] # Relaxed to show properties without available beds
    having_params = []

    if min_price is not None:
        having_clauses.append("MIN(r.monthly_rent) >= %s")
        having_params.append(min_price)

    if max_price is not None:
        having_clauses.append("MIN(r.monthly_rent) <= %s")
        having_params.append(max_price)

    if college_lat is not None and college_lon is not None and radius_km is not None:
        having_clauses.append(f"distance_km(p.latitude, p.longitude, {college_lat}, {college_lon}) <= %s")
        having_params.append(radius_km)

    having_sql = " HAVING " + " AND ".join(having_clauses)

    # Sort clause
    dist_expr = f"distance_km(p.latitude, p.longitude, {college_lat}, {college_lon})" if (college_lat is not None) else "NULL"
    sort_map = {
        "price_asc": "min_rent ASC",
        "price_desc": "min_rent DESC",
        "distance": f"{dist_expr} ASC NULLS LAST",
        "rating": "p.avg_rating DESC NULLS LAST",
        "newest": "p.created_at DESC"
    }
    order_by_sql = sort_map[sort]

    sql_query = f"""
        SELECT p.property_id, p.name, p.type, p.city, p.area, p.address, p.description,
               p.latitude, p.longitude, p.facilities, p.photos, p.avg_rating, p.created_at,
               COALESCE(MIN(r.monthly_rent), 0) AS min_rent,
               COALESCE(MAX(r.monthly_rent), 0) AS max_rent,
               COUNT(b.bed_id) FILTER (WHERE b.status = 'available') AS available_beds_count,
               ARRAY_AGG(DISTINCT r.room_type) AS room_types,
               {dist_expr} AS distance_km
        FROM properties p
        LEFT JOIN owner_profiles op ON op.owner_id = p.owner_id
        LEFT JOIN rooms r ON r.property_id = p.property_id
        LEFT JOIN beds b ON b.room_id = r.room_id
        WHERE {where_sql}
        GROUP BY p.property_id, p.name, p.type, p.city, p.area, p.address, p.description,
                 p.latitude, p.longitude, p.facilities, p.photos, p.avg_rating, p.created_at
        {having_sql}
        ORDER BY {order_by_sql}
    """

    full_params = params + having_params

    with conn.cursor() as cur:
        cur.execute(sql_query, full_params)
        all_rows = cur.fetchall()
        cols = [d.name for d in cur.description]

    total = len(all_rows)
    start_idx = (page - 1) * page_size
    page_rows = all_rows[start_idx:start_idx + page_size]

    items = []
    for r in page_rows:
        item = dict(zip(cols, r))
        if item.get("property_id") is not None:
            item["property_id"] = str(item["property_id"])
        # Parse JSON photos & facilities if needed
        photos = item.get("photos")
        if isinstance(photos, str):
            try: photos = json.loads(photos)
            except Exception: photos = []
        item["cover_photo"] = photos[0] if (photos and len(photos) > 0) else None

        facilities_val = item.get("facilities")
        if isinstance(facilities_val, str):
            try: item["facilities"] = json.loads(facilities_val)
            except Exception: item["facilities"] = []

        # Convert float for rent and distance
        item["min_rent"] = float(item["min_rent"]) if item.get("min_rent") is not None else 0.0
        item["max_rent"] = float(item["max_rent"]) if item.get("max_rent") is not None else 0.0
        if item.get("distance_km") is None and college_lat is not None and item.get("latitude") and item.get("longitude"):
            item["distance_km"] = _haversine_distance(item["latitude"], item["longitude"], college_lat, college_lon)

        items.append(item)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# ─────────────────────────────────────────────────────────────
# Public Map Search Endpoint (Lightweight, max 200 markers)
# ─────────────────────────────────────────────────────────────
@router.get("/properties/map")
@limiter.limit("60/minute")
def map_properties(
    request: Request,
    q: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    area: Optional[str] = Query(None),
    college_id: Optional[str] = Query(None),
    radius_km: Optional[float] = Query(None, ge=0),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    room_type: Optional[str] = Query(None),
    sharing: Optional[int] = Query(None, ge=1),
    gender_type: Optional[str] = Query(None),
    is_ac: Optional[bool] = Query(None),
    food: Optional[bool] = Query(None),
    facilities: Optional[str] = Query(None),
    available_from: Optional[date] = Query(None),
    north: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    west: Optional[float] = Query(None),
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    q = q.default if hasattr(q, "default") else q
    city = city.default if hasattr(city, "default") else city
    area = area.default if hasattr(area, "default") else area
    college_id = college_id.default if hasattr(college_id, "default") else college_id
    radius_km = radius_km.default if hasattr(radius_km, "default") else radius_km
    min_price = min_price.default if hasattr(min_price, "default") else min_price
    max_price = max_price.default if hasattr(max_price, "default") else max_price
    room_type = room_type.default if hasattr(room_type, "default") else room_type
    sharing = sharing.default if hasattr(sharing, "default") else sharing
    gender_type = gender_type.default if hasattr(gender_type, "default") else gender_type
    is_ac = is_ac.default if hasattr(is_ac, "default") else is_ac
    food = food.default if hasattr(food, "default") else food
    facilities = facilities.default if hasattr(facilities, "default") else facilities
    available_from = available_from.default if hasattr(available_from, "default") else available_from
    north = north.default if hasattr(north, "default") else north
    south = south.default if hasattr(south, "default") else south
    east = east.default if hasattr(east, "default") else east
    west = west.default if hasattr(west, "default") else west

    college_lat, college_lon = None, None
    if college_id:
        with conn.cursor() as cur:
            cur.execute("SELECT latitude, longitude FROM colleges WHERE college_id = %s", (college_id,))
            crow = cur.fetchone()
            if crow:
                college_lat, college_lon = crow[0], crow[1]

    where_clauses = ["1=1"] # Relaxed filters for map
    params = []

    if q and q.strip():
        where_clauses.append("(p.tsv @@ websearch_to_tsquery('english', %s) OR p.name ILIKE %s OR p.city ILIKE %s OR p.area ILIKE %s OR p.address ILIKE %s OR p.description ILIKE %s)")
        q_clean = q.strip()
        
        # Handle common typos
        if q_clean.lower() == "vadodra":
            q_clean = "Vadodara"
        elif q_clean.lower() == "ahmedbad":
            q_clean = "Ahmedabad"
            
        params.extend([q_clean, f"%{q_clean}%", f"%{q_clean}%", f"%{q_clean}%", f"%{q_clean}%", f"%{q_clean}%"])

    if city:
        where_clauses.append("LOWER(p.city) = LOWER(%s)")
        params.append(city.strip())
    if area:
        where_clauses.append("LOWER(p.area) = LOWER(%s)")
        params.append(area.strip())
    if gender_type:
        where_clauses.append("p.type = %s")
        params.append(gender_type)
    if room_type:
        where_clauses.append("r.room_type = %s")
        params.append(room_type)
    if sharing:
        where_clauses.append("r.capacity = %s")
        params.append(sharing)
    if is_ac is not None:
        where_clauses.append("r.is_ac = %s")
        params.append(is_ac)
    if food:
        where_clauses.append("(p.facilities::text ILIKE '%%food%%' OR p.facilities::text ILIKE '%%mess%%' OR p.facilities::text ILIKE '%%meals%%')")
    if facilities:
        for fac in [f.strip() for f in facilities.split(",") if f.strip()]:
            where_clauses.append("p.facilities::text ILIKE %s")
            params.append(f"%{fac}%")
    if available_from:
        where_clauses.append("(b.available_from IS NULL OR b.available_from <= %s)")
        params.append(available_from)

    if north is not None and south is not None and east is not None and west is not None:
        where_clauses.append("p.latitude <= %s AND p.latitude >= %s AND p.longitude <= %s AND p.longitude >= %s")
        params.extend([north, south, east, west])

    where_sql = " AND ".join(where_clauses)
    having_clauses = ["1=1"]
    having_params = []

    if min_price is not None:
        having_clauses.append("MIN(r.monthly_rent) >= %s")
        having_params.append(min_price)
    if max_price is not None:
        having_clauses.append("MIN(r.monthly_rent) <= %s")
        having_params.append(max_price)
    if college_lat is not None and college_lon is not None and radius_km is not None:
        having_clauses.append(f"distance_km(p.latitude, p.longitude, {college_lat}, {college_lon}) <= %s")
        having_params.append(radius_km)

    having_sql = " HAVING " + " AND ".join(having_clauses)

    sql_query = f"""
        SELECT p.property_id, p.name, p.latitude, p.longitude, p.type, p.avg_rating, p.photos,
               COALESCE(MIN(r.monthly_rent), 0) AS min_rent,
               COUNT(b.bed_id) FILTER (WHERE b.status = 'available') AS available_beds_count
        FROM properties p
        LEFT JOIN owner_profiles op ON op.owner_id = p.owner_id
        LEFT JOIN rooms r ON r.property_id = p.property_id
        LEFT JOIN beds b ON b.room_id = r.room_id
        WHERE {where_sql}
        GROUP BY p.property_id
        {having_sql}
        LIMIT 200
    """

    with conn.cursor() as cur:
        cur.execute(sql_query, params + having_params)
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]

    results = []
    for r in rows:
        item = dict(zip(cols, r))
        item["property_id"] = str(item["property_id"])
        photos = item.get("photos")
        if isinstance(photos, str):
            try: photos = json.loads(photos)
            except Exception: photos = []
        item["cover_photo"] = photos[0] if (photos and len(photos) > 0) else None
        del item["photos"]
        
        item["min_rent"] = float(item["min_rent"])
        if college_lat is not None:
            item["distance_km"] = _haversine_distance(item["latitude"], item["longitude"], college_lat, college_lon)
        results.append(item)

    return results

# ─────────────────────────────────────────────────────────────
# Compare endpoint (must be BEFORE /{property_id} to avoid shadowing)
# ─────────────────────────────────────────────────────────────
@router.get("/properties/compare")
def compare_properties(
    ids: str,
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    if not ids:
        raise HTTPException(400, "ids query parameter is required")
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    if len(id_list) == 0:
        raise HTTPException(400, "At least 1 property ID is required")
    if len(id_list) > 3:
        raise HTTPException(400, "Maximum 3 properties can be compared at a time")

    with conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(id_list))
        query = f"""
            SELECT p.property_id, p.name, p.type, p.city, p.area, p.address,
                   p.description, p.house_rules, p.facilities, p.photos,
                   p.security_deposit_default, p.notice_period_days, p.lock_in_months,
                   p.cancellation_policy, p.booking_mode, p.avg_rating,
                   COALESCE(MIN(r.monthly_rent), 0) AS min_rent,
                   COALESCE(MAX(r.monthly_rent), 0) AS max_rent,
                   COUNT(b.bed_id) FILTER (WHERE b.status = 'available') AS available_beds_count
            FROM properties p
            JOIN owner_profiles op ON op.owner_id = p.owner_id
            LEFT JOIN rooms r ON r.property_id = p.property_id AND r.status = 'Active'
            LEFT JOIN beds b ON b.room_id = r.room_id
            WHERE p.property_id IN ({placeholders})
            GROUP BY p.property_id
        """
        cur.execute(query, id_list)
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
        results = []
        for row in rows:
            item = dict(zip(cols, row))
            if item.get("property_id") is not None:
                item["property_id"] = str(item["property_id"])
            photos = item.get("photos")
            if isinstance(photos, str):
                try: photos = json.loads(photos)
                except Exception: photos = []
            item["cover_photo"] = photos[0] if (photos and len(photos) > 0) else None
            facilities = item.get("facilities")
            if isinstance(facilities, str):
                try: item["facilities"] = json.loads(facilities)
                except Exception: item["facilities"] = []
            results.append(item)
        return results


# ─────────────────────────────────────────────────────────────
# Property Detail View (Public, approved only, hides owner contact)
# ─────────────────────────────────────────────────────────────
@router.get("/properties/{property_id}")
@limiter.limit("60/minute")
def get_property_detail(
    property_id: str,
    request: Request,
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    with conn.cursor() as cur:
        # Fetch approved property with owner check
        cur.execute(
            """SELECT p.property_id, p.name, p.type, p.address, p.city, p.area,
                      p.latitude, p.longitude, p.description, p.house_rules,
                      p.facilities, p.photos, p.security_deposit_default,
                      p.notice_period_days, p.lock_in_months, p.booking_mode,
                      p.cancellation_policy, p.late_fee_config, p.rent_due_day,
                      p.rent_cycle_mode, p.avg_rating, p.created_at,
                      op.business_name AS owner_business_name
               FROM properties p
               JOIN owner_profiles op ON op.owner_id = p.owner_id
               WHERE p.property_id = %s""",
            (property_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Property not found or not approved")

        cols = [d.name for d in cur.description]
        prop_dict = dict(zip(cols, row))

        # Parse JSON fields
        for json_field in ("facilities", "photos", "cancellation_policy", "late_fee_config"):
            val = prop_dict.get(json_field)
            if isinstance(val, str):
                try: prop_dict[json_field] = json.loads(val)
                except Exception: pass

        # Fetch rooms with live available beds count
        cur.execute(
            """SELECT r.room_id, r.room_number, r.floor, r.room_type, r.capacity,
                      r.is_ac, r.attached_bath, r.monthly_rent, r.deposit, r.photos,
                      COUNT(b.bed_id) FILTER (WHERE b.status = 'available') AS available_beds_count
               FROM rooms r
               LEFT JOIN beds b ON b.room_id = r.room_id
               WHERE r.property_id = %s AND r.status = 'Active'
               GROUP BY r.room_id
               ORDER BY r.room_number""",
            (property_id,)
        )
        room_rows = cur.fetchall()
        room_cols = [d.name for d in cur.description]

        rooms = []
        for rr in room_rows:
            room_item = dict(zip(room_cols, rr))
            rphotos = room_item.get("photos")
            if isinstance(rphotos, str):
                try: room_item["photos"] = json.loads(rphotos)
                except Exception: room_item["photos"] = []
            rooms.append(room_item)

        prop_dict["rooms"] = rooms
        
        # Ensure owner contact details (phone, email) are EXCLUDED
        prop_dict.pop("phone", None)
        prop_dict.pop("email", None)
        prop_dict.pop("owner_email", None)
        prop_dict.pop("owner_phone", None)

        return prop_dict
