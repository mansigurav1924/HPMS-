"""
Colleges router — public read-only and admin CRUD.
"""
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field, model_validator
import psycopg
from ..db.session import get_public_db_connection, get_db_connection, require_role, get_uid
from ..services.audit import log_action

router = APIRouter(prefix="/colleges", tags=["colleges"])

class CollegeCreateRequest(BaseModel):
    name: str
    city: str
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)

    @model_validator(mode="after")
    def check_coords(self):
        if self.latitude == 0 and self.longitude == 0:
            raise ValueError("Coordinates cannot be (0, 0)")
        return self

class CollegeUpdateRequest(BaseModel):
    name: str
    city: str
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)

    @model_validator(mode="after")
    def check_coords(self):
        if self.latitude == 0 and self.longitude == 0:
            raise ValueError("Coordinates cannot be (0, 0)")
        return self


@router.get("")
def list_colleges(
    city: str = None,
    conn: psycopg.Connection = Depends(get_public_db_connection),
):
    with conn.cursor() as cur:
        if city:
            cur.execute(
                "SELECT college_id, name, city, latitude, longitude FROM colleges WHERE lower(city) = lower(%s) ORDER BY name",
                (city,)
            )
        else:
            cur.execute(
                "SELECT college_id, name, city, latitude, longitude FROM colleges ORDER BY city, name"
            )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.post("")
def create_college(
    request: Request,
    data: CollegeCreateRequest,
    conn: psycopg.Connection = Depends(get_db_connection)
):
    admin_uid = get_uid(require_role(request, "admin"))
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "INSERT INTO colleges (name, city, latitude, longitude) VALUES (%s, %s, %s, %s) RETURNING *",
            (data.name, data.city, data.latitude, data.longitude)
        )
        new_college = cur.fetchone()
        log_action(conn, admin_uid, "admin", "CREATE_COLLEGE", "colleges", str(new_college['college_id']), None, new_college)
        conn.commit()
        return new_college


@router.put("/{college_id}")
def update_college(
    request: Request,
    college_id: str,
    data: CollegeUpdateRequest,
    conn: psycopg.Connection = Depends(get_db_connection)
):
    admin_uid = get_uid(require_role(request, "admin"))
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "UPDATE colleges SET name = %s, city = %s, latitude = %s, longitude = %s WHERE college_id = %s RETURNING *",
            (data.name, data.city, data.latitude, data.longitude, college_id)
        )
        updated = cur.fetchone()
        if not updated:
            raise HTTPException(404, "College not found")
        log_action(conn, admin_uid, "admin", "UPDATE_COLLEGE", "colleges", college_id, None, updated)
        conn.commit()
        return updated


@router.delete("/{college_id}")
def delete_college(
    request: Request,
    college_id: str,
    conn: psycopg.Connection = Depends(get_db_connection)
):
    admin_uid = get_uid(require_role(request, "admin"))
    with conn.cursor() as cur:
        # Check if properties reference the college
        cur.execute("SELECT 1 FROM properties WHERE college_id = %s LIMIT 1", (college_id,))
        if cur.fetchone():
            raise HTTPException(409, "Cannot delete college because properties are associated with it")
            
        cur.execute("DELETE FROM colleges WHERE college_id = %s RETURNING college_id", (college_id,))
        if not cur.fetchone():
            raise HTTPException(404, "College not found")
            
        log_action(conn, admin_uid, "admin", "DELETE_COLLEGE", "colleges", college_id, None, None)
        conn.commit()
        return {"status": "success"}
