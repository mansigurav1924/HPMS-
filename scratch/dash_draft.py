import sys
import os
sys.path.append(os.getcwd() + '/backend')
from app.db.session import get_db_connection
import psycopg

def test_student_dash():
    db = next(get_db_connection())
    uid = '198dd97e-13c5-41e7-8b09-cf8f5a6b0c2a' # We need a real student UID
    with db.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE role = 'student' LIMIT 1")
        row = cur.fetchone()
        if not row:
            print("No student found")
            return
        uid = row[0]
        
        # 1. State Hero
        cur.execute("""
            SELECT t.tenancy_id, t.property_id, p.name as property_name, t.status, 
                   t.check_in_date, t.expected_checkout_date, t.agreement_url,
                   r.room_number, b.bed_number
            FROM tenancies t 
            JOIN properties p ON t.property_id = p.property_id
            LEFT JOIN rooms r ON t.room_id = r.room_id
            LEFT JOIN beds b ON t.bed_id = b.bed_id
            WHERE t.student_id = %s AND t.status IN ('active', 'notice')
            ORDER BY t.created_at DESC LIMIT 1
        """, (uid,))
        tenancy = cur.fetchone()
        
        # ... logic ...
        print("Tenancy:", tenancy)

if __name__ == '__main__':
    test_student_dash()
