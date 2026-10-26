-- Phase 3 Migration: Full-text search tsvector, distance_km function, indexes

-- Haversine distance function in kilometers
CREATE OR REPLACE FUNCTION distance_km(
  lat1 double precision, lon1 double precision,
  lat2 double precision, lon2 double precision
) RETURNS double precision LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  r double precision := 6371.0; -- Earth radius in km
  dlat double precision;
  dlon double precision;
  a double precision;
  c double precision;
BEGIN
  IF lat1 IS NULL OR lon1 IS NULL OR lat2 IS NULL OR lon2 IS NULL THEN
    RETURN NULL;
  END IF;
  dlat := radians(lat2 - lat1);
  dlon := radians(lon2 - lon1);
  a := sin(dlat / 2.0)^2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2.0)^2;
  c := 2.0 * atan2(sqrt(a), sqrt(1.0 - a));
  RETURN r * c;
END;
$$;

-- Add generated tsvector column for full-text search
ALTER TABLE properties
ADD COLUMN IF NOT EXISTS tsv tsvector
GENERATED ALWAYS AS (
  to_tsvector('english', coalesce(name, '') || ' ' || coalesce(area, '') || ' ' || coalesce(city, '') || ' ' || coalesce(address, '') || ' ' || coalesce(description, ''))
) STORED;

-- Add indexes
CREATE INDEX IF NOT EXISTS properties_tsv_idx ON properties USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_properties_search ON properties(city, area, status);
CREATE INDEX IF NOT EXISTS idx_rooms_property_rent ON rooms(property_id, monthly_rent);
CREATE INDEX IF NOT EXISTS idx_beds_status ON beds(status);

-- Ensure RLS on wishlists
ALTER TABLE wishlists ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename = 'wishlists' AND policyname = 'Users manage their own wishlist'
  ) THEN
    CREATE POLICY "Users manage their own wishlist" ON wishlists
    FOR ALL TO authenticated
    USING (user_id = public.get_auth_uid())
    WITH CHECK (user_id = public.get_auth_uid());
  END IF;
END $$;
