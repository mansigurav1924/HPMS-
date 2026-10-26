"""
Owner KYC router — POST /owners/kyc, GET /owners/me/profile
"""
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Depends
import psycopg
import json

from ..db.session import get_db_connection, require_role, get_uid
from ..services.audit import log_action
from ..services.storage import validate_file, upload_file, get_signed_url, secure_filename
from ..services.notifications import send_notification

router = APIRouter(prefix="/owners", tags=["owners"])


@router.post("/kyc")
async def submit_kyc(
    request: Request,
    id_proof: UploadFile = File(...),
    auth_doc: UploadFile = File(...),
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    # Validate both files (KYC allows pdf too)
    for f in (id_proof, auth_doc):
        content = await f.read()
        try:
            validate_file(content, len(content), allow_pdf=True)
        except ValueError as e:
            raise HTTPException(422, str(e))
        await f.seek(0)

    # Upload id_proof
    id_bytes = await id_proof.read()
    id_path = f"{uid}/id_proof_{secure_filename(id_proof.filename)}"
    upload_file("kyc-docs", id_path, id_bytes, id_proof.content_type)

    # Upload auth doc
    auth_bytes = await auth_doc.read()
    auth_path = f"{uid}/auth_doc_{secure_filename(auth_doc.filename)}"
    upload_file("kyc-docs", auth_path, auth_bytes, auth_doc.content_type)

    kyc_urls = json.dumps({"id_proof": id_path, "auth_doc": auth_path})

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE owner_profiles
               SET kyc_document_url = %s, verification_status = 'pending_verification'
               WHERE owner_id = %s
               RETURNING owner_id""",
            (kyc_urls, uid)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Owner profile not found")
    conn.commit()

    log_action(conn, uid, "owner", "KYC_SUBMITTED", "owner_profiles", uid,
               None, {"kyc_document_url": "[redacted]"})

    return {"message": "KYC submitted for review"}


@router.get("/me/profile")
def get_my_profile(
    request: Request,
    conn: psycopg.Connection = Depends(get_db_connection),
):
    claims = require_role(request, "owner")
    uid = get_uid(claims)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT op.owner_id, op.business_name, op.verification_status,
                      op.rejection_reason, u.name, u.email
               FROM owner_profiles op
               JOIN users u ON u.user_id = op.owner_id
               WHERE op.owner_id = %s""",
            (uid,)
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(404, "Owner profile not found")

    return {
        "owner_id": row[0],
        "business_name": row[1],
        "verification_status": row[2],
        "rejection_reason": row[3],
        "name": row[4],
        "email": row[5],
    }
