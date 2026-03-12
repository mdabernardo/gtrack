
import datetime
import random
from django.core.management.base import BaseCommand
from django.conf import settings
from gtrack.models import Route, RoutePoint, Location, CollectionSchedule, AIRoutePrediction, CollectionHistory
import firebase_admin
from firebase_admin import credentials, firestore

class Command(BaseCommand):
    help = 'Wipes old data and seeds fresh routes, schedules, and resident ETAs starting from TODAY.'

    def handle(self, *args, **options):
        self.stdout.write("Starting Clean & Seed Process...")

        # 1. Initialize Firebase
        if not firebase_admin._apps:
            try:
                cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', None)
                if cred_path:
                    cred = credentials.Certificate(cred_path)
                    firebase_admin.initialize_app(cred)
                else:
                    firebase_admin.initialize_app()
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Firebase init warning: {e}"))

        try:
            db = firestore.client()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Firestore client failed: {e}"))
            return

        # 2. Django DB Cleanup
        self.stdout.write("Cleaning Django DB...")
        AIRoutePrediction.objects.all().delete()
        CollectionHistory.objects.all().delete()
        CollectionSchedule.objects.all().delete()
        RoutePoint.objects.all().delete()
        Route.objects.all().delete()
        # We can keep Locations or update them. Let's keep them but ensure our 6 specific ones exist.
        
        # 3. Firestore Cleanup
        self.stdout.write("Cleaning Firestore Collections...")
        collections_to_clean = [
            'route_suggestion',
            'collector_schedules',
            'dropoffSchedules',
            'dropofflocation',
            'garbagelevel'
        ]
        
        for coll_name in collections_to_clean:
            ref = db.collection(coll_name)
            self.delete_collection(ref, 50)

        # 4. Create Main Route and Locations
        self.stdout.write("Creating Main Route and Locations...")
        route = Route.objects.create(name="Main Route", description="Primary collection route")
        
        locations_data = [
            {"name": "Sitio 6 Basketball Court", "lat": 14.668553, "lng": 120.962445},
            {"name": "SM Hoa", "lat": 14.661900, "lng": 120.960100},
            {"name": "Lucas Compound", "lat": 14.660700, "lng": 120.959000},
            {"name": "Justice", "lat": 14.664300, "lng": 120.955900},
            {"name": "Gulayan", "lat": 14.662500, "lng": 120.957400},
            {"name": "Dumpsite", "lat": 14.659100, "lng": 120.956900}
        ]

        route_points = []
        for idx, loc_data in enumerate(locations_data, 1):
            loc, created = Location.objects.get_or_create(
                name=loc_data["name"],
                defaults={"latitude": loc_data["lat"], "longitude": loc_data["lng"], "address": loc_data["name"]}
            )
            # Update coords if needed
            loc.latitude = loc_data["lat"]
            loc.longitude = loc_data["lng"]
            loc.save()

            rp = RoutePoint.objects.create(
                route=route,
                location=loc,
                order=idx,
                estimated_time_minutes=15
            )
            route_points.append(rp)

        # 4b. Create SQL Collection Schedules (Weekly Template)
        self.stdout.write("Creating SQL Collection Schedules (Weekly Template)...")
        # 0=Monday to 6=Sunday
        for day_idx in range(7): 
            CollectionSchedule.objects.create(
                route=route,
                day_of_week=day_idx,
                start_time=datetime.time(6, 0),
                end_time=datetime.time(21, 0),
                is_active=True
            )

        # 5. Seed Dropoff Locations (Static Data)
        self.stdout.write("Seeding Dropoff Locations...")
        dl_ref = db.collection('dropofflocation')
        for loc_data in locations_data:
            dl_doc = {
                "location": loc_data["name"],
                "latitude": loc_data["lat"],
                "longitude": loc_data["lng"],
                "timestamp": datetime.datetime.now().isoformat()
            }
            dl_ref.add(dl_doc)

        # 6. Seed Firestore Data (Starting YESTERDAY to cover timezone diffs)
        today = datetime.date.today()
        # Go back 2 days to be absolutely safe about "yesterday" in any timezone
        start_seed_date = today - datetime.timedelta(days=2)
        days_to_seed = 365 + 2 # Seed a full year from 2 days ago
        
        self.stdout.write(f"Seeding Firestore data from {start_seed_date} for {days_to_seed} days...")

        batch = db.batch()
        count = 0
        total_seeded = 0

        current_date = start_seed_date
        for i in range(days_to_seed):
            iso_date = current_date.strftime('%Y-%m-%d')
            ymd = current_date.strftime('%Y%m%d')
            day_name = current_date.strftime('%A')
            # 1=Monday, 7=Sunday
            day_index = current_date.isoweekday() 
            
            # --- A. Route Suggestion (Resident ETAs) ---
            doc_id = f"1_{ymd}" # Using route ID 1 convention
            doc_ref = db.collection('route_suggestion').document(doc_id)
            
            # Start of the day for route timestamp
            route_ts = datetime.datetime.combine(current_date, datetime.time(6, 0))

            suggested_points = []
            
            # 2 Rounds: Morning (8 AM) and Afternoon (2 PM)
            rounds = [8, 14]
            point_order_seq = 1
            
            for start_hour in rounds:
                current_time = datetime.datetime.combine(current_date, datetime.time(start_hour, 0))
                
                for loc_data in locations_data:
                    # If past 9 PM, stop
                    if current_time.hour >= 21: 
                        break

                    time_str = current_time.strftime('%I:%M %p')
                    
                    point = {
                        "location_name": loc_data["name"],
                        "estimated_arrival": time_str,
                        "urgency": random.choice(["Low", "Medium", "High"]),
                        "order": point_order_seq,
                        "status": "pending"
                    }
                    suggested_points.append(point)
                    point_order_seq += 1
                    
                    # Travel time ~25-30 mins
                    current_time += datetime.timedelta(minutes=random.randint(25, 30))

            rs_data = {
                "date": iso_date,
                "route_id": str(route.id),
                "day_name": day_name,
                "day_index": day_index, # Added
                "suggested_points": suggested_points,
                "total_points": len(suggested_points),
                "created_at": route_ts
            }
            batch.set(doc_ref, rs_data)
            count += 1

            # --- B. Collector Schedule ---
            sched_ref = db.collection('collector_schedules').document(doc_id)
            sched_data = {
                "date": iso_date,
                "dayName": day_name,
                "dayIndex": day_index, # Added
                "routeId": str(route.id),
                "routeName": route.name,
                "collectorId": "1", # Default collector
                "startTime": "06:00 AM",
                "endTime": "09:00 PM",
                "status": "scheduled",
                "task": "Garbage Collection",
                "updatedAt": route_ts
            }
            batch.set(sched_ref, sched_data, merge=True)
            count += 1

            # --- C. Drop-off Schedules ---
            # For each location, add a drop-off slot
            dropoff_times = {
                "Sitio 6 Basketball Court": "08:00 AM",
                "SM Hoa": "09:00 AM",
                "Lucas Compound": "10:00 AM",
                "Justice": "11:00 AM",
                "Gulayan": "01:00 PM",
                "Dumpsite": "02:00 PM",
            }
            
            for loc_name, time_val in dropoff_times.items():
                # Create a deterministic ID to prevent duplicates if re-run or overlapped
                safe_name = loc_name.replace(" ", "_").replace("/", "-")
                doc_id = f"{iso_date}_{safe_name}"
                do_ref = db.collection('dropoffSchedules').document(doc_id)
                
                # Convert time string to datetime
                # time_val is like "08:00 AM"
                t_struct = datetime.datetime.strptime(time_val, "%I:%M %p").time()
                do_ts = datetime.datetime.combine(current_date, t_struct)
                
                do_data = {
                    "date": iso_date,
                    "day": day_name,
                    "day_index": day_index, # Added
                    "name": loc_name,
                    "time": time_val,
                    "timestamp": do_ts
                }
                batch.set(do_ref, do_data)
                count += 1

            # Commit batch if needed
            if count >= 400:
                batch.commit()
                batch = db.batch()
                count = 0
                self.stdout.write(f"  Seeded up to {iso_date} ({i+1}/{days_to_seed})")
            
            current_date += datetime.timedelta(days=1)
            total_seeded += 1

        if count > 0:
            batch.commit()
            
        # 6. Seed rich Garbage Level data (Past 3 days to show history)
        self.stdout.write("Seeding Garbage Levels (Past 3 days)...")
        gl_ref = db.collection('garbagelevel')
        
        # Last 3 days including today
        seed_dates = [today - datetime.timedelta(days=i) for i in range(3)]
        # Times per day
        seed_times = ["08:00", "12:00", "16:00"]
        
        gl_batch = db.batch()
        gl_count = 0

        for s_date in seed_dates:
            date_str = s_date.strftime('%Y-%m-%d')
            for s_time in seed_times:
                for loc_data in locations_data:
                    # Randomize level
                    level = random.choice(["Low", "Low", "Medium", "Medium", "High", "Critical"])
                    
                    # Create a timestamp combining date and time
                    dt_str = f"{date_str} {s_time}"
                    dt_obj = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                    
                    doc_ref = gl_ref.document() # Auto-ID
                    gl_data = {
                        "location": loc_data["name"],
                        "garbageLevel": level,
                        "date": date_str,
                        "time": s_time,
                        "collectorName": "System",
                        "timestamp": dt_obj,
                        "latitude": loc_data["lat"],
                        "longitude": loc_data["lng"]
                    }
                    gl_batch.set(doc_ref, gl_data)
                    gl_count += 1
                    
                    if gl_count >= 400:
                        gl_batch.commit()
                        gl_batch = db.batch()
                        gl_count = 0
        
        if gl_count > 0:
            gl_batch.commit()

        self.stdout.write(self.style.SUCCESS(f"Done! Wiped old data and seeded {total_seeded} days starting {today}."))

    def delete_collection(self, coll_ref, batch_size):
        self.stdout.write(f"Deleting from {coll_ref.id}...")
        deleted = 0
        while True:
            docs = list(coll_ref.limit(batch_size).stream())
            if not docs:
                break
            
            batch = firestore.client().batch()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()
            
            deleted += len(docs)
            self.stdout.write(f"Deleted {deleted} docs...")
            
        self.stdout.write(f"Finished deleting {deleted} docs from {coll_ref.id}")
