-- Enable RLS on every table
alter table users enable row level security;
alter table student_profiles enable row level security;
alter table owner_profiles enable row level security;
alter table properties enable row level security;
alter table rooms enable row level security;
alter table beds enable row level security;
alter table bookings enable row level security;
alter table tenancies enable row level security;
alter table rent_dues enable row level security;
alter table payments enable row level security;
alter table refunds enable row level security;
alter table deposit_ledger enable row level security;
alter table maintenance_tasks enable row level security;
alter table complaints enable row level security;
alter table complaint_updates enable row level security;
alter table notices enable row level security;
alter table reviews enable row level security;
alter table wishlists enable row level security;
alter table visit_requests enable row level security;
alter table notification_templates enable row level security;
alter table notification_logs enable row level security;
alter table property_staff enable row level security;
alter table colleges enable row level security;
alter table webhook_events enable row level security;
alter table audit_logs enable row level security;
alter table data_access_requests enable row level security;

-- Helper functions to get current user id and role from JWT
-- Note: We put these in the public schema because Supabase locks down the auth schema!
create or replace function public.get_auth_uid() returns uuid as $$
  select nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid;
$$ language sql stable;

create or replace function public.get_auth_role() returns text as $$
  select nullif(current_setting('request.jwt.claims', true)::json->>'role', '');
$$ language sql stable;

-- Admin sees all
create policy "Admin all properties" on properties for all using (public.get_auth_role() = 'admin');
create policy "Admin all rooms" on rooms for all using (public.get_auth_role() = 'admin');
create policy "Admin all beds" on beds for all using (public.get_auth_role() = 'admin');
create policy "Admin all bookings" on bookings for all using (public.get_auth_role() = 'admin');
create policy "Admin all tenancies" on tenancies for all using (public.get_auth_role() = 'admin');
create policy "Admin all rent_dues" on rent_dues for all using (public.get_auth_role() = 'admin');
create policy "Admin all payments" on payments for all using (public.get_auth_role() = 'admin');
create policy "Admin all complaints" on complaints for all using (public.get_auth_role() = 'admin');
create policy "Admin all users" on users for all using (public.get_auth_role() = 'admin');
create policy "Admin all audit" on audit_logs for select using (public.get_auth_role() = 'admin');
-- Note: audit_logs rejects UPDATE/DELETE for everyone
create policy "No update audit" on audit_logs for update using (false);
create policy "No delete audit" on audit_logs for delete using (false);

-- Public read of approved properties, rooms, beds
create policy "Public read approved properties" on properties for select using (status = 'Approved');
create policy "Public read rooms of approved properties" on rooms for select using (exists (select 1 from properties p where p.property_id = rooms.property_id and p.status = 'Approved'));
create policy "Public read beds of approved properties" on beds for select using (exists (select 1 from rooms r join properties p on p.property_id = r.property_id where r.room_id = beds.room_id and p.status = 'Approved'));

-- Users see own profile
create policy "Users own profile" on users for select using (public.get_auth_uid() = user_id);
create policy "Users update own profile" on users for update using (public.get_auth_uid() = user_id);

-- Owner sees rows for properties they own
create policy "Owner own properties" on properties for all using (public.get_auth_uid() = owner_id);
create policy "Owner own rooms" on rooms for all using (exists (select 1 from properties p where p.property_id = rooms.property_id and p.owner_id = public.get_auth_uid()));
create policy "Owner own beds" on beds for all using (exists (select 1 from rooms r join properties p on p.property_id = r.property_id where r.room_id = beds.room_id and p.owner_id = public.get_auth_uid()));
create policy "Owner own bookings" on bookings for all using (exists (select 1 from properties p where p.property_id = bookings.property_id and p.owner_id = public.get_auth_uid()));
create policy "Owner own tenancies" on tenancies for all using (exists (select 1 from properties p where p.property_id = tenancies.property_id and p.owner_id = public.get_auth_uid()));
create policy "Owner own rent_dues" on rent_dues for all using (exists (select 1 from tenancies t join properties p on p.property_id = t.property_id where t.tenancy_id = rent_dues.tenancy_id and p.owner_id = public.get_auth_uid()));
create policy "Owner own complaints" on complaints for all using (exists (select 1 from properties p where p.property_id = complaints.property_id and p.owner_id = public.get_auth_uid()));

-- Student sees own rows
create policy "Student own profile" on student_profiles for all using (public.get_auth_uid() = student_id);
create policy "Student own bookings" on bookings for select using (public.get_auth_uid() = student_id);
create policy "Student own tenancies" on tenancies for select using (public.get_auth_uid() = student_id);
create policy "Student own rent_dues" on rent_dues for select using (exists (select 1 from tenancies t where t.tenancy_id = rent_dues.tenancy_id and t.student_id = public.get_auth_uid()));
create policy "Student own payments" on payments for select using (public.get_auth_uid() = payer_id);
create policy "Student own complaints" on complaints for all using (public.get_auth_uid() = student_id);

-- Warden sees rows for properties in property_staff
create policy "Warden assigned properties" on properties for select using (exists (select 1 from property_staff ps where ps.property_id = properties.property_id and ps.user_id = public.get_auth_uid()));
create policy "Warden assigned rooms" on rooms for select using (exists (select 1 from property_staff ps where ps.property_id = rooms.property_id and ps.user_id = public.get_auth_uid()));
create policy "Warden assigned beds" on beds for select using (exists (select 1 from rooms r join property_staff ps on ps.property_id = r.property_id where r.room_id = beds.room_id and ps.user_id = public.get_auth_uid()));
create policy "Warden assigned tenancies" on tenancies for select using (exists (select 1 from property_staff ps where ps.property_id = tenancies.property_id and ps.user_id = public.get_auth_uid()));
create policy "Warden assigned complaints" on complaints for all using (exists (select 1 from property_staff ps where ps.property_id = complaints.property_id and ps.user_id = public.get_auth_uid()));
