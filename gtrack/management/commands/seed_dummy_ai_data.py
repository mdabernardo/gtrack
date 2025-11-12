from django.core.management.base import BaseCommand
from django.db import transaction
from datetime import datetime, date, timedelta, time

from gtrack.models import Location, Route, RoutePoint, CollectionHistory, CollectionSchedule
from gtrack.ai_predictor import GarbageRoutePredictor

class Command(BaseCommand):
    help = "Seed dummy locations, route, history, and generate AI predictions and optimizations."

    def handle(self, *args, **options):
        with transaction.atomic():
            # Create dummy locations
            locs = []
            loc_defs = [
                {"name": "Central Park", "latitude": 40.785091, "longitude": -73.968285, "address": "Central Park, NY"},
                {"name": "City Hall", "latitude": 40.712772, "longitude": -74.006058, "address": "City Hall, NY"},
                {"name": "Union Square", "latitude": 40.735863, "longitude": -73.991084, "address": "Union Square, NY"},
            ]
            for d in loc_defs:
                loc, _ = Location.objects.get_or_create(
                    name=d["name"],
                    defaults={"latitude": d["latitude"], "longitude": d["longitude"], "address": d["address"]}
                )
                # Update coordinates/address if changed
                loc.latitude = d["latitude"]
                loc.longitude = d["longitude"]
                loc.address = d["address"]
                loc.save()
                locs.append(loc)

            # Create a demo route
            route, _ = Route.objects.get_or_create(name="Demo Route", defaults={"description": "Demo garbage collection route"})

            # Create route points in a fixed order
            RoutePoint.objects.update_or_create(route=route, location=locs[0], defaults={"order": 1, "estimated_time_minutes": 5})
            RoutePoint.objects.update_or_create(route=route, location=locs[1], defaults={"order": 2, "estimated_time_minutes": 5})
            RoutePoint.objects.update_or_create(route=route, location=locs[2], defaults={"order": 3, "estimated_time_minutes": 5})

            # Create a simple weekly schedule (Mon-Fri 08:00-16:00)
            for dow in [0, 1, 2, 3, 4]:
                CollectionSchedule.objects.update_or_create(
                    route=route,
                    day_of_week=dow,
                    defaults={"start_time": time(8, 0), "end_time": time(16, 0), "is_active": True}
                )

            # Seed 10 days of collection history with slight variations
            today = date.today()
            for i in range(1, 11):
                d = today - timedelta(days=i)
                # Stagger start around 07:30–09:30 and duration 1.5–3.0h
                base_start_hour = 7 + (i % 3)  # 7,8,9 pattern
                start_t = time(base_start_hour, 30)
                duration_minutes = 90 + (i % 5) * 30  # 90,120,150,180,210
                end_dt = datetime.combine(d, start_t) + timedelta(minutes=duration_minutes)
                end_t = end_dt.time()

                CollectionHistory.objects.update_or_create(
                    route=route,
                    date=d,
                    defaults={
                        "start_time": start_t,
                        "end_time": end_t,
                        "notes": "seeded",
                        "weather_condition": "clear",
                        "traffic_condition": "moderate",
                    }
                )

        # Generate predictions and mirror to Firestore
        predictor = GarbageRoutePredictor()
        generated = []
        for i in range(7):
            target_date = today + timedelta(days=i)
            pred = predictor.predict_route_schedule(route.id, target_date)
            if pred:
                predictor.save_prediction(route.id, target_date, pred)
                generated.append(target_date.strftime('%Y-%m-%d'))

        # Generate optimization (also mirrors to Firestore)
        optimization = predictor.optimize_route_by_garbage_level(route.id)

        self.stdout.write(self.style.SUCCESS("Dummy data seeded."))
        self.stdout.write(f"Route: {route.name} (id={route.id})")
        self.stdout.write(f"Predictions generated for days: {', '.join(generated)}")
        self.stdout.write("Optimization published to Firestore (data + by-day index).")
        self.stdout.write("\nVerify via:")
        self.stdout.write(f"- API predictions: GET http://127.0.0.1:8000/api/routes/{route.id}/predictions?days=7")
        self.stdout.write(f"- API optimization: GET http://127.0.0.1:8000/api/routes/{route.id}/optimize/")
        self.stdout.write("- Firestore Console: artifacts/g-trackapp/public/data/predictions and optimizations")
        self.stdout.write("- Firestore Console: artifacts/g-trackapp/public/index/predictions_by_day and optimizations_by_day")