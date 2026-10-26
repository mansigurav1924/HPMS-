HPMS Fake Demo Data

Generated to match the uploaded HPMS portal schema.

Contains:
- 5 owners
- 30 students
- 15 properties (PG + boys/girls/co-ed hostels)
- 50+ rooms
- 100+ beds
- bookings
- active tenancies
- rent dues
- payments
- complaints
- reviews
- visit requests

Important:
The users table references Supabase auth.users(id). Create matching Auth users first
before inserting public.users records. The included SQL seed is for properties, rooms
and beds and assumes valid owner IDs already exist.

All names, emails, phone numbers and property details are fictional demo data.
