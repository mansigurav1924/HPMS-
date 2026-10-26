-- Phase 2 Migration: Storage policies, duplicate detection, notification templates, colleges seed

-- ============================================================
-- STORAGE BUCKETS (SQL-based bucket creation for Supabase)
-- ============================================================
-- NOTE: Supabase Storage buckets must be created via the dashboard or management API.
-- The SQL below sets up the storage.objects RLS policies only.
-- Buckets to create: room-photos (public), kyc-docs (private)

-- Storage RLS for room-photos (public bucket)
create policy "Owners upload room photos under their folder"
on storage.objects for insert
to authenticated
with check (
  bucket_id = 'room-photos' AND
  (storage.foldername(name))[1] = public.get_auth_uid()::text
);

create policy "Public read room photos"
on storage.objects for select
using (bucket_id = 'room-photos');

create policy "Owners delete their own room photos"
on storage.objects for delete
to authenticated
using (
  bucket_id = 'room-photos' AND
  (storage.foldername(name))[1] = public.get_auth_uid()::text
);

-- Storage RLS for kyc-docs (private bucket)
create policy "Owners upload KYC docs under their folder"
on storage.objects for insert
to authenticated
with check (
  bucket_id = 'kyc-docs' AND
  (storage.foldername(name))[1] = public.get_auth_uid()::text
);

create policy "Owners and admins read KYC docs"
on storage.objects for select
to authenticated
using (
  bucket_id = 'kyc-docs' AND (
    (storage.foldername(name))[1] = public.get_auth_uid()::text
    OR public.get_auth_role() = 'admin'
  )
);

-- ============================================================
-- DUPLICATE DETECTION TABLE
-- ============================================================
create table if not exists duplicate_flags (
  flag_id uuid primary key default gen_random_uuid(),
  entity_type text not null check (entity_type in ('owner', 'property')),
  entity_id_a uuid not null,
  entity_id_b uuid not null,
  match_reason text not null,
  status text not null default 'pending' check (status in ('pending', 'keep_separate', 'marked_duplicate')),
  resolved_by uuid references users(user_id),
  resolved_at timestamptz,
  created_at timestamptz not null default now()
);

alter table duplicate_flags enable row level security;
create policy "Admin all duplicate_flags" on duplicate_flags for all using (public.get_auth_role() = 'admin');

-- ============================================================
-- NOTIFICATION TEMPLATES SEED
-- ============================================================
insert into notification_templates (template_id, name, channel, version, body) values
('owner_approved',   'Owner KYC Approved',   'email', 1,
 'Dear {{name}}, your KYC has been verified and your account is now approved. You can start listing properties.'),
('owner_rejected',   'Owner KYC Rejected',   'email', 1,
 'Dear {{name}}, your KYC verification was rejected. Reason: {{reason}}. Please re-submit with correct documents.'),
('property_approved','Property Approved',    'email', 1,
 'Dear {{name}}, your property "{{property_name}}" has been approved and is now listed publicly.'),
('property_rejected','Property Rejected',    'email', 1,
 'Dear {{name}}, your property "{{property_name}}" was rejected. Reason: {{reason}}.'),
('property_suspended','Property Suspended',  'email', 1,
 'Dear {{name}}, your property "{{property_name}}" has been suspended. Reason: {{reason}}.')
ON CONFLICT (template_id) DO NOTHING;

-- ============================================================
-- COLLEGES SEED (Vadodara, Gujarat)
-- ============================================================
insert into colleges (name, city, latitude, longitude) values
('Maharaja Sayajirao University of Baroda', 'Vadodara', 22.3072, 73.1812),
('ITM SLS Baroda University',               'Vadodara', 22.3450, 73.2100),
('Parul University',                        'Vadodara', 22.2747, 73.0678),
('Navrachana University',                   'Vadodara', 22.3526, 73.2278),
('BITS Edu Campus',                         'Vadodara', 22.3080, 73.2280),
('Baroda Medical College',                  'Vadodara', 22.3217, 73.1853),
('Faculty of Technology & Engineering MSU', 'Vadodara', 22.3104, 73.1820)
ON CONFLICT (name, city) DO NOTHING;

-- ============================================================
-- PROPERTY DUPLICATE CHECK FUNCTION
-- ============================================================
create or replace function check_property_duplicate(
  p_address text, p_lat double precision, p_lon double precision,
  p_owner_id uuid, p_property_id uuid default null
) returns void language plpgsql as $$
declare v_match uuid;
begin
  -- Address similarity or coordinate proximity (<200m approx)
  select property_id into v_match
  from properties
  where property_id <> coalesce(p_property_id, gen_random_uuid())
    and owner_id <> p_owner_id
    and (
      lower(trim(address)) = lower(trim(p_address))
      or (
        p_lat is not null and p_lon is not null and latitude is not null and longitude is not null and
        abs(latitude - p_lat) < 0.002 and abs(longitude - p_lon) < 0.002
      )
    )
  limit 1;
  if found then
    insert into duplicate_flags (entity_type, entity_id_a, entity_id_b, match_reason)
    values ('property', p_property_id, v_match, 'Same address or coordinates within 200m')
    on conflict do nothing;
  end if;
end $$;
