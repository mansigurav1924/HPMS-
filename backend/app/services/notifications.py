import os
import json
import logging
import psycopg
from psycopg.rows import dict_row
from typing import Optional, Any
from ..config import settings
import re

logger = logging.getLogger(__name__)

class NotificationError(Exception):
    pass

class EmailProvider:
    def send(self, to: str, subject: str, body: str):
        # Stub for dev, real implementation depends on env
        if settings.ENV == "test":
            pass # testing env
        else:
            logger.info(f"Sending Email to {to} | Subject: {subject} | Body: {body}")
            # If SendGrid/SMTP were configured, they would go here

class SmsProvider:
    def send(self, to: str, body: str):
        logger.info(f"Sending SMS to {to} | Body: {body}")

class NotificationService:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        self.email_provider = EmailProvider()
        self.sms_provider = SmsProvider()

    def get_preferences(self, user_id: str) -> dict:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT email_enabled, sms_enabled, in_app_enabled FROM notification_preferences WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                return row
            return {"email_enabled": True, "sms_enabled": False, "in_app_enabled": True}

    def render(self, body_template: str, context: dict) -> str:
        # Find all {{var}} placeholders
        placeholders = re.findall(r'\{\{([a-zA-Z0-9_]+)\}\}', body_template)
        rendered = body_template
        for p in placeholders:
            if p not in context:
                # Missing variable raises clear error in tests (and fails fast)
                raise NotificationError(f"Missing variable '{p}' for template")
            rendered = rendered.replace(f"{{{{{p}}}}}", str(context[p]))
        return rendered

    def send(self, user_id: str, template_id: str, recipient_contact: str, context: dict, subject: Optional[str] = None):
        """Queue the notification via Celery task (or send synchronously if testing)"""
        from .tasks import send_notification_task
        if settings.ENV == "test":
            self._send_sync(user_id, template_id, recipient_contact, context, subject)
        else:
            send_notification_task.delay(user_id, template_id, recipient_contact, context, subject)

    def _send_sync(self, user_id: str, template_id: str, recipient_contact: str, context: dict, subject: Optional[str] = None, retry_count: int = 0):
        with self.conn.cursor(row_factory=dict_row) as cur:
            # 1. Fetch template
            cur.execute("SELECT name, channel, body FROM notification_templates WHERE template_id = %s AND is_active", (template_id,))
            template = cur.fetchone()
            if not template:
                return  # Template not found or inactive
            
            # 2. Render
            body = self.render(template["body"], context)
            sub = subject or template["name"]
            
            # 3. Preferences & Suppression
            prefs = self.get_preferences(user_id)
            channel = template["channel"]
            
            # If in-app enabled OR critical (we treat all templates seeded as 'critical' enough for in-app unless disabled, wait requirement says "critical templates always sent in-app" so we just create it)
            if prefs["in_app_enabled"] or template_id in ["guardian_consent_request", "notice_announcement", "checkout_settlement"]:
                # Create in-app notification
                cur.execute(
                    "INSERT INTO in_app_notifications (user_id, title, body, link) VALUES (%s, %s, %s, %s)",
                    (user_id, sub, body, context.get("link"))
                )
                
            if channel == "email" and prefs["email_enabled"]:
                # Check suppression
                cur.execute("SELECT reason FROM suppression_list WHERE email = %s", (recipient_contact,))
                suppressed = cur.fetchone()
                if suppressed:
                    cur.execute(
                        """INSERT INTO notification_logs 
                           (user_id, template_id, recipient, subject, status, error, retry_count) 
                           VALUES (%s, %s, %s, %s, 'suppressed', %s, %s)""",
                        (user_id, template_id, recipient_contact, sub, f"Suppressed: {suppressed['reason']}", retry_count)
                    )
                    return
                
                try:
                    self.email_provider.send(recipient_contact, sub, body)
                    cur.execute(
                        """INSERT INTO notification_logs 
                           (user_id, template_id, recipient, subject, status, retry_count) 
                           VALUES (%s, %s, %s, %s, 'sent', %s)""",
                        (user_id, template_id, recipient_contact, sub, retry_count)
                    )
                except Exception as e:
                    raise NotificationError(str(e))
            elif channel == "sms" and prefs["sms_enabled"]:
                try:
                    self.sms_provider.send(recipient_contact, body)
                    cur.execute(
                        """INSERT INTO notification_logs 
                           (user_id, template_id, recipient, subject, status, retry_count) 
                           VALUES (%s, %s, %s, %s, 'sent', %s)""",
                        (user_id, template_id, recipient_contact, sub, retry_count)
                    )
                except Exception as e:
                    raise NotificationError(str(e))

# Backward compatible stub for old endpoints
def send_notification(conn, user_id, template_id, recipient_email, context, subject=None):
    svc = NotificationService(conn)
    svc.send(user_id, template_id, recipient_email, context, subject)
