"""
Supabase Storage helper — upload/delete via the supabase-py client using the service role key
so that server-side operations bypass RLS on storage.objects.
"""
from supabase import create_client
from ..config import settings
import re

# Service-role client — used ONLY for storage operations server-side (never exposed to browser)
_svc = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_KYC_TYPES   = ALLOWED_IMAGE_TYPES | {"application/pdf"}
MAX_SIZE_BYTES = 5 * 1024 * 1024   # 5 MB


import uuid
import os

def sniff_mime_type(data: bytes) -> str:
    if data.startswith(b'\xFF\xD8\xFF'): return "image/jpeg"
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return "image/png"
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP': return "image/webp"
    if data.startswith(b'%PDF'): return "application/pdf"
    return "application/octet-stream"

def validate_file(data: bytes, size: int, allow_pdf: bool = False):
    actual_mime = sniff_mime_type(data)
    allowed = ALLOWED_KYC_TYPES if allow_pdf else ALLOWED_IMAGE_TYPES
    if actual_mime not in allowed:
        raise ValueError(f"File type {actual_mime!r} not allowed. Allowed: {allowed}")
    if size > MAX_SIZE_BYTES:
        raise ValueError(f"File size {size} exceeds 5 MB limit")
        
def secure_filename(original_name: str) -> str:
    ext = os.path.splitext(original_name)[1].lower()
    return f"{uuid.uuid4().hex}{ext}"


def upload_file(bucket: str, path: str, data: bytes, content_type: str) -> str:
    """Upload bytes to Supabase Storage. Returns the public/signed URL path."""
    _svc.storage.from_(bucket).upload(
        path, data, file_options={"content-type": content_type, "upsert": "true"}
    )
    return path


def get_public_url(bucket: str, path: str) -> str:
    return _svc.storage.from_(bucket).get_public_url(path)


def get_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    res = _svc.storage.from_(bucket).create_signed_url(path, expires_in)
    return res.get("signedURL") or res.get("signedUrl", "")


def delete_file(bucket: str, path: str):
    _svc.storage.from_(bucket).remove([path])
