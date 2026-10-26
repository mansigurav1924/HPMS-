-- Update expire_overdue_holds to respect pending payments

CREATE OR REPLACE FUNCTION expire_overdue_holds()
RETURNS int LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_count int;
BEGIN
    UPDATE bookings 
    SET status = 'expired'
    WHERE status IN ('requested', 'approved', 'payment_pending')
      AND hold_expires_at < now()
      AND NOT EXISTS (
          SELECT 1 FROM payments p 
          WHERE p.booking_id = bookings.booking_id 
            AND p.status = 'Pending' 
            AND p.created_at >= now() - interval '30 minutes'
      );
      
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END $$;
