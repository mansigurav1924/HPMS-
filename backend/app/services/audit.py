import psycopg
import json
import hashlib
from uuid import UUID
from datetime import datetime, date
from ..config import settings

class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)

def log_action(conn: psycopg.Connection, user_id: str, role: str, action: str, entity_type: str, entity_id: str, prev_val: dict = None, new_val: dict = None):
    pii_fields = {'name', 'email', 'phone', 'id_proof_url', 'guardian_name', 'guardian_phone', 'dob', 'address'}
    
    def redact(data: dict):
        if not data: return data
        d = {}
        for k, v in data.items():
            if k in pii_fields and v is not None:
                h = hashlib.sha256(f"{v}{settings.HASH_PEPPER}".encode()).hexdigest()
                d[k] = f"[redacted] sha256:{h}"
            else:
                d[k] = v
        return d
        
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO audit_logs (user_id, role, action, entity_type, entity_id, previous_value, new_value)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                user_id, 
                role, 
                action, 
                entity_type, 
                entity_id, 
                json.dumps(redact(prev_val), cls=CustomEncoder) if prev_val else None, 
                json.dumps(redact(new_val), cls=CustomEncoder) if new_val else None
            )
        )
