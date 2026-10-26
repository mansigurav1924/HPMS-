create type user_role as enum ('student', 'owner', 'admin', 'warden');
create type property_type as enum ('boys_hostel', 'girls_hostel', 'coed', 'pg');
create type booking_mode as enum ('instant', 'approval');

create table users (
    user_id uuid primary key,
    role user_role not null,
    name text not null,
    email text not null,
    phone text,
    email_verified boolean not null default false,
    phone_verified boolean not null default false,
    email_bounced boolean not null default false,
    consent_version text,
    consented_at timestamptz,
    mfa_enabled boolean not null default false,
    status text not null default 'Active',
    created_at timestamptz not null default now(),
    retention_expires_at timestamptz
);

create table student_profiles (
    student_id uuid primary key references users(user_id) on delete cascade,
    college text,
    course text,
    guardian_name text,
    guardian_phone text,
    id_proof_url text,
    home_city text,
    emergency_contact text
);

create table owner_profiles (
    owner_id uuid primary key references users(user_id) on delete cascade,
    business_name text,
    kyc_document_url text,
    verification_status text not null default 'Pending Verification',
    verified_by uuid references users(user_id),
    verified_at timestamptz,
    rejection_reason text,
    payout_account_ref text
);

create table properties (
    property_id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references users(user_id),
    name text not null,
    type property_type not null,
    address text not null,
    city text not null,
    area text,
    latitude double precision,
    longitude double precision,
    description text,
    house_rules text,
    facilities jsonb,
    photos jsonb,
    security_deposit_default numeric(12,2),
    notice_period_days int,
    lock_in_months int,
    booking_mode booking_mode not null,
    cancellation_policy jsonb,
    late_fee_config jsonb,
    rent_due_day int,
    rent_cycle_mode text,
    status text not null default 'Draft',
    avg_rating numeric(3,2),
    created_at timestamptz not null default now()
);

create table rooms (
    room_id uuid primary key default gen_random_uuid(),
    property_id uuid not null references properties(property_id) on delete cascade,
    room_number text not null,
    floor text,
    room_type text not null,
    capacity int not null,
    is_ac boolean not null default false,
    attached_bath boolean not null default false,
    monthly_rent numeric(12,2) not null,
    deposit numeric(12,2),
    photos jsonb,
    status text not null default 'Active'
);

create table beds (
    bed_id uuid primary key default gen_random_uuid(),
    room_id uuid not null references rooms(room_id) on delete cascade,
    bed_label text not null,
    status text not null default 'available',
    available_from date,
    reserved_until timestamptz
);

-- Section 12.26 Reference SQL starts here
create type booking_status as enum (
'requested','approved','payment_pending','confirmed','checked_in',
'notice','checked_out','deposit_settled','closed',
'rejected','cancelled','expired','no_show');
create type tenancy_status as enum ('active','notice','checked_out','evicted');

create table bookings (
    booking_id uuid primary key default gen_random_uuid(),
    student_id uuid not null references users(user_id),
    property_id uuid not null references properties(property_id),
    room_id uuid not null references rooms(room_id),
    bed_id uuid not null references beds(bed_id),
    move_in_date date not null,
    duration_months int,
    rent_amount numeric(12,2) not null,
    deposit_amount numeric(12,2),
    advance_paid numeric(12,2),
    status booking_status not null,
    requested_at timestamptz not null default now(),
    responded_at timestamptz,
    responded_by uuid references users(user_id),
    rejection_reason text,
    hold_expires_at timestamptz,
    cancelled_at timestamptz,
    cancel_reason text
);

create table tenancies (
    tenancy_id uuid primary key default gen_random_uuid(),
    booking_id uuid not null references bookings(booking_id),
    student_id uuid not null references users(user_id),
    property_id uuid not null references properties(property_id),
    bed_id uuid not null references beds(bed_id),
    check_in_date date,
    expected_checkout_date date,
    actual_checkout_date date,
    rent_amount numeric(12,2) not null,
    deposit_paid numeric(12,2),
    agreement_url text,
    status tenancy_status not null,
    notice_given_on date,
    notice_given_by uuid references users(user_id)
);

create table rent_dues (
    due_id uuid primary key default gen_random_uuid(),
    tenancy_id uuid not null references tenancies(tenancy_id),
    month date not null,
    base_amount numeric(12,2) not null,
    extra_charges jsonb,
    late_fee numeric(12,2),
    total_amount numeric(12,2) not null,
    amount_paid numeric(12,2) not null default 0,
    due_date date not null,
    status text not null,
    waived_by uuid references users(user_id),
    waive_reason text
);

