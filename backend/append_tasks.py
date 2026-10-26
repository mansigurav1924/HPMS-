import re

filepath = r"C:\Users\mgura\OneDrive\Desktop\internship\HPMS_portal\backend\app\services\tasks.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Add send_notification_task and send_notice_fanout to tasks.py
append_tasks = """
@celery_app.task(bind=True, max_retries=5)
def send_notification_task(self, user_id, template_id, recipient_contact, context, subject):
    from .notifications import NotificationService, NotificationError
    logger.info(f"Executing send_notification_task for user_id={user_id}, template_id={template_id}")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        svc = NotificationService(conn)
        try:
            svc._send_sync(user_id, template_id, recipient_contact, context, subject, retry_count=self.request.retries)
        except NotificationError as e:
            # Handle error and backoff
            logger.error(f"Notification sending failed: {e}")
            if self.request.retries < self.max_retries:
                # Exponential backoff
                countdown = 2 ** self.request.retries
                raise self.retry(exc=e, countdown=countdown)
            else:
                # Mark failed in DB
                with conn.cursor() as cur:
                    sub = subject or template_id
                    cur.execute(
                        \"\"\"INSERT INTO notification_logs 
                           (user_id, template_id, recipient, subject, status, error, retry_count) 
                           VALUES (%s, %s, %s, %s, 'failed', %s, %s)\"\"\",
                        (user_id, template_id, recipient_contact, sub, str(e), self.request.retries)
                    )

@celery_app.task
def send_notice_fanout(notice_id: str, property_id: str, title: str, body: str):
    logger.info(f"Fan-out notice {notice_id} for property {property_id}")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(\"\"\"
                SELECT DISTINCT u.user_id, u.email
                FROM tenancies t
                JOIN bookings b ON t.booking_id = b.booking_id
                JOIN users u ON b.student_id = u.user_id
                WHERE t.property_id = %s AND t.status IN ('active', 'notice')
            \"\"\", (property_id,))
            tenants = cur.fetchall()
            
            from .notifications import NotificationService
            svc = NotificationService(conn)
            
            for t in tenants:
                context = {
                    "property_name": "Property", # ideally fetch property name
                    "notice_title": title,
                    "notice_body": body,
                    "link": f"/notices/{notice_id}"
                }
                # Use celery task inside to actually send
                send_notification_task.delay(t["user_id"], "notice_announcement", t["email"], context, title)

@celery_app.task
def review_request_job():
    logger.info("Running review request job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 3 days after check-out OR 30 days of stay, once per tenancy
            # Only if review doesn't already exist
            cur.execute(\"\"\"
                SELECT t.tenancy_id, u.user_id, u.email, u.name, p.name as prop_name
                FROM tenancies t
                JOIN bookings b ON t.booking_id = b.booking_id
                JOIN users u ON b.student_id = u.user_id
                JOIN properties p ON t.property_id = p.property_id
                LEFT JOIN reviews r ON r.tenancy_id = t.tenancy_id
                WHERE r.review_id IS NULL
                  AND (
                      (t.status = 'checked_out' AND current_date >= t.last_stay_date + interval '3 days')
                      OR
                      (t.status IN ('active', 'notice') AND current_date >= t.start_date + interval '30 days')
                  )
            \"\"\")
            for row in cur.fetchall():
                # Note: to prevent sending this every day until they review, 
                # we could log to notification_logs and check if we already sent review_request for this tenancy.
                # Let's check notification_logs to see if we already sent a review request
                cur.execute(
                    "SELECT 1 FROM notification_logs WHERE user_id = %s AND template_id = 'review_request' AND status != 'failed'",
                    (row["user_id"],)
                )
                if not cur.fetchone():
                    context = {
                        "student_name": row["name"],
                        "property_name": row["prop_name"],
                        "link": f"/my-tenancy"
                    }
                    send_notification_task.delay(row["user_id"], "review_request", row["email"], context, "Review your stay")
"""

with open(filepath, "a", encoding="utf-8") as f:
    f.write(append_tasks)

print("Tasks appended")
