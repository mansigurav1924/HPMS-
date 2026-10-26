from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
import httpx
import psycopg
import json
from jose import jwt
from ..config import settings
from ..db.session import get_db_connection
from ..services.audit import log_action
from supabase import create_client, Client
from ..core.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
supabase_admin: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

class SignupRequest(BaseModel):
    email: str
    password: str
    role: str
    name: str
    phone: Optional[str] = None
    dob: Optional[date] = None
    gender: Optional[str] = None
    consent_version: Optional[str] = None
    turnstile_token: str

async def verify_turnstile(token: str):
    """Skip CAPTCHA in development mode, verify in production."""
    if settings.ENV == "development" or settings.TURNSTILE_SECRET == "dummy-token-for-dev":
        return  # Skip in dev
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": settings.TURNSTILE_SECRET, "response": token}
        )
        if not res.json().get("success"):
            raise HTTPException(400, "CAPTCHA failed")

@router.post("/signup")
@limiter.limit("5/minute")
async def signup(request: Request, req: SignupRequest):
    await verify_turnstile(req.turnstile_token)

    try:
        auth_res = supabase.auth.sign_up({
            "email": req.email,
            "password": req.password,
            "options": {
                "data": {
                    "role": req.role,
                    "name": req.name
                }
            }
        })
    except Exception as e:
        raise HTTPException(400, f"Signup failed: {str(e)}")

    if not auth_res.user:
        raise HTTPException(400, "Signup failed: no user returned")

    user_id = auth_res.user.id

    # Update metadata via admin API as well to guarantee metadata persistence
    try:
        supabase_admin.auth.admin.update_user_by_id(user_id, {
            "user_metadata": {"role": req.role, "name": req.name}
        })
    except Exception:
        pass

    # Create profile row using admin service role (REST API over HTTPS)
    try:
        supabase_admin.table("users").insert({
            "user_id": user_id,
            "role": req.role,
            "name": req.name,
            "email": req.email,
            "phone": req.phone,
            "dob": req.dob.isoformat() if req.dob else None,
            "gender": req.gender,
            "consent_version": req.consent_version,
            "consented_at": datetime.now().isoformat() if req.consent_version else None
        }).execute()

        if req.role == 'student':
            supabase_admin.table("student_profiles").insert({"student_id": user_id}).execute()
        elif req.role == 'owner':
            supabase_admin.table("owner_profiles").insert({"owner_id": user_id}).execute()
    except Exception as e:
        # Ignore duplicate profile errors if already exists
        pass

    return {"message": "Signup successful. Check your email to confirm your account.", "user_id": user_id}

class LoginRequest(BaseModel):
    email: str
    password: str

@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, req: LoginRequest):
    try:
        auth_res = supabase.auth.sign_in_with_password({"email": req.email, "password": req.password})
    except Exception as e:
        raise HTTPException(401, f"Login failed: {str(e)}")

    if not auth_res.session:
        raise HTTPException(401, "Invalid credentials")

    user_id = auth_res.user.id
    user_role = auth_res.user.user_metadata.get("role")
    user_name = auth_res.user.user_metadata.get("name", req.email)

    # Fetch role and name from database users table if missing or fallback needed
    try:
        db_user = supabase_admin.table("users").select("role, name").eq("user_id", user_id).single().execute()
        if db_user and db_user.data:
            user_role = db_user.data.get("role") or user_role
            user_name = db_user.data.get("name") or user_name
    except Exception:
        pass

    return {
        "access_token": auth_res.session.access_token,
        "user_id": user_id,
        "name": user_name or req.email,
        "role": user_role or "student"
    }

@router.post("/password-reset")
def password_reset(email: str):
    supabase.auth.reset_password_email(email)
    return {"message": "Password reset email sent"}

@router.post("/mfa/enroll")
def mfa_enroll():
    return {"message": "Not implemented"}

@router.post("/mfa/verify")
def mfa_verify():
    return {"message": "Not implemented"}
