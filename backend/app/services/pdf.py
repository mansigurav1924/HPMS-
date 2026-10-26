import logging
import psycopg
import os
import tempfile
from ..core.celery import celery_app
from ..config import settings
from .pdf_generator import generate_rental_agreement
from .storage import upload_file

logger = logging.getLogger(__name__)

@celery_app.task(bind=True, max_retries=3)
def generate_and_upload_agreement(self, tenancy_id: str):
    logger.info(f"Generating agreement for tenancy {tenancy_id}")
    try:
        conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.rent_amount, t.deposit_paid, t.check_in_date, t.expected_checkout_date,
                       p.name, p.notice_period_days, p.lock_in_months, p.house_rules, p.gate_timing,
                       u_owner.name, u_tenant.name, b.bed_label, r.room_number
                FROM tenancies t
                JOIN properties p ON t.property_id = p.property_id
                JOIN users u_owner ON p.owner_id = u_owner.user_id
                JOIN users u_tenant ON t.student_id = u_tenant.user_id
                JOIN beds b ON t.bed_id = b.bed_id
                JOIN rooms r ON b.room_id = r.room_id
                WHERE t.tenancy_id = %s
            """, (tenancy_id,))
            row = cur.fetchone()
            if not row:
                raise Exception("Tenancy not found")
            
            context = {
                "rent_amount": row[0],
                "deposit_amount": row[1] or 0,
                "check_in_date": row[2],
                "expected_checkout": row[3],
                "property_name": row[4],
                "notice_period": row[5] or 30,
                "lock_in": row[6] or 0,
                "house_rules": row[7] or "None",
                "gate_timing": row[8] or "No restrictions",
                "owner_name": row[9],
                "tenant_name": row[10],
                "bed_name": row[11],
                "room_name": row[12],
            }
        
        # Temp file for PDF
        fd, temp_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        
        # Generate PDF
        generate_rental_agreement(context, temp_path)
        
        # Upload
        # Simulate upload by placing it in the private bucket and getting URL
        # For this test, we can use a mock path or actual storage upload if it exists
        remote_path = f"agreements/{tenancy_id}.pdf"
        
        # Upload to agreements bucket
        with open(temp_path, "rb") as f:
            pdf_bytes = f.read()
            
        path = upload_file("agreements", f"{tenancy_id}.pdf", pdf_bytes, "application/pdf")
        
        # Save path
        with psycopg.connect(settings.DATABASE_URL, autocommit=True) as update_conn:
            with update_conn.cursor() as ucur:
                ucur.execute("UPDATE tenancies SET agreement_url = %s WHERE tenancy_id = %s", (path, tenancy_id))
                
        # Cleanup
        try:
            os.unlink(temp_path)
        except:
            pass
            
    except Exception as exc:
        logger.error(f"Failed to generate agreement for {tenancy_id}: {exc}")
        # Mark as failed in DB so owner can retry
        with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE tenancies SET agreement_url = 'FAILED' WHERE tenancy_id = %s", (tenancy_id,))
        raise self.retry(exc=exc, countdown=60)