create table payments (
    payment_id uuid primary key default gen_random_uuid(),
    due_id uuid references rent_dues(due_id),
    booking_id uuid references bookings(booking_id),
    tenancy_id uuid references tenancies(tenancy_id),
    payer_id uuid not null references users(user_id),
    amount numeric(12,2) not null,
    type text not null,
    mode text not null,
    gateway_order_id text,
    gateway_payment_id text,
    reference_no text,
    status text not null,
    recorded_by uuid references users(user_id),
    paid_at timestamptz,
    receipt_url text
);

create table maintenance_tasks (
    task_id uuid primary key default gen_random_uuid(),
    property_id uuid not null references properties(property_id),
    room_id uuid references rooms(room_id),
    bed_id uuid references beds(bed_id),
    title text not null,
    description text,
    assigned_vendor text,
    cost numeric(12,2),
    status text not null,
    scheduled_on timestamptz,
    completed_on timestamptz,
    blocks_availability boolean not null default false
);

create table complaints (
    complaint_id uuid primary key default gen_random_uuid(),
    tenancy_id uuid not null references tenancies(tenancy_id),
    student_id uuid not null references users(user_id),
    property_id uuid not null references properties(property_id),
    category text not null,
    description text not null,
    photos jsonb,
    priority text,
    status text not null,
    assigned_to uuid references users(user_id),
    expected_resolution_date date,
    resolution_notes text,
    resolution_proof jsonb,
    student_rating int,
    escalated boolean not null default false,
    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    closed_at timestamptz
);

create table complaint_updates (
    update_id uuid primary key default gen_random_uuid(),
    complaint_id uuid not null references complaints(complaint_id) on delete cascade,
    author_id uuid not null references users(user_id),
    visibility text not null,
    message text not null,
    status_change text,
    created_at timestamptz not null default now()
);

create table notices (
    notice_id uuid primary key default gen_random_uuid(),
    property_id uuid not null references properties(property_id),
    title text not null,
    body text not null,
    posted_by uuid not null references users(user_id),
    posted_at timestamptz not null default now(),
    expires_at timestamptz
);

create table reviews (
    review_id uuid primary key default gen_random_uuid(),
    tenancy_id uuid not null unique references tenancies(tenancy_id),
    property_id uuid not null references properties(property_id),
    student_id uuid not null references users(user_id),
    rating int not null,
    comment text,
    owner_reply text,
    status text not null default 'Visible',
    created_at timestamptz not null default now()
);

create table wishlists (
    user_id uuid not null references users(user_id),
    property_id uuid not null references properties(property_id),
    added_at timestamptz not null default now(),
    primary key (user_id, property_id)
);

create table visit_requests (
    visit_id uuid primary key default gen_random_uuid(),
    student_id uuid not null references users(user_id),
    property_id uuid not null references properties(property_id),
    requested_slot timestamptz not null,
    status text not null default 'Requested'
);

create table notification_templates (
    template_id text primary key,
    name text not null,
    channel text not null,
    version int not null default 1,
    body text not null,
    is_active boolean not null default true
);

create table notification_logs (
    log_id uuid primary key default gen_random_uuid(),
    user_id uuid references users(user_id),
    template_id text references notification_templates(template_id),
    recipient text not null,
    subject text,
    status text not null,
    sent_at timestamptz,
    error text,
    retry_count int not null default 0,
    bounce_type text
);

create table audit_logs (
    log_id uuid primary key default gen_random_uuid(),
    user_id uuid references users(user_id),
    role text not null,
    action text not null,
    entity_type text not null,
    entity_id uuid not null,
    timestamp timestamptz not null default now(),
    previous_value jsonb,
    new_value jsonb
);

create table data_access_requests (
    request_id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(user_id),
    request_type text not null,
    status text not null default 'Pending',
    requested_at timestamptz not null default now(),
    fulfilled_at timestamptz,
    fulfilled_by uuid references users(user_id),
    export_file_url text
);

-- Note: We assume auth.users already exists because this is a Supabase project.

alter table users
add constraint users_auth_fk foreign key (user_id) references auth.users(id),
add column dob date,
add column gender text check (gender in ('male','female','other','prefer_not_to_say')),
add column is_minor boolean;

create or replace function set_is_minor() returns trigger language plpgsql as $$
begin
  new.is_minor := new.dob is not null and new.dob > (current_date - interval '18 years');
  return new;
