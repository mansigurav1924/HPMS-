import logging
import psycopg
from datetime import date
from ..core.celery import celery_app
from ..config import settings
from .notifications import send_notification

logger = logging.getLogger(__name__)

@celery_app.task
def checkin_instructions_job():
    logger.info("Running checkin instructions job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            # 1 day before move_in_date
            cur.execute("""
                SELECT b.booking_id, b.student_id, u.email, p.name, b.move_in_date
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                JOIN users u ON b.student_id = u.user_id
                WHERE b.status = 'confirmed' 
                  AND b.move_in_date = current_date + interval '1 day'
            """)
            for row in cur.fetchall():
                send_notification(conn, row[1], "checkin_instructions", row[2], {
                    "property_name": row[3], "move_in_date": str(row[4])
                }, "Move-in Instructions")

@celery_app.task
def auto_complete_notice():
    logger.info("Running auto complete notice job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            # past last_stay_date + 1 day
            cur.execute("""
                SELECT t.tenancy_id, p.owner_id, u.email, t.student_id
                FROM tenancies t
                JOIN properties p ON t.property_id = p.property_id
                JOIN users u ON p.owner_id = u.user_id
                WHERE t.status = 'notice' 
                  AND current_date >= t.last_stay_date + interval '1 day'
                  AND t.cancel_reason IS DISTINCT FROM 'flagged_for_checkout'
            """)
            for row in cur.fetchall():
                # Flag it
                cur.execute("UPDATE tenancies SET cancel_reason = 'flagged_for_checkout' WHERE tenancy_id = %s", (row[0],))
                # Notify owner
                send_notification(conn, row[1], "tenant_checkout_ready", row[2], {
                    "tenancy_id": row[0]
                }, "Tenant ready for checkout")

@celery_app.task
def no_show_flagging():
    logger.info("Running no show flagging job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.booking_id, p.owner_id, u.email, b.move_in_date, COALESCE(p.no_show_days, 3)
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                JOIN users u ON p.owner_id = u.user_id
                WHERE b.status = 'confirmed'
            """)
            for row in cur.fetchall():
                b_id, owner_id, email, move_in_date, no_show_days = row
                if date.today() >= move_in_date + type(move_in_date).resolution * no_show_days:
                    # Mark booking as no_show
                    cur.execute("UPDATE bookings SET cancel_reason = 'no_show_flagged' WHERE booking_id = %s", (b_id,))
                    send_notification(conn, owner_id, "booking_no_show", email, {
                        "booking_id": b_id
                    }, "Booking No-Show Flagged")

@celery_app.task
def reconciliation_job():
    logger.info("Running payment reconciliation job")
    from .razorpay_service import razorpay_service
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # Pending older than 30 minutes
            cur.execute("""
                SELECT payment_id, gateway_order_id, status 
                FROM payments 
                WHERE status = 'Pending' AND gateway_order_id IS NOT NULL
                  AND created_at <= now() - interval '30 minutes'
            """)
            payments = cur.fetchall()
            
            for p in payments:
                # fetch order payments from Razorpay
                order_id = p['gateway_order_id']
                try:
                    gw_payments = razorpay_service.fetch_order_payments(order_id)
                except Exception as e:
                    logger.error(f"Error fetching order {order_id}: {e}")
                    continue
                
                captured = [gp for gp in gw_payments if gp.get("status") == "captured"]
                if captured:
                    gw_pay = captured[0]
                    cur.execute("SELECT apply_successful_payment(%s, %s, now())", (p['payment_id'], gw_pay['id']))
                else:
                    # check if order is expired or all failed
                    # for simplicity, we mark failed
                    cur.execute("UPDATE payments SET status = 'Failed' WHERE payment_id = %s", (p['payment_id'],))
                    
            logger.info(f"Reconciled {len(payments)} payments")

@celery_app.task(bind=True, max_retries=3)
def generate_receipt_pdf(self, payment_id: str):
    logger.info(f"Generating receipt for payment {payment_id}")
    try:
        from .pdf_generator import WEASYPRINT_AVAILABLE, generate_rental_agreement # we can reuse or just do mock
        # For this phase we just mock receipt generation in a similar way if weasyprint is not available
        import tempfile
        from google.cloud import storage # or supabase storage
        
        with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT p.*, u.name as payer_name, u.email
                    FROM payments p
                    JOIN users u ON p.payer_id = u.user_id
                    WHERE p.payment_id = %s
                """, (payment_id,))
                payment = cur.fetchone()
                
                if not payment:
                    return
                    
                receipt_no = f"RCT-{str(payment_id).split('-')[0]}"
                url = f"https://mock-storage.com/receipts/{receipt_no}.pdf"
                
                cur.execute("UPDATE payments SET receipt_url = %s WHERE payment_id = %s", (url, payment_id))
                
                send_notification(conn, payment['payer_id'], "payment_receipt", payment['email'], {
                    "receipt_url": url,
                    "amount": payment['amount']
                }, "Payment Receipt")
                
    except Exception as exc:
        logger.error(f"Generate receipt failed: {exc}")
        self.retry(exc=exc, countdown=60)

@celery_app.task
def generate_rent_dues_job(month: str = None):
    """
    Monthly job: generate rent_dues for a given month (default = current month).
    Idempotent — safe to call multiple times.
    Runs at 00:30 IST on the 1st of each month via Celery beat.

    Args:
        month: ISO date string (YYYY-MM-DD). Defaults to today's month start.
    """
    import datetime
    logger.info(f"Running generate_rent_dues_job for month={month or 'current'}")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            target_month = month or datetime.date.today().replace(day=1).isoformat()
            cur.execute("SELECT generate_rent_dues(%s::date)", (target_month,))
            count = cur.fetchone()[0]
            logger.info(f"generate_rent_dues: inserted {count} new dues for {target_month}")


@celery_app.task
def flag_overdue_dues_job():
    """
    Daily job: flag dues where status is Due/Partial and due_date + grace < today.
    Also applies late fees if enabled on the property. Idempotent.
    Runs at 01:00 IST daily via Celery beat.
    """
    logger.info("Running flag_overdue_dues_job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT flag_overdue_dues()")
            count = cur.fetchone()[0]
            logger.info(f"flag_overdue_dues: flagged {count} dues")


@celery_app.task
def send_rent_reminders_job():
    """
    Daily job at 09:00 IST: sends rent reminder emails.
    Three stages per due (tracked via reminder_stage to prevent duplicates):
      - 'upcoming' : X days before due_date (property setting, default 3)
      - 'due_today': on due_date
      - 'overdue'  : after due_date + grace_days

    Sends a copy to the owner for overdue notices.
    """
    logger.info("Running send_rent_reminders_job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            import datetime
            today = datetime.date.today()

            # Fetch all unsettled dues with tenancy/property info
            cur.execute("""
                SELECT
                    rd.due_id, rd.month, rd.due_date, rd.total_amount,
                    rd.amount_paid, rd.status, rd.reminder_stage,
                    t.student_id, t.property_id,
                    u.name  AS tenant_name,  u.email AS tenant_email,
                    ou.email AS owner_email, p.owner_id,
                    p.name  AS property_name,
                    COALESCE((p.late_fee_config->>'grace_days')::int, 3) AS grace_days,
                    COALESCE((p.late_fee_config->>'reminder_days_before')::int, 3) AS reminder_days
                FROM rent_dues rd
                JOIN tenancies   t  ON t.tenancy_id  = rd.tenancy_id
                JOIN properties  p  ON p.property_id = t.property_id
                JOIN users       u  ON u.user_id      = t.student_id
                JOIN users       ou ON ou.user_id     = p.owner_id
                WHERE rd.status NOT IN ('Paid', 'Waived')
            """)
            dues = cur.fetchall()

            sent = 0
            for d in dues:
                days_before_due = (d["due_date"] - today).days
                past_grace      = (today - d["due_date"]).days > d["grace_days"]
                stage           = d["reminder_stage"]

                ctx = {
                    "tenant_name":   d["tenant_name"],
                    "amount":        float(d["total_amount"] - d["amount_paid"]),
                    "month":         d["month"].strftime("%B %Y") if hasattr(d["month"], "strftime") else str(d["month"]),
                    "property_name": d["property_name"],
                    "due_date":      str(d["due_date"]),
                }

                new_stage = None
                if days_before_due == d["reminder_days"] and stage != "upcoming":
                    send_notification(conn, d["student_id"], "rent_due_reminder", d["tenant_email"], ctx, "Rent Due Reminder")
                    new_stage = "upcoming"
                    sent += 1
                elif days_before_due == 0 and stage not in ("upcoming", "due_today"):
                    send_notification(conn, d["student_id"], "rent_due_reminder", d["tenant_email"],
                                      {**ctx, "due_date": "TODAY"}, "Rent Due Today")
                    new_stage = "due_today"
                    sent += 1
                elif past_grace and stage != "overdue":
                    # Student notice
                    send_notification(conn, d["student_id"], "rent_overdue_notice", d["tenant_email"], ctx, "Rent Overdue")
                    # Owner copy
                    send_notification(conn, d["owner_id"], "rent_due_owner_copy", d["owner_email"], ctx, "Tenant Rent Overdue")
                    new_stage = "overdue"
                    sent += 1

                if new_stage:
                    cur.execute("UPDATE rent_dues SET reminder_stage = %s WHERE due_id = %s",
                                (new_stage, d["due_id"]))

            logger.info(f"send_rent_reminders_job: sent {sent} notifications")




# ─── Phase 8: Complaint background jobs ──────────────────────────────────────

@celery_app.task
def escalate_overdue_complaints_job():
    """
    Run every 30 minutes (idempotent).
    Calls the DB function escalate_overdue_complaints() which:
      - Level 1: notifies warden/owner when ack or resolve SLA is breached.
      - Level 2: notifies admin when complaint is still unresolved at 2× SLA.
    Sends notifications for newly escalated complaints only (never duplicates).
    """
    logger.info("Running escalate_overdue_complaints_job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # Run the escalation function and get counts
            cur.execute("SELECT escalate_overdue_complaints()")
            result = cur.fetchone()["escalate_overdue_complaints"]
            l1 = result.get("l1_escalated", 0)
            l2 = result.get("l2_escalated", 0)
            logger.info(f"escalate_overdue_complaints: l1={l1} l2={l2}")

            # Send notifications for complaints that just hit level 1
            if l1 > 0:
                cur.execute("""
                    SELECT
                        c.complaint_id, c.priority,
                        p.owner_id,   ou.email AS owner_email,  ou.name AS owner_name,
                        p.name AS property_name
                    FROM complaints c
                    JOIN properties p  ON p.property_id = c.property_id
                    JOIN users      ou ON ou.user_id     = p.owner_id
                    WHERE c.escalation_level = 1
                      AND c.escalated_at >= now() - interval '31 minutes'
                """)
                for row in cur.fetchall():
                    send_notification(
                        conn, row["owner_id"], "complaint_escalated_owner",
                        row["owner_email"], {
                            "complaint_id":   str(row["complaint_id"]),
                            "property_name":  row["property_name"],
                            "priority":       row["priority"],
                            "owner_name":     row["owner_name"],
                        }, "Complaint Escalated"
                    )

            # Send notifications for complaints that just hit level 2
            if l2 > 0:
                cur.execute("""
                    SELECT
                        c.complaint_id, c.priority,
                        p.name AS property_name
                    FROM complaints c
                    JOIN properties p ON p.property_id = c.property_id
                    WHERE c.escalation_level = 2
                      AND c.escalated_at >= now() - interval '31 minutes'
                """)
                # Notify all admins
                cur2_conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
                with cur2_conn.cursor(row_factory=psycopg.rows.dict_row) as cur2:
                    cur2.execute("SELECT user_id, email FROM users WHERE role = 'admin'")
                    admins = cur2.fetchall()

                for c_row in cur.fetchall():
                    for admin in admins:
                        send_notification(
                            conn, admin["user_id"], "complaint_escalated_admin",
                            admin["email"], {
                                "complaint_id":  str(c_row["complaint_id"]),
                                "property_name": c_row["property_name"],
                                "priority":      c_row["priority"],
                            }, "Complaint Escalated to Admin"
                        )
                cur2_conn.close()


@celery_app.task
def auto_close_resolved_complaints_job():
    """
    Daily at 02:00 IST (20:30 UTC previous day).
    Auto-closes complaints resolved longer than their property's reopen_window_days
    with no reopening. Idempotent.
    """
    logger.info("Running auto_close_resolved_complaints_job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT auto_close_resolved_complaints()")
            count = cur.fetchone()[0]
            logger.info(f"auto_close_resolved_complaints: closed {count} complaints")


@celery_app.task
def complaint_sla_reminder_job():
    """
    Daily at 09:00 IST (03:30 UTC).
    Sends a reminder to the assigned staff member for each complaint that is
    within 25% of its SLA deadline and hasn't been sent a reminder yet.
    Uses a simple heuristic: complaint is 'approaching SLA' when elapsed time
    is > 75% of resolve_sla_hours. Sends once per complaint (tracked via a
    reminder flag stored in complaint_updates with visibility='internal').
    """
    logger.info("Running complaint_sla_reminder_job")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT
                    c.complaint_id, c.priority, c.assigned_to,
                    c.created_at, p.complaint_sla,
                    u.email AS assignee_email, u.name AS assignee_name
                FROM complaints c
                JOIN properties p ON p.property_id = c.property_id
                LEFT JOIN users u ON u.user_id = c.assigned_to
                WHERE c.status NOT IN ('closed', 'resolved')
                  AND c.assigned_to IS NOT NULL
                  -- Not already reminded: no internal SLA_REMINDER update
                  AND NOT EXISTS (
                      SELECT 1 FROM complaint_updates cu
                      WHERE cu.complaint_id = c.complaint_id
                        AND cu.visibility = 'internal'
                        AND cu.message LIKE 'SLA_REMINDER%'
                  )
            """)
            for row in cur.fetchall():
                sla   = row["complaint_sla"] or {}
                rh    = (sla.get("resolve_sla_hours") or {}).get(row["priority"], 72)
                # If > 75% of SLA has elapsed, send a reminder
                elapsed = (
                    __import__("datetime").datetime.utcnow().replace(tzinfo=__import__("datetime").timezone.utc)
                    - row["created_at"]
                ).total_seconds() / 3600
                if elapsed >= rh * 0.75:
                    send_notification(
                        conn, row["assigned_to"], "complaint_sla_reminder",
                        row["assignee_email"], {
                            "complaint_id":   str(row["complaint_id"]),
                            "priority":       row["priority"],
                            "assignee_name":  row["assignee_name"],
                        }, "Complaint SLA Reminder"
                    )
                    # Mark so we don't resend
                    cur.execute("""
                        INSERT INTO complaint_updates
                            (complaint_id, author_id, visibility, message)
                        VALUES (%s, %s, 'internal', 'SLA_REMINDER sent')
                    """, (str(row["complaint_id"]), row["assigned_to"]))
            logger.info("complaint_sla_reminder_job: done")


@celery_app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    from celery.schedules import crontab
    # Existing jobs
    sender.add_periodic_task(crontab(minute=0, hour=0), checkin_instructions_job.s(), name='Daily checkin instructions')
    sender.add_periodic_task(crontab(minute=0, hour=0), auto_complete_notice.s(), name='Daily notice completion')
    sender.add_periodic_task(crontab(minute=0, hour=0), no_show_flagging.s(), name='Daily no-show flagging')
    sender.add_periodic_task(crontab(minute='*/15'), reconciliation_job.s(), name='Every 15 min reconciliation')

    # Phase 7 jobs (all times in UTC; IST = UTC+5:30)
    # Monthly rent generation: 1st of month at 00:30 IST = 19:00 UTC on last day / use 1st 00:00 UTC as approx
    sender.add_periodic_task(crontab(minute=0, hour=19, day_of_month=28),  # ~00:30 IST 1st
                             generate_rent_dues_job.s(), name='Monthly rent due generation')
    # Daily overdue flagging: 01:00 IST = 19:30 UTC previous day
    sender.add_periodic_task(crontab(minute=30, hour=19),
                             flag_overdue_dues_job.s(), name='Daily overdue flagging')
    # Daily reminders: 09:00 IST = 03:30 UTC
    sender.add_periodic_task(crontab(minute=30, hour=3),
                             send_rent_reminders_job.s(), name='Daily rent reminders')

    # Phase 8 jobs
    # Escalation: every 30 minutes (idempotent)
    sender.add_periodic_task(
        crontab(minute='*/30'),
        escalate_overdue_complaints_job.s(),
        name='Every 30 min complaint escalation'
    )
    # Auto-close: daily at 02:00 IST = 20:30 UTC previous day
    sender.add_periodic_task(
        crontab(minute=30, hour=20),
        auto_close_resolved_complaints_job.s(),
        name='Daily auto-close resolved complaints'
    )
    # SLA reminder: daily at 09:00 IST = 03:30 UTC
    sender.add_periodic_task(
        crontab(minute=30, hour=3),
        complaint_sla_reminder_job.s(),
        name='Daily complaint SLA reminders'
    )

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
                        """INSERT INTO notification_logs 
                           (user_id, template_id, recipient, subject, status, error, retry_count) 
                           VALUES (%s, %s, %s, %s, 'failed', %s, %s)""",
                        (user_id, template_id, recipient_contact, sub, str(e), self.request.retries)
                    )

@celery_app.task
def send_notice_fanout(notice_id: str, property_id: str, title: str, body: str):
    logger.info(f"Fan-out notice {notice_id} for property {property_id}")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT DISTINCT u.user_id, u.email
                FROM tenancies t
                JOIN bookings b ON t.booking_id = b.booking_id
                JOIN users u ON b.student_id = u.user_id
                WHERE t.property_id = %s AND t.status IN ('active', 'notice')
            """, (property_id,))
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
            cur.execute("""
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
            """)
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

@celery_app.task
def audit_pii_check_job():
    logger.info("Running audit PII check job")
    # basic regex for email and phone. We just check the json strings.
    # Note: we redact the keys 'email', 'phone' in audit.py, but values could leak in other keys.
    email_regex = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    phone_regex = r'\d{10}'
    
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # Check last 1 day of logs
            cur.execute("""
                SELECT log_id, previous_value, new_value 
                FROM audit_logs 
                WHERE timestamp >= now() - interval '1 day'
            """)
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
