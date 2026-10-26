from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
import psycopg
from app.db.session import get_db_connection
from app.db.session import require_role, get_uid

router = APIRouter(prefix="/dashboard", tags=["dashboards"])

@router.get("/owner")
def get_owner_dashboard(
    request: Request,
    property_id: Optional[str] = None,
    db: psycopg.Connection = Depends(get_db_connection)
):
    import time
    start_time = time.time()
    claims = require_role(request, "owner", "warden")
    uid = get_uid(claims)
    role = claims.get("role", "")
    
    try:
        with db.cursor() as cur:
            filter_str = ""
            params = []
            
            if role == "warden":
                filter_str = "property_id IN (SELECT property_id FROM property_staff WHERE user_id = %s)"
                params.append(uid)
            else:
                filter_str = "owner_id = %s"
                params.append(uid)
                
            if property_id:
                filter_str += " AND property_id = %s"
                params.append(property_id)
            
            # 1. SUMMARY STATS
            cur.execute(f"""
                SELECT 
                    SUM(total_beds) as total_beds,
                    SUM(vacant_beds) as vacant_beds,
                    SUM(occupied_beds) as occupied_beds,
                    SUM(reserved_beds) as reserved_beds,
                    SUM(maintenance_beds) as maintenance_beds,
                    SUM(open_complaints) as open_complaints,
                    SUM(pending_booking_requests) as pending_booking_requests,
                    SUM(upcoming_checkouts) as upcoming_checkouts,
                    SUM(flagged_no_shows) as flagged_no_shows
                FROM owner_dashboard_stats
                WHERE {filter_str}
            """, params)
            stats = cur.fetchone()
            
            if not stats or stats[0] is None:
                # No properties or data
                return {"data": {}}
                
            total_beds = int(stats[0] or 0)
            vacant_beds = int(stats[1] or 0)
            occupied_beds = int(stats[2] or 0)
            reserved_beds = int(stats[3] or 0)
            maintenance_beds = int(stats[4] or 0)
            open_complaints = int(stats[5] or 0)
            pending_requests = int(stats[6] or 0)
            upcoming_checkouts = int(stats[7] or 0)
            
            occupancy_rate = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0.0
            
            # For properties chart
            cur.execute(f"""
                SELECT property_id, property_name, total_beds, occupied_beds 
                FROM owner_dashboard_stats 
                WHERE {filter_str}
                ORDER BY property_name
            """, params)
            property_occupancy = []
            for row in cur.fetchall():
                property_occupancy.append({
                    "property_id": row[0],
                    "name": row[1],
                    "total": int(row[2] or 0),
                    "occupied": int(row[3] or 0)
                })

            # a) Booking requests waiting approval
            cur.execute(f"""
                SELECT b.booking_id, b.property_id, p.name as property_name, b.status, b.requested_at, 
                       (b.requested_at + interval '48 hours') as owner_response_due_at,
                       COALESCE(u.name, 'Student Resident') as student_name
                FROM bookings b
                LEFT JOIN users u ON b.student_id = u.user_id
                JOIN properties p ON b.property_id = p.property_id
                WHERE b.status = 'requested' AND b.property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                ORDER BY b.requested_at ASC
                LIMIT 5
            """, params)
            booking_requests = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]
            soonest_response_due_at = booking_requests[0]['owner_response_due_at'] if booking_requests else None

            # b) Complaints attention
            cur.execute(f"""
                SELECT c.complaint_id, c.property_id, p.name as property_name, c.status, c.priority, c.created_at, c.title, c.escalation_level
                FROM complaints c
                JOIN properties p ON c.property_id = p.property_id
                WHERE c.status IN ('open', 'in_progress', 'reopened', 'acknowledged')
                AND c.property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                ORDER BY c.escalation_level DESC, c.created_at ASC
                LIMIT 5
            """, params)
            complaints_attention = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]
            breached_complaints = sum(1 for c in complaints_attention if c['escalation_level'] > 0)

            # c) Ready for check-in
            cur.execute(f"""
                SELECT b.booking_id, b.property_id, p.name as property_name, b.move_in_date, COALESCE(u.name, 'Student Resident') as student_name
                FROM bookings b
                LEFT JOIN users u ON b.student_id = u.user_id
                JOIN properties p ON b.property_id = p.property_id
                WHERE b.status = 'approved' AND b.move_in_date <= CURRENT_DATE + 7
                AND b.property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                ORDER BY b.move_in_date ASC
                LIMIT 5
            """, params)
            ready_for_checkin = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]

            # d) No-shows
            cur.execute(f"""
                SELECT b.booking_id, b.property_id, p.name as property_name, b.move_in_date, COALESCE(u.name, 'Student Resident') as student_name
                FROM bookings b
                LEFT JOIN users u ON b.student_id = u.user_id
                JOIN properties p ON b.property_id = p.property_id
                WHERE b.status = 'no_show'
                AND b.property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                ORDER BY b.move_in_date ASC
                LIMIT 5
            """, params)
            no_shows = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]

            # e) Notice tenants
            cur.execute(f"""
                SELECT t.tenancy_id, t.property_id, p.name as property_name, t.expected_checkout_date as last_stay_date, COALESCE(u.name, 'Student Resident') as student_name
                FROM tenancies t
                LEFT JOIN users u ON t.student_id = u.user_id
                JOIN properties p ON t.property_id = p.property_id
                WHERE t.status = 'notice'
                AND t.property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                ORDER BY t.expected_checkout_date ASC
                LIMIT 5
            """, params)
            notice_tenants = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]

            # f) Pending verification
            cur.execute(f"""
                SELECT property_id, name as property_name, status 
                FROM properties 
                WHERE status = 'Pending' 
                AND property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                LIMIT 5
            """, params)
            pending_verifications = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]

            # Overview: Funnel
            cur.execute(f"""
                SELECT status, COUNT(*) as count
                FROM bookings
                WHERE requested_at >= CURRENT_DATE - interval '30 days'
                AND property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                GROUP BY status
            """, params)
            funnel_rows = cur.fetchall()
            booking_funnel = {"requested": 0, "approved": 0, "confirmed": 0, "rejected": 0, "expired": 0, "cancelled": 0}
            for row in funnel_rows:
                if row[0] in booking_funnel:
                    booking_funnel[row[0]] = int(row[1])

            # Overview: Reviews
            cur.execute(f"""
                SELECT r.review_id, r.rating, r.comment, r.created_at, COALESCE(u.name, 'Student Reviewer') as student_name, r.owner_reply
                FROM reviews r
                LEFT JOIN users u ON r.student_id = u.user_id
                WHERE r.property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                ORDER BY r.created_at DESC
                LIMIT 5
            """, params)
            latest_reviews = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]

            cur.execute(f"""
                SELECT AVG(rating) as avg_rating, COUNT(*) FILTER (WHERE owner_reply IS NULL) as missing_replies
                FROM reviews
                WHERE property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
            """, params)
            review_stats = cur.fetchone()
            avg_rating = round(float(review_stats[0] or 0), 1) if review_stats and review_stats[0] is not None else 0.0
            missing_replies = int(review_stats[1] or 0) if review_stats else 0

            # Recent activity
            cur.execute(f"""
                SELECT 'booking' as type, booking_id as id, requested_at as ts, 'New booking request' as description
                FROM bookings
                WHERE property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                UNION ALL
                SELECT 'complaint' as type, complaint_id as id, created_at as ts, 'New complaint: ' || title as description
                FROM complaints
                WHERE property_id IN (SELECT property_id FROM owner_dashboard_stats WHERE {filter_str})
                ORDER BY ts DESC
                LIMIT 10
            """, params * 2)
            recent_activity = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]

            response_data = {
                "summary": {
                    "total_beds": total_beds,
                    "occupied_beds": occupied_beds,
                    "vacant_beds": vacant_beds,
                    "occupancy_rate": occupancy_rate,
                    "pending_requests": pending_requests,
                    "soonest_response_due_at": soonest_response_due_at,
                    "open_complaints": open_complaints,
                    "breached_complaints": breached_complaints,
                    "upcoming_checkouts": upcoming_checkouts
                },
                "needs_attention": {
                    "booking_requests": booking_requests,
                    "complaints": complaints_attention,
                    "ready_for_checkin": ready_for_checkin,
                    "no_shows": no_shows,
                    "notice_tenants": notice_tenants,
                    "pending_verifications": pending_verifications
                },
                "overview": {
                    "property_occupancy": property_occupancy,
                    "bed_status": {
                        "available": vacant_beds,
                        "reserved": reserved_beds,
                        "occupied": occupied_beds,
                        "maintenance": maintenance_beds
                    },
                    "booking_funnel": booking_funnel,
                    "latest_reviews": latest_reviews,
                    "avg_rating": avg_rating,
                    "missing_replies": missing_replies,
                    "recent_activity": recent_activity
                },
                "timing_ms": round((time.time() - start_time) * 1000, 2)
            }
            print(f"Owner dashboard responded in {response_data['timing_ms']} ms")
            return {"data": response_data}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"data": {}, "error": str(e)}