end $$;

create trigger trg_users_is_minor
before insert or update of dob on users
for each row execute function set_is_minor();

alter table student_profiles
add column guardian_consent_at timestamptz,
add column guardian_consent_version text,
add column guardian_verified boolean not null default false;

alter table rooms add constraint uq_room_number unique (property_id, room_number);

alter table bookings
add column owner_response_due_at timestamptz,
add column payment_due_at timestamptz;

create unique index uq_bed_active_booking on bookings(bed_id)
where status in ('requested','approved','payment_pending','confirmed','checked_in','notice');

create unique index uq_bed_active_tenancy on tenancies(bed_id)
where status in ('active','notice');

alter table rent_dues add constraint uq_due_month unique (tenancy_id, month);

alter table payments alter column tenancy_id drop not null;
alter table payments add constraint chk_payment_target
check (num_nonnulls(due_id, booking_id, tenancy_id) >= 1);
alter table payments add constraint uq_gateway_order unique (gateway_order_id);

create table webhook_events (
event_id text primary key, provider text not null, payload jsonb not null,
received_at timestamptz not null default now(), processed_at timestamptz, error text);

create table property_staff (
property_id uuid references properties(property_id) on delete cascade,
user_id uuid references users(user_id) on delete cascade,
staff_role text not null check (staff_role in ('warden','manager')),
invited_by uuid references users(user_id),
created_at timestamptz not null default now(),
primary key (property_id, user_id));

create table colleges (
college_id uuid primary key default gen_random_uuid(),
name text not null, city text not null,
latitude double precision not null, longitude double precision not null,
unique (name, city));

create table refunds (
refund_id uuid primary key default gen_random_uuid(),
payment_id uuid not null references payments(payment_id),
booking_id uuid references bookings(booking_id),
tenancy_id uuid references tenancies(tenancy_id),
amount numeric(12,2) not null check (amount > 0),
reason text, gateway_refund_id text unique,
status text not null check (status in ('pending','processed','failed')),
created_at timestamptz not null default now());

create table deposit_ledger (
entry_id uuid primary key default gen_random_uuid(),
tenancy_id uuid not null references tenancies(tenancy_id),
entry_type text not null check (entry_type in ('collected','deduction','refund')),
amount numeric(12,2) not null check (amount > 0),
reason text, created_by uuid references users(user_id),
created_at timestamptz not null default now());

create function sync_bed_status() returns trigger language plpgsql as $$
begin
update beds set status = case
when exists (select 1 from tenancies t where t.bed_id = new.bed_id
and t.status in ('active','notice')) then 'occupied'
when exists (select 1 from bookings b where b.bed_id = new.bed_id
and b.status in ('requested','approved','payment_pending','confirmed')) then 'reserved'
when exists (select 1 from maintenance_tasks m where m.bed_id = new.bed_id
and m.blocks_availability and m.status <> 'done') then 'maintenance'
else 'available' end
where bed_id = new.bed_id;
return new;
end $$;

create trigger trg_bookings_sync after insert or update of status on bookings
for each row execute function sync_bed_status();
create trigger trg_tenancies_sync after insert or update of status on tenancies
for each row execute function sync_bed_status();

create function create_booking_request(
p_student uuid, p_bed uuid, p_move_in date, p_months int)
returns uuid language plpgsql as $$
declare
v_bed beds%rowtype; v_room rooms%rowtype; v_prop properties%rowtype;
v_id uuid; v_owner_due timestamptz := now() + interval '24 hours';
begin
select * into v_bed from beds where bed_id = p_bed for update;
if not found then raise exception 'BED_NOT_FOUND'; end if;
select * into v_room from rooms where room_id = v_bed.room_id;
select * into v_prop from properties where property_id = v_room.property_id;
if v_prop.status <> 'Approved' or v_room.status <> 'Active' then
raise exception 'NOT_BOOKABLE'; end if;
if v_bed.status <> 'available' then raise exception 'BED_UNAVAILABLE'; end if;
insert into bookings(student_id, property_id, room_id, bed_id, move_in_date,
duration_months, rent_amount, deposit_amount, status, requested_at,
owner_response_due_at, hold_expires_at)
values (p_student, v_prop.property_id, v_room.room_id, p_bed, p_move_in, p_months,
v_room.monthly_rent, v_room.deposit,
case when v_prop.booking_mode = 'instant' then 'payment_pending' else 'requested' end,
now(), v_owner_due, v_owner_due)
returning booking_id into v_id;
return v_id;
end $$;
