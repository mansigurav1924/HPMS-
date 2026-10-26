SELECT t.tenancy_id, t.property_id, p.name as property_name, t.status, 
       t.check_in_date, t.expected_checkout_date, t.agreement_url,
       r.room_number, b.bed_label
FROM tenancies t 
JOIN properties p ON t.property_id = p.property_id
LEFT JOIN beds b ON t.bed_id = b.bed_id
LEFT JOIN rooms r ON b.room_id = r.room_id
WHERE t.student_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' AND t.status IN ('active', 'notice')
ORDER BY t.check_in_date DESC NULLS LAST LIMIT 1;

SELECT b.booking_id, b.property_id, p.name as property_name, b.status,
       b.requested_at, (b.requested_at + interval '48 hours') as owner_response_due_at,
       b.move_in_date, p.latitude, p.longitude, u.name as owner_name, u.phone as owner_phone,
       r.room_number, bd.bed_label
FROM bookings b
JOIN properties p ON b.property_id = p.property_id
JOIN users u ON p.owner_id = u.user_id
LEFT JOIN rooms r ON b.room_id = r.room_id
LEFT JOIN beds bd ON b.bed_id = bd.bed_id
WHERE b.student_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba'
ORDER BY b.requested_at DESC LIMIT 1;

SELECT email_verified, phone_verified FROM users WHERE user_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba';

SELECT id_proof_url, college, guardian_name, guardian_phone
FROM student_profiles WHERE student_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba';

SELECT complaint_id, resolved_at FROM complaints 
WHERE student_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' AND status = 'resolved' AND student_rating IS NULL
ORDER BY resolved_at DESC LIMIT 1;

SELECT t.tenancy_id, t.property_id, p.name as property_name
FROM tenancies t
JOIN properties p ON t.property_id = p.property_id
LEFT JOIN reviews r ON t.property_id = r.property_id AND r.student_id = t.student_id
WHERE t.student_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' AND t.status = 'checked_out' AND r.review_id IS NULL
ORDER BY t.actual_checkout_date DESC LIMIT 1;

SELECT COUNT(*) FROM complaints WHERE student_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' AND status NOT IN ('resolved', 'closed');

SELECT complaint_id, category as title, status, created_at FROM complaints 
WHERE student_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' ORDER BY created_at DESC LIMIT 3;

SELECT COUNT(*) FROM in_app_notifications WHERE user_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' AND read_at IS NULL;

SELECT id as notification_id, title, body as message, created_at, read_at FROM in_app_notifications 
WHERE user_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' ORDER BY created_at DESC LIMIT 5;

SELECT notice_id, title, posted_at, expires_at
FROM notices WHERE property_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' AND expires_at > now()
ORDER BY posted_at DESC LIMIT 3;

SELECT w.property_id, p.name, p.city, p.status
FROM wishlists w JOIN properties p ON w.property_id = p.property_id
WHERE w.user_id = 'd0107291-7667-4d69-a36a-f0f8a9ee07ba' ORDER BY w.added_at DESC LIMIT 3;
