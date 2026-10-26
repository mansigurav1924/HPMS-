from locust import HttpUser, task, between
import random

class HPMSLoadTest(HttpUser):
    wait_time = between(1, 3)

    @task(3)
    def search_properties(self):
        # Simulate unauthenticated users searching for properties
        filters = random.choice([
            "?city=Test",
            "?max_price=10000",
            "?room_type=single",
            ""
        ])
        self.client.get(f"/search{filters}")

    @task(1)
    def get_public_property(self):
        # We assume 50 properties, IDs are seeded but we can search for a list first
        response = self.client.get("/search")
        if response.status_code == 200:
            props = response.json()
            if props:
                prop_id = random.choice(props)["property_id"]
                self.client.get(f"/search/{prop_id}")
