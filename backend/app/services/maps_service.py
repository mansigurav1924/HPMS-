import httpx
import json
import logging
import urllib.parse
from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional, Tuple
from ..config import settings
from ..db.session import get_public_db_connection

logger = logging.getLogger(__name__)

class GeocodeResponse(BaseModel):
    lat: float
    lng: float
    formatted_address: str
    city: str = ""
    area: str = ""

def _normalize_address(address: str) -> str:
    return address.strip().lower()

def _init_cache_table(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.geocode_cache (
                id SERIAL PRIMARY KEY,
                query_key TEXT UNIQUE NOT NULL,
                lat NUMERIC(10, 7) NOT NULL,
                lng NUMERIC(10, 7) NOT NULL,
                formatted_address TEXT,
                city TEXT,
                area TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
            );
            """)
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to init geocode_cache table: {e}")
        conn.rollback()

def _get_from_cache(query_key: str) -> Optional[GeocodeResponse]:
    # Using public db connection to avoid RLS issues for cache
    conn_gen = get_public_db_connection()
    conn = next(conn_gen)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lat, lng, formatted_address, city, area FROM public.geocode_cache WHERE query_key = %s", (query_key,))
            row = cur.fetchone()
            if row:
                return GeocodeResponse(
                    lat=float(row[0]),
                    lng=float(row[1]),
                    formatted_address=row[2],
                    city=row[3] or "",
                    area=row[4] or ""
                )
    except Exception as e:
        # Table might not exist yet
        if "relation \"public.geocode_cache\" does not exist" in str(e):
            conn.rollback()
            _init_cache_table(conn)
        else:
            logger.error(f"Cache read error: {e}")
            conn.rollback()
    finally:
        conn.close()
    return None

def _set_in_cache(query_key: str, data: GeocodeResponse):
    conn_gen = get_public_db_connection()
    conn = next(conn_gen)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.geocode_cache (query_key, lat, lng, formatted_address, city, area)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (query_key) DO UPDATE SET
                    lat = EXCLUDED.lat,
                    lng = EXCLUDED.lng,
                    formatted_address = EXCLUDED.formatted_address,
                    city = EXCLUDED.city,
                    area = EXCLUDED.area,
                    created_at = now()
            """, (
                query_key, data.lat, data.lng, data.formatted_address, data.city, data.area
            ))
        conn.commit()
    except Exception as e:
        if "relation \"public.geocode_cache\" does not exist" in str(e):
            conn.rollback()
            _init_cache_table(conn)
            # Try again once
            _set_in_cache(query_key, data)
        else:
            logger.error(f"Cache write error: {e}")
            conn.rollback()
    finally:
        conn.close()

async def geocode(address: str) -> GeocodeResponse:
    if not settings.GOOGLE_MAPS_SERVER_KEY:
        raise HTTPException(status_code=503, detail="Geocoding service is not configured")
        
    normalized = _normalize_address(address)
    query_key = f"geo:{normalized}"
    
    cached = _get_from_cache(query_key)
    if cached:
        return cached

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address,
        "key": settings.GOOGLE_MAPS_SERVER_KEY
    }
    
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Geocoding API error: {e}")
            raise HTTPException(status_code=502, detail="Geocoding service unavailable")
            
    if data.get("status") != "OK":
        logger.error(f"Geocoding failed with status: {data.get('status')}")
        raise HTTPException(status_code=400, detail="Address not found")
        
    result = data["results"][0]
    location = result["geometry"]["location"]
    
    lat = location["lat"]
    lng = location["lng"]
    formatted_address = result["formatted_address"]
    
    city = ""
    area = ""
    for comp in result["address_components"]:
        types = comp["types"]
        if "locality" in types:
            city = comp["long_name"]
        elif "sublocality_level_1" in types or "sublocality" in types or "neighborhood" in types:
            if not area:
                area = comp["long_name"]
                
    resp = GeocodeResponse(
        lat=lat,
        lng=lng,
        formatted_address=formatted_address,
        city=city,
        area=area
    )
    
    _set_in_cache(query_key, resp)
    return resp

async def reverse_geocode(lat: float, lng: float) -> GeocodeResponse:
    if not settings.GOOGLE_MAPS_SERVER_KEY:
        raise HTTPException(status_code=503, detail="Geocoding service is not configured")
        
    query_key = f"rev:{lat:.5f},{lng:.5f}"
    cached = _get_from_cache(query_key)
    if cached:
        return cached

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "latlng": f"{lat},{lng}",
        "key": settings.GOOGLE_MAPS_SERVER_KEY
    }
    
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Reverse geocoding API error: {e}")
            raise HTTPException(status_code=502, detail="Geocoding service unavailable")
            
    if data.get("status") != "OK" or not data.get("results"):
        raise HTTPException(status_code=400, detail="Location not found")
        
    result = data["results"][0]
    location = result["geometry"]["location"]
    
    formatted_address = result["formatted_address"]
    
    city = ""
    area = ""
    for comp in result["address_components"]:
        types = comp["types"]
        if "locality" in types:
            city = comp["long_name"]
        elif "sublocality_level_1" in types or "sublocality" in types or "neighborhood" in types:
            if not area:
                area = comp["long_name"]
                
    resp = GeocodeResponse(
        lat=location["lat"],
        lng=location["lng"],
        formatted_address=formatted_address,
        city=city,
        area=area
    )
    
    _set_in_cache(query_key, resp)
    return resp
