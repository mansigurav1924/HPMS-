"""
Shared dependency: extract JWT claims from the current DB session.
"""
import json
from fastapi import Request, HTTPException
import psycopg
from jose import jwt, JWTError
from typing import Generator
from ..config import settings


def _decode_token(request: Request) -> dict:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header.split(" ")[1]
    try:
        claims = jwt.decode(
            token, settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"], audience="authenticated",
            options={"verify_signature": False, "verify_aud": False}
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return claims


def get_db_connection(request: Request) -> Generator[psycopg.Connection, None, None]:
    claims = _decode_token(request)
    conn = psycopg.connect(settings.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL role authenticated")
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps(claims),)
            )
        yield conn
    finally:
        conn.close()


def get_public_db_connection() -> Generator[psycopg.Connection, None, None]:
    """
    JWT-free DB connection for public endpoints (search, property detail, colleges).
    Uses the postgres superuser connection (service role URL) but does NOT set RLS user context.
    The queries themselves filter by p.status = 'Approved' / op.verification_status = 'approved'.
    """
    conn = psycopg.connect(settings.DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def require_role(request: Request, *roles: str) -> dict:
    """Decode token and enforce role. Returns claims dict."""
    claims = _decode_token(request)
    
    app_meta = claims.get("app_metadata", {})
    user_meta = claims.get("user_metadata", {})
    user_role = user_meta.get("role") or app_meta.get("role") or claims.get("role", "")
    
    if user_role not in roles:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' not allowed here")
        
    if user_role == "admin":
        if claims.get("aal") != "aal2":
            raise HTTPException(status_code=403, detail="MFA is required for admin actions (aal2)")
            
    return claims


def get_uid(claims) -> str:
    if isinstance(claims, Request):
        claims = _decode_token(claims)
    return claims.get("sub", "") if isinstance(claims, dict) else ""
