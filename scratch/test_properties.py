import psycopg

try:
    with psycopg.connect("dbname=hpms user=postgres password=postgres host=localhost port=5432") as conn:
        with conn.cursor() as cur:
            sql_query = """
                SELECT p.property_id, p.name, p.type, p.city, p.area, p.address, p.description,
                       p.latitude, p.longitude, p.facilities, p.photos, p.avg_rating, p.created_at,
                       COALESCE(MIN(r.monthly_rent), 0) AS min_rent,
                       COALESCE(MAX(r.monthly_rent), 0) AS max_rent,
                       COUNT(b.bed_id) FILTER (WHERE b.status = 'available') AS available_beds_count,
                       ARRAY_AGG(DISTINCT r.room_type) AS room_types,
                       NULL AS distance_km
                FROM properties p
                LEFT JOIN owner_profiles op ON op.owner_id = p.owner_id
                LEFT JOIN rooms r ON r.property_id = p.property_id
                LEFT JOIN beds b ON b.room_id = r.room_id
                WHERE 1=1
                GROUP BY p.property_id, p.name, p.type, p.city, p.area, p.address, p.description,
                         p.latitude, p.longitude, p.facilities, p.photos, p.avg_rating, p.created_at
                HAVING 1=1
                ORDER BY min_rent ASC
            """
            cur.execute(sql_query)
            rows = cur.fetchall()
            print(f"FOUND {len(rows)} PROPERTIES")
            for row in rows:
                print(row[1]) # Print the property name
except Exception as e:
    print("ERROR:", e)
