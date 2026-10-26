from fastapi import APIRouter
from typing import Optional
import psycopg
import random
import uuid
import json
from datetime import date
from app.config import settings

router = APIRouter(tags=["seed"])

HOSTEL_NAMES = ["Sunrise PG", "Elite Hostel", "Comfort Stay", "Student Nest", "Premium Living", "Urban PG", "Green View", "Blue Sky Hostel", "Cozy Rooms", "Royal PG"]
AREAS = ["Kothrud", "Wakad", "Hinjewadi", "Viman Nagar", "Baner", "Koregaon Park", "Aundh", "Shivaji Nagar", "Katraj", "Karve Nagar"]
CITIES = ["Pune", "Mumbai", "Delhi", "Bangalore", "Hyderabad"]
FACILITIES = ["Wi-Fi", "AC", "Washing Machine", "Meals", "Security", "Cleaning", "Parking", "Gym"]

@router.get("/seed/100")
def seed_100_properties():
    try:
        sql_path = r"C:\Users\mgura\.gemini\antigravity-ide\brain\8b3a509b-3ed3-489d-bf94-2e3f562cfdd6\scratch\fake_data\HPMS_properties_rooms_beds_seed.sql"
        with open(sql_path, "r", encoding="utf-8") as f:
            sql_script = f.read()

        conn = psycopg.connect(settings.DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute(sql_script)
            conn.commit()
        conn.close()
            
        return {"message": "Successfully executed the uploaded SQL seed file!"}
    except Exception as e:
        return {"error": str(e)}

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from seed_demo_data import seed_demo_data

@router.get("/seed/demo")
def seed_demo_endpoint():
    try:
        seed_demo_data()
        return {"message": "Successfully executed seed_demo_data()"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/seed/check-status")
def seed_check_status():
    try:
        conn = psycopg.connect(settings.DATABASE_URL)
        with conn.cursor() as cur:
            mansi_id = "2d409d6d-9790-4e61-a394-50c9143d5f3f"
            
            # Count bookings for Mansi's properties grouped by status
            cur.execute("""
                SELECT b.status, count(*)
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                WHERE p.owner_id = %s
                GROUP BY b.status
            """, (mansi_id,))
            bookings_by_status = dict(cur.fetchall())

            # Count bookings with user join
            cur.execute("""
                SELECT b.status, count(*)
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                JOIN users u ON b.student_id = u.user_id
                WHERE p.owner_id = %s
                GROUP BY b.status
            """, (mansi_id,))
            bookings_with_users_by_status = dict(cur.fetchall())

            # Count tenancies for Mansi
            cur.execute("""
                SELECT t.status, count(*)
                FROM tenancies t
                JOIN properties p ON t.property_id = p.property_id
                WHERE p.owner_id = %s
                GROUP BY t.status
            """, (mansi_id,))
            tenancies_by_status = dict(cur.fetchall())

            # Check if there are other bookings in DB
            cur.execute("SELECT count(*), count(DISTINCT p.owner_id) FROM bookings b JOIN properties p ON b.property_id = p.property_id")
            total_bookings, total_booking_owners = cur.fetchone()

            # Check total properties in DB
            cur.execute("SELECT count(*), count(DISTINCT owner_id) FROM properties")
            total_props, total_prop_owners = cur.fetchone()

        conn.close()
        return {
            "mansi_id": mansi_id,
            "bookings_by_status": bookings_by_status,
            "bookings_with_users_by_status": bookings_with_users_by_status,
            "tenancies_by_status": tenancies_by_status,
            "total_bookings_in_db": total_bookings,
            "total_booking_owners": total_booking_owners,
            "total_props_in_db": total_props,
            "total_prop_owners": total_prop_owners
        }
    except Exception as e:
        return {"error": str(e)}