@router.get("/admin")
def get_admin_dashboard(
    request: Request,
    db: psycopg.Connection = Depends(get_db_connection)
):
    require_role(request, "admin")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM admin_dashboard_stats WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return {"data": {}}
        columns = [desc[0] for desc in cur.description]
        return {"data": dict(zip(columns, row))}

@router.get("/student")
def get_student_dashboard(
    request: Request,
    db: psycopg.Connection = Depends(get_db_connection)
):
    # Student dashboard stats (active tenancies, open complaints, notices, unread notifications)
    claims = require_role(request, "student")
    uid = get_uid(claims)
    with db.cursor() as cur:
        # 1. Tenancy state
        cur.execute("""
            SELECT t.tenancy_id, t.property_id, p.name as property_name, t.status, 
                   t.check_in_date, t.expected_checkout_date, t.agreement_url,
                   r.room_number, b.bed_label
            FROM tenancies t 
            JOIN properties p ON t.property_id = p.property_id
            LEFT JOIN beds b ON t.bed_id = b.bed_id
            LEFT JOIN rooms r ON b.room_id = r.room_id
            WHERE t.student_id = %s AND t.status IN ('active', 'notice')
            ORDER BY t.check_in_date DESC NULLS LAST LIMIT 1
        """, (uid,))
        tenancy_row = cur.fetchone()
        tenancy = dict(zip([d[0] for d in cur.description], tenancy_row)) if tenancy_row else None
        
        # 2. Booking state (if no active tenancy)
        booking = None
        if not tenancy:
            cur.execute("""
                SELECT b.booking_id, b.property_id, p.name as property_name, b.status,
                       b.requested_at, (b.requested_at + interval '48 hours') as owner_response_due_at,
                       b.move_in_date, p.latitude, p.longitude, u.name as owner_name, u.phone as owner_phone,
                       r.room_number, bd.bed_label
                FROM bookings b
                JOIN properties p ON b.property_id = p.property_id
                JOIN users u ON p.owner_id = u.user_id
                LEFT JOIN rooms r ON b.room_id = r.room_id
                LEFT JOIN beds bd ON b.bed_id = bd.bed_id
                WHERE b.student_id = %s
                ORDER BY b.requested_at DESC LIMIT 1
            """, (uid,))
            booking_row = cur.fetchone()
            booking = dict(zip([d[0] for d in cur.description], booking_row)) if booking_row else None

        # 3. Needs attention
        # - user verified
        cur.execute("SELECT email_verified, phone_verified FROM users WHERE user_id = %s", (uid,))
        user_row = cur.fetchone()
        email_ver = user_row[0] if user_row else False
        phone_ver = user_row[1] if user_row else False
        
        # - profile complete
        cur.execute("""
            SELECT id_proof_url, college, guardian_name, guardian_phone
            FROM student_profiles WHERE student_id = %s
        """, (uid,))
        prof_row = cur.fetchone()
        profile_incomplete = not prof_row or not all(prof_row[:4])
        
        minor_missing_consent = False # Fields don't exist in DB

        # - resolved complaints waiting for rating
        cur.execute("""
            SELECT complaint_id, resolved_at FROM complaints 
            WHERE student_id = %s AND status = 'resolved' AND student_rating IS NULL
            ORDER BY resolved_at DESC LIMIT 1
        """, (uid,))
        complaint_to_rate_row = cur.fetchone()
        complaint_to_rate = dict(zip(['complaint_id', 'resolved_at'], complaint_to_rate_row)) if complaint_to_rate_row else None
        
        # - unreviewed checkouts
        cur.execute("""
            SELECT t.tenancy_id, t.property_id, p.name as property_name
            FROM tenancies t
            JOIN properties p ON t.property_id = p.property_id
            LEFT JOIN reviews r ON t.property_id = r.property_id AND r.student_id = t.student_id
            WHERE t.student_id = %s AND t.status = 'checked_out' AND r.review_id IS NULL
            ORDER BY t.actual_checkout_date DESC LIMIT 1
        """, (uid,))
        unreviewed_checkout_row = cur.fetchone()
        unreviewed_checkout = dict(zip(['tenancy_id', 'property_id', 'property_name'], unreviewed_checkout_row)) if unreviewed_checkout_row else None

        # 4. Widgets
        # Complaints
        cur.execute("SELECT COUNT(*) FROM complaints WHERE student_id = %s AND status NOT IN ('resolved', 'closed')", (uid,))
        open_complaints = cur.fetchone()[0]
        cur.execute("""
            SELECT complaint_id, category as title, status, created_at FROM complaints 
            WHERE student_id = %s ORDER BY created_at DESC LIMIT 3
        """, (uid,))
        complaints = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

        # Notifications
        cur.execute("SELECT COUNT(*) FROM in_app_notifications WHERE user_id = %s AND read_at IS NULL", (uid,))
        unread_notifications = cur.fetchone()[0]
        cur.execute("""
            SELECT id as notification_id, title, body as message, created_at, read_at FROM in_app_notifications 
            WHERE user_id = %s ORDER BY created_at DESC LIMIT 5
        """, (uid,))
        notifications = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

        # Notices
        notices = []
        if tenancy:
            cur.execute("""
                SELECT notice_id, title, posted_at, expires_at
                FROM notices WHERE property_id = %s AND expires_at > now()
                ORDER BY posted_at DESC LIMIT 3
            """, (tenancy['property_id'],))
            notices = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

        # Wishlist
        cur.execute("""
            SELECT w.property_id, p.name, p.city, p.status
            FROM wishlists w JOIN properties p ON w.property_id = p.property_id
            WHERE w.user_id = %s ORDER BY w.added_at DESC LIMIT 3
        """, (uid,))
        wishlist = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

        return {
            "data": {
                "hero": {
                    "tenancy": tenancy,
                    "booking": booking
                },
                "attention": {
                    "minor_missing_consent": minor_missing_consent,
                    "email_verified": email_ver,
                    "phone_verified": phone_ver,
                    "profile_incomplete": profile_incomplete,
                    "complaint_to_rate": complaint_to_rate,
                    "unreviewed_checkout": unreviewed_checkout
                },
                "widgets": {
                    "open_complaints": open_complaints,
                    "complaints": complaints,
                    "unread_notifications": unread_notifications,
                    "notifications": notifications,
                    "notices": notices,
                    "wishlist": wishlist
                },
                "payments_enabled": False
            }
        }

@router.post("/refresh")
def refresh_dashboards(
    request: Request,
    db: psycopg.Connection = Depends(get_db_connection)
):
    require_role(request, "admin")
    # Note: normally Celery beat refreshes this every 15 min. This is a manual trigger.
    with db.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY owner_dashboard_stats")
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY admin_dashboard_stats")
    db.commit()
    return {"status": "success", "message": "Dashboards refreshed"}
