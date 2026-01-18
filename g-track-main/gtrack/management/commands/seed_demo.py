from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import datetime, timedelta, time
import random

from gtrack.models import (
    Location, Route, RoutePoint, CollectionSchedule,
    CollectionHistory, GarbageCollector, Resident
)


class Command(BaseCommand):
    help = "Seed demo data: locations, routes, points, schedules, and history"

    def handle(self, *args, **options):
        random.seed(42)

        # Create locations
        locations = []
        location_data = [
            ("Central Park", 40.785091, -73.968285, "Central Park, NY"),
            ("Times Square", 40.758896, -73.985130, "Times Square, NY"),
            ("Brooklyn Bridge", 40.706086, -73.996864, "Brooklyn Bridge, NY"),
            ("Union Square", 40.735863, -73.991084, "Union Square, NY"),
            ("Chelsea Market", 40.742439, -74.006073, "Chelsea Market, NY"),
            ("Battery Park", 40.703277, -74.017028, "Battery Park, NY"),
            ("SoHo", 40.723301, -74.002988, "SoHo, NY"),
            ("Harlem", 40.811550, -73.946477, "Harlem, NY"),
            ("East Village", 40.726477, -73.981533, "East Village, NY"),
            ("Financial District", 40.707490, -74.011276, "Financial District, NY"),
        ]
        for name, lat, lon, addr in location_data:
            loc, _ = Location.objects.get_or_create(
                name=name,
                defaults={"latitude": lat, "longitude": lon, "address": addr}
            )
            locations.append(loc)

        # Create routes
        routes = []
        for i in range(1, 4):
            route, _ = Route.objects.get_or_create(name=f"Route {i}", defaults={"description": f"Daily route {i}"})
            routes.append(route)

        # Assign points to routes
        for idx, route in enumerate(routes):
            route_points = random.sample(locations, 5)
            for order, loc in enumerate(route_points, start=1):
                RoutePoint.objects.get_or_create(
                    route=route,
                    location=loc,
                    order=order,
                    defaults={"estimated_time_minutes": random.choice([5, 7, 10])}
                )

        # Create weekly schedules (Mon–Sat)
        for route in routes:
            for day in range(0, 6):
                CollectionSchedule.objects.get_or_create(
                    route=route,
                    day_of_week=day,
                    defaults={
                        "start_time": time(hour=7 + (day % 2), minute=0),
                        "end_time": time(hour=10 + (day % 2), minute=0),
                        "is_active": True,
                    }
                )

        # Create history for past 30 days per route
        today = timezone.now().date()
        for route in routes:
            for days_back in range(1, 31):
                date = today - timedelta(days=days_back)
                dow = date.weekday()
                # Only log on scheduled days
                if dow in range(0, 6):
                    base_start_hour = 7 + (dow % 2)
                    jitter = random.randint(-20, 25)  # minutes jitter
                    start_minutes = max(6 * 60, base_start_hour * 60 + jitter)
                    duration = random.randint(120, 180)
                    end_minutes = start_minutes + duration
                    start_time = time(hour=start_minutes // 60, minute=start_minutes % 60)
                    end_time = time(hour=(end_minutes // 60) % 24, minute=end_minutes % 60)

                    CollectionHistory.objects.get_or_create(
                        route=route,
                        date=date,
                        defaults={
                            "start_time": start_time,
                            "end_time": end_time,
                            "notes": "Routine collection",
                            "weather_condition": random.choice(["clear", "cloudy", "rain", "unknown"]),
                            "traffic_condition": random.choice(["light", "normal", "heavy"]),
                        }
                    )

        # Create demo users
        resident_user, _ = User.objects.get_or_create(username="resident1", defaults={"email": "resident1@example.com"})
        collector_user, _ = User.objects.get_or_create(username="collector1", defaults={"email": "collector1@example.com"})

        # Create Resident and assign a random location
        res_loc = random.choice(locations)
        Resident.objects.get_or_create(
            user=resident_user,
            defaults={"location": res_loc, "phone_number": "555-0101", "notification_enabled": True}
        )

        # Create GarbageCollector and assign routes
        gc, _ = GarbageCollector.objects.get_or_create(user=collector_user, defaults={"phone_number": "555-0102"})
        gc.assigned_routes.set(routes)
        gc.save()

        self.stdout.write(self.style.SUCCESS("Demo data seeded successfully."))