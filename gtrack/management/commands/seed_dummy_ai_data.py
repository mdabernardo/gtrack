from django.core.management.base import BaseCommand
from django.db import transaction
from datetime import datetime, date, timedelta, time
import os
import random

from gtrack.models import Location, Route, RoutePoint, CollectionHistory, CollectionSchedule
from gtrack.ai_predictor import GarbageRoutePredictor

class Command(BaseCommand):
    help = "Seed dummy locations, route, history, and generate AI predictions and optimizations."

    def handle(self, *args, **options):
        with transaction.atomic():
            # Create dummy locations
            locs = []
            loc_defs = [
                {"name": "Sitio 6 basketball court", "latitude": 14.668553, "longitude": 120.962445, "address": "Sitio 6"},
                {"name": "Hernandez street", "latitude": 14.665100, "longitude": 120.958800, "address": "Hernandez"},
                {"name": "SMC Hoa", "latitude": 14.661900, "longitude": 120.960100, "address": "SMC"},
                {"name": "justice", "latitude": 14.664300, "longitude": 120.955900, "address": "Justice"},
                {"name": "Gulayan", "latitude": 14.662500, "longitude": 120.957400, "address": "Gulayan"},
                {"name": "Lucas Compound", "latitude": 14.660700, "longitude": 120.959000, "address": "Lucas"},
                {"name": "Dumpsite", "latitude": 14.659100, "longitude": 120.956900, "address": "Dumpsite"},
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
            for idx, loc in enumerate(locs, start=1):
                RoutePoint.objects.update_or_create(route=route, location=loc, defaults={"order": idx, "estimated_time_minutes": 5})

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

        try:
            import firebase_admin
            from firebase_admin import credentials, firestore as _fs
            if not getattr(firebase_admin, '_apps', None):
                cred_path = os.getenv('FIREBASE_CREDENTIALS_PATH') or os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
                if cred_path and os.path.exists(cred_path):
                    firebase_admin.initialize_app(credentials.Certificate(cred_path))
                else:
                    firebase_admin.initialize_app()
            db = _fs.client()
        except Exception:
            db = None

        if db:
            names = [
                "Sitio 6 basketball court",
                "Hernandez street",
                "SMC Hoa",
                "justice",
                "Gulayan",
                "Lucas Compound",
                "Dumpsite",
            ]
            base_lat = 14.6591
            base_lng = 120.9569
            loc_coords = {
                "Sitio 6 basketball court": (14.668553, 120.962445),
                "Hernandez street": (14.665100, 120.958800),
                "SMC Hoa": (14.661900, 120.960100),
                "justice": (14.664300, 120.955900),
                "Gulayan": (14.662500, 120.957400),
                "Lucas Compound": (14.660700, 120.959000),
                "Dumpsite": (base_lat, base_lng),
            }

            weights = {
                "Sitio 6 basketball court": 1.0,
                "Hernandez street": 1.0,
                "SMC Hoa": 1.0,
                "justice": 1.0,
                "Gulayan": 1.2,
                "Lucas Compound": 1.0,
                "Dumpsite": 2.2,
            }

            def pick_name():
                total = sum(weights.values())
                r = random.random() * total
                acc = 0.0
                for n, w in weights.items():
                    acc += w
                    if r <= acc:
                        return n
                return "Dumpsite"

            def rand_time():
                h = random.choice([7,8,9,10,11])
                m = random.choice([0,5,10,15,20,25,30,35,40,45,50,55])
                s = random.choice([0,10,20,30,40,50])
                return f"{h:02d}:{m:02d}:{s:02d}"

            def rand_level(name):
                if name == "Dumpsite":
                    return random.choice(["High","High","Medium","High","Low","High","Medium"])  
                return random.choice(["Low","Medium","Medium","High","Low","Medium"]) 

            start_date = date.today() - timedelta(days=45)
            gl_batch = []
            for i in range(1000):
                nm = pick_name()
                dt = start_date + timedelta(days=random.randint(0, 45))
                tm = rand_time()
                gl_batch.append({
                    "collectorName": random.choice(["Unknown","Team A","Team B"]),
                    "date": dt.strftime("%Y-%m-%d"),
                    "garbageLevel": rand_level(nm),
                    "location": nm,
                    "time": tm,
                })
            for item in gl_batch:
                db.collection('garbagelevel').add(item)

            sch_batch = []
            for i in range(1000):
                nm = pick_name()
                day = random.choice(["monday","tuesday","wednesday","thursday","friday","saturday"])
                tm = random.choice(["07:30 AM","08:00 AM","08:30 AM","09:00 AM","10:00 AM"])
                sch_batch.append({
                    "day": day,
                    "name": nm,
                    "time": tm,
                })
            for item in sch_batch:
                db.collection('dropoffSchedules').add(item)

            for nm, (lat, lng) in loc_coords.items():
                db.collection('dropofflocation').document().set({
                    "location": nm,
                    "latitude": lat,
                    "longitude": lng,
                    "timestamp": datetime.utcnow().isoformat()
                }, merge=True)

        self.stdout.write(self.style.SUCCESS("Dummy data seeded."))
        self.stdout.write(f"Route: {route.name} (id={route.id})")
        self.stdout.write(f"Predictions generated for days: {', '.join(generated)}")
        self.stdout.write("Optimization published to Firestore (data + by-day index).")
        if db:
            self.stdout.write("Firestore seed: 1000 garbagelevel, 1000 dropoffSchedules, 7 dropofflocation")
        else:
            self.stdout.write("Skipping Firestore seed: Admin SDK not initialized")
        self.stdout.write("\nVerify via:")
        self.stdout.write(f"- API predictions: GET http://127.0.0.1:8000/api/routes/{route.id}/predictions?days=7")
        self.stdout.write(f"- API optimization: GET http://127.0.0.1:8000/api/routes/{route.id}/optimize/")
        self.stdout.write("- Firestore Console: artifacts/g-trackapp/public/data/predictions and optimizations")
        self.stdout.write("- Firestore Console: artifacts/g-trackapp/public/index/predictions_by_day and optimizations_by_day")
