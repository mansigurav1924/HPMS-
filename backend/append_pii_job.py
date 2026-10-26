import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\services\tasks.py"

append_pii = """
@celery_app.task
def audit_pii_check_job():
    logger.info("Running audit PII check job")
    # basic regex for email and phone. We just check the json strings.
    # Note: we redact the keys 'email', 'phone' in audit.py, but values could leak in other keys.
    email_regex = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    phone_regex = r'\b\d{10}\b'
    
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # Check last 1 day of logs
            cur.execute(\"\"\"
                SELECT log_id, previous_value, new_value 
                FROM audit_logs 
                WHERE timestamp >= now() - interval '1 day'
            \"\"\")
            rows = cur.fetchall()
            violations = []
            
            for r in rows:
                content = str(r["previous_value"]) + " " + str(r["new_value"])
                if re.search(email_regex, content) or re.search(phone_regex, content):
                    violations.append(r["log_id"])
                    
            if violations:
                logger.error(f"PII leak detected in audit logs: {violations}")
                # Send alert to admin via in_app_notifications
                # We need an admin user id. We could broadcast to all admins.
                cur.execute("SELECT user_id FROM users WHERE role = 'admin'")
                admins = cur.fetchall()
                for admin in admins:
                    cur.execute(
                        "INSERT INTO in_app_notifications (user_id, title, body) VALUES (%s, %s, %s)",
                        (admin["user_id"], "PII Leak Alert", f"PII leak detected in audit logs: {violations}")
                    )
"""

with open(filepath, "a", encoding="utf-8") as f:
    f.write(append_pii)

print("Appended PII check job")
