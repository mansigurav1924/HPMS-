CREATE TABLE IF NOT EXISTS public.geocode_cache (
    id SERIAL PRIMARY KEY,
    query_key TEXT UNIQUE NOT NULL,
    lat NUMERIC(10, 7) NOT NULL,
    lng NUMERIC(10, 7) NOT NULL,
    formatted_address TEXT,
    city TEXT,
    area TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- RLS
ALTER TABLE public.geocode_cache ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Enable read access for all users" ON public.geocode_cache FOR SELECT USING (true);
CREATE POLICY "Enable insert for authenticated users" ON public.geocode_cache FOR INSERT TO authenticated WITH CHECK (true);
