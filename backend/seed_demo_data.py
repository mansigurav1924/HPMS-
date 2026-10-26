"""
Seed script for demo properties, rooms, beds, and colleges for Phase 3.
"""
import uuid
from supabase import create_client
from app.config import settings

sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

DEMO_PROPERTIES = [
    {
        "name": "Baroda Student Residency",
        "type": "boys_hostel",
        "address": "45 MSU Road, Near Fatehgunj Circle",
        "city": "Vadodara",
        "area": "Fatehgunj",
        "latitude": 22.3120,
        "longitude": 73.1830,
        "description": "Premium boys hostel with 24/7 security, high-speed WiFi, nutritious meals, and daily housekeeping.",
        "house_rules": "No smoking. Gate closes at 10 PM. Visitors allowed in lobby only.",
        "facilities": ["WiFi", "Meals", "Laundry", "Parking", "Security", "Power Backup"],
        "photos": [
            "https://images.unsplash.com/photo-1555854877-bab0e564b8d5?w=800",
            "https://images.unsplash.com/photo-1595526114035-0d45ed16cfbf?w=800"
        ],
        "security_deposit_default": 5000,
        "notice_period_days": 30,
        "lock_in_months": 3,
        "booking_mode": "instant",
        "status": "Approved",
        "avg_rating": 4.8,
        "rooms": [
            {"room_number": "101", "room_type": "single", "capacity": 1, "is_ac": True, "monthly_rent": 8500, "deposit": 5000},
            {"room_number": "102", "room_type": "double", "capacity": 2, "is_ac": True, "monthly_rent": 6000, "deposit": 4000},
            {"room_number": "103", "room_type": "triple", "capacity": 3, "is_ac": False, "monthly_rent": 4500, "deposit": 3000}
        ]
    },
    {
        "name": "Navrachana Girls PG & Suite",
        "type": "girls_hostel",
        "address": "12 University Highway, Vasna Road",
        "city": "Vadodara",
        "area": "Vasna",
        "latitude": 22.3500,
        "longitude": 73.2250,
        "description": "Safe and modern PG for female students with attached bathrooms, study desks, and biometric entrance.",
        "house_rules": "Female residents only. Quiet hours 10 PM - 6 AM.",
        "facilities": ["WiFi", "Meals", "Security", "Cleaning", "Water Purifier"],
        "photos": [
            "https://images.unsplash.com/photo-1522708323590-d24dbb6b0267?w=800",
            "https://images.unsplash.com/photo-1502672260266-1c1ef2d93688?w=800"
        ],
        "security_deposit_default": 6000,
        "notice_period_days": 30,
        "lock_in_months": 6,
        "booking_mode": "approval",
        "status": "Approved",
        "avg_rating": 4.6,
        "rooms": [
            {"room_number": "G-01", "room_type": "single", "capacity": 1, "is_ac": True, "monthly_rent": 9500, "deposit": 6000},
            {"room_number": "G-02", "room_type": "double", "capacity": 2, "is_ac": False, "monthly_rent": 6500, "deposit": 4500}
        ]
    },
    {
        "name": "Sayajigunj Co-Ed Student Living",
        "type": "coed",
        "address": "88 Sayajigunj Main Rd",
        "city": "Vadodara",
        "area": "Sayajigunj",
        "latitude": 22.3080,
        "longitude": 73.1850,
        "description": "Vibrant student community living with gym, gaming lounge, high speed fiber internet and rooftop cafe.",
        "house_rules": "Respect quiet hours. Clean common spaces after use.",
        "facilities": ["WiFi", "Gym", "Laundry", "Cleaning", "Security", "Parking"],
        "photos": [
            "https://images.unsplash.com/photo-1560448204-e02f11c3d0e2?w=800"
        ],
        "security_deposit_default": 7000,
        "notice_period_days": 15,
        "lock_in_months": 1,
        "booking_mode": "instant",
        "status": "Approved",
        "avg_rating": 4.9,
        "rooms": [
            {"room_number": "S-301", "room_type": "double", "capacity": 2, "is_ac": True, "monthly_rent": 7500, "deposit": 5000},
            {"room_number": "S-302", "room_type": "dormitory", "capacity": 4, "is_ac": False, "monthly_rent": 4000, "deposit": 3000}
        ]
    }
]


def seed_demo_data():
    print("Seeding demo properties, rooms, and beds...")
    
    # Check or create an approved demo owner
    owner_email = "demo_owner@hpms.com"
    existing = sb.table("users").select("user_id").eq("email", owner_email).execute()
    
    if existing.data and len(existing.data) > 0:
        owner_id = existing.data[0]["user_id"]
    else:
        # First check if there's any owner we can just reuse
        any_owner = sb.table("users").select("user_id").eq("role", "owner").limit(1).execute()
        if any_owner.data and len(any_owner.data) > 0:
            owner_id = any_owner.data[0]["user_id"]
            # Ensure they have an approved profile
            profile_check = sb.table("owner_profiles").select("owner_id").eq("owner_id", owner_id).execute()
            if not profile_check.data:
                sb.table("owner_profiles").insert({
                    "owner_id": owner_id,
                    "business_name": "HPMS Premium Stays Ltd",
                    "verification_status": "approved"
                }).execute()
            else:
                sb.table("owner_profiles").update({"verification_status": "approved"}).eq("owner_id", owner_id).execute()
        else:
            # Need to create auth user first to satisfy users_auth_fk
            auth_res = sb.auth.admin.create_user({
                "email": owner_email,
                "password": "Password123!",
                "email_confirm": True
            })
            owner_id = auth_res.user.id
            
            # Note: Many Supabase setups have a trigger that auto-inserts into public.users.
            # We will try to update first, if no rows updated, we insert.
            update_res = sb.table("users").update({"role": "owner", "name": "Demo Approved Owner"}).eq("user_id", owner_id).execute()
            if not update_res.data:
                sb.table("users").insert({
                    "user_id": owner_id,
                    "role": "owner",
                    "name": "Demo Approved Owner",
                    "email": owner_email
                }).execute()
                
            sb.table("owner_profiles").insert({
                "owner_id": owner_id,
                "business_name": "HPMS Premium Stays Ltd",
                "verification_status": "approved"
            }).execute()
    
    # Insert properties
    for prop in DEMO_PROPERTIES:
        rooms_list = prop.pop("rooms")
        prop["owner_id"] = owner_id
        
        # Insert property
        pres = sb.table("properties").insert(prop).execute()
        if pres.data:
            property_id = pres.data[0]["property_id"]
            print(f"Created property: '{prop['name']}' ({property_id})")
            
            # Insert rooms and beds
            for rdata in rooms_list:
                rdata["property_id"] = property_id
                rres = sb.table("rooms").insert(rdata).execute()
                if rres.data:
                    room_id = rres.data[0]["room_id"]
                    cap = rdata["capacity"]
                    for b_idx in range(1, cap + 1):
                        sb.table("beds").insert({
                            "room_id": room_id,
                            "bed_label": f"Bed {b_idx}",
                            "status": "available"
                        }).execute()
                    print(f"  -> Added room {rdata['room_number']} with {cap} bed(s)")

    print("\n✅ Seed completed successfully!")


if __name__ == "__main__":
    seed_demo_data()
