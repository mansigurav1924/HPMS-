import psycopg
import logging
from ..core.celery import celery_app
from ..config import settings
import json

logger = logging.getLogger(__name__)

@celery_app.task
def refresh_dashboards_job():
    logger.info("Refreshing materialized views for dashboards")
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY owner_dashboard_stats")
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY admin_dashboard_stats")

@celery_app.task
def purge_expired_records(dry_run=False):
    logger.info(f"Purging expired records (dry_run={dry_run})")
    stats = {}
    with psycopg.connect(settings.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # 1. Purge notification_logs
            if dry_run:
                cur.execute("SELECT COUNT(*) FROM notification_logs WHERE retention_expires_at <= now()")
                stats["notification_logs_to_purge"] = cur.fetchone()[0]
            else:
                cur.execute("DELETE FROM notification_logs WHERE retention_expires_at <= now()")
                stats["notification_logs_purged"] = cur.rowcount

            # 2. Purge webhook_events
            if dry_run:
                cur.execute("SELECT COUNT(*) FROM webhook_events WHERE retention_expires_at <= now()")
                stats["webhook_events_to_purge"] = cur.fetchone()[0]
            else:
                cur.execute("DELETE FROM webhook_events WHERE retention_expires_at <= now()")
                stats["webhook_events_purged"] = cur.rowcount
            
            # 3. Purge expired notices
            if dry_run:
                cur.execute("SELECT COUNT(*) FROM notices WHERE expires_at <= now() - interval '6 months'")
                stats["notices_to_purge"] = cur.fetchone()[0]
            else:
                cur.execute("DELETE FROM notices WHERE expires_at <= now() - interval '6 months'")
                stats["notices_purged"] = cur.rowcount
            
            # 4. Inactive accounts flagging
            cur.execute("""
                SELECT user_id, email, phone FROM users 
                WHERE status = 'Active' AND created_at <= now() - interval '24 months'
                AND NOT EXISTS (
                    SELECT 1 FROM tenancies WHERE student_id = users.user_id AND status IN ('active', 'notice')
                )
            """)
            inactive_users = cur.fetchall()
            stats["inactive_accounts_flagged"] = len(inactive_users)
            
            if not dry_run and inactive_users:
                # set status
                for u in inactive_users:
                    cur.execute("UPDATE users SET status = 'Inactive_Flagged' WHERE user_id = %s", (u[0],))
                    from .notifications import send_notification
                    # Assume notification_templates has 'Account Inactivity Notice' seeded
                    send_notification(conn, u[0], "account_inactivity", u[1] or u[2], {"name": "User"})
            
            cur.execute("""
                INSERT INTO audit_logs (actor_id, action, entity_type, old_data, new_data)
                VALUES (NULL, 'purge_expired_records', 'system', NULL, %s)
            """, (json.dumps({"dry_run": dry_run, "stats": stats}),))
            
        conn.commit()
    return stats
