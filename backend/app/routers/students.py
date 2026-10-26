from fastapi import APIRouter, Depends
import psycopg
from ..db.session import get_db_connection
from pydantic import BaseModel

router = APIRouter(prefix="/students", tags=["students"])

class GuardianConsent(BaseModel):
    version: str

@router.post("/guardian-consent")
def guardian_consent(req: GuardianConsent, conn: psycopg.Connection = Depends(get_db_connection)):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE student_profiles 
            SET guardian_consent_version = %s, guardian_consent_at = now(), guardian_verified = true
            WHERE student_id = (select nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid)
        """, (req.version,))
        conn.commit()
    return {"status": "consent recorded"}
