from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from datetime import timedelta, datetime, time
import random

from gtrack.models import Location, Route, RoutePoint, CollectionSchedule, CollectionHistory

try:
    import firebase_admin
    from firebase_admin import firestore
except Exception:
    firebase_admin = None
    firestore = None


class Command(BaseCommand):
    help = "Seed dataset for 6 locations (Sitio 6, Gulayan, SM Hoa, Lucas Compound, Justice, Dumpsite) and clear old Catmon/Purok demo data"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=180)
        parser.add_argument("--route_name", type=str, default="Main Route")

    def handle(self, *args, **opts):
        days = int(opts.get("days") or 180)
        route_name = opts.get("route_name") or "Main Route"

        target_names = [
            "Sitio 6 basketball court",
            "Gulayan",
            "SM Hoa",
            "Lucas Compound",
            "Justice",
            "Dumpsite",
        ]
        base_lat = 14.6620
        base_lng = 120.9490

        with transaction.atomic():
            RoutePoint.objects.filter(route__name__icontains="Catmon Daily Route").delete()
            CollectionSchedule.objects.filter(route__name__icontains="Catmon Daily Route").delete()
            CollectionHistory.objects.filter(route__name__icontains="Catmon Daily Route").delete()
            Route.objects.filter(name__icontains="Catmon Daily Route").delete()
            Location.objects.filter(name__icontains="catmon").delete()

            locations = []
            for i, name in enumerate(target_names):
                lat = base_lat + (i * 0.0010)
                lng = base_lng + ((i % 3) * 0.0013)
                loc = Location.objects.filter(name__iexact=name).first()
                if loc:
                    loc.name = name
                    loc.latitude = lat
                    loc.longitude = lng
                    if not loc.address:
                        loc.address = f"{name}, Malabon"
                        loc.save(update_fields=["name", "latitude", "longitude", "address"])
                    else:
                        loc.save(update_fields=["name", "latitude", "longitude"])
                else:
                    loc = Location.objects.create(
                        name=name,
                        latitude=lat,
                        longitude=lng,
                        address=f"{name}, Malabon",
                    )
                locations.append(loc)

            route, _ = Route.objects.get_or_create(name=route_name, defaults={"description": "Auto-seeded route"})

            RoutePoint.objects.filter(route=route).delete()
            for idx, loc in enumerate(locations, start=1):
                RoutePoint.objects.create(route=route, location=loc, order=idx, estimated_time_minutes=7)

            CollectionSchedule.objects.filter(route=route).delete()
            for dow in range(7):
                CollectionSchedule.objects.create(route=route, day_of_week=dow, start_time=time(8, 0), end_time=time(12, 0))

        db = None
        if firebase_admin and firestore and getattr(firebase_admin, "_apps", None):
            try:
                db = firestore.client()
            except Exception:
                db = None

        today = timezone.localdate()
        start_day = today - timedelta(days=days)

        if db:
            try:
                batch = db.batch()
                write_count = 0

                catmon_route_id = "19"
                try:
                    for snap in db.collection("collector_schedules").get():
                        if str(snap.id).startswith(catmon_route_id + "_"):
                            batch.delete(db.collection("collector_schedules").document(snap.id))
                            write_count += 1
                            if write_count % 200 == 0:
                                batch.commit()
                                batch = db.batch()
                except Exception:
                    pass

                try:
                    for snap in db.collection("scheduling_assistance").get():
                        if str(snap.id).startswith(catmon_route_id + "_"):
                            batch.delete(db.collection("scheduling_assistance").document(snap.id))
                            write_count += 1
                            if write_count % 200 == 0:
                                batch.commit()
                                batch = db.batch()
                except Exception:
                    pass

                try:
                    for snap in db.collection("route_suggestion").get():
                        if str(snap.id).startswith(catmon_route_id + "_"):
                            batch.delete(db.collection("route_suggestion").document(snap.id))
                            write_count += 1
                            if write_count % 200 == 0:
                                batch.commit()
                                batch = db.batch()
                except Exception:
                    pass

                try:
                    for snap in db.collection("dropoffSchedules").where("barangay", "==", "Catmon").get():
                        batch.delete(db.collection("dropoffSchedules").document(snap.id))
                        write_count += 1
                        if write_count % 200 == 0:
                            batch.commit()
                            batch = db.batch()
                except Exception:
                    pass

                try:
                    for snap in db.collection("garbagelevel").where("barangay", "==", "Catmon").get():
                        batch.delete(db.collection("garbagelevel").document(snap.id))
                        write_count += 1
                        if write_count % 200 == 0:
                            batch.commit()
                            batch = db.batch()
                except Exception:
                    pass

                try:
                    for snap in db.collection("dropofflocation").where("barangay", "==", "Catmon").get():
                        batch.delete(db.collection("dropofflocation").document(snap.id))
                        write_count += 1
                        if write_count % 200 == 0:
                            batch.commit()
                            batch = db.batch()
                except Exception:
                    pass

                if write_count % 200 != 0:
                    batch.commit()
                    batch = db.batch()
                    write_count = 0

                drop_col = db.collection("dropofflocation")
                for loc in locations:
                    doc = drop_col.document(loc.name.replace(" ", "_"))
                    batch.set(
                        doc,
                        {
                            "name": loc.name,
                            "latitude": loc.latitude,
                            "longitude": loc.longitude,
                            "address": loc.address,
                        },
                        merge=True,
                    )
                    write_count += 1
                    if write_count % 450 == 0:
                        batch.commit()
                        batch = db.batch()

                garb_col = db.collection("garbagelevel")
                cur = start_day
                while cur <= today:
                    rng = random.Random(int(cur.strftime("%Y%m%d")))
                    for idx, loc in enumerate(locations):
                        is_dumpsite = loc.name.strip().lower() == "dumpsite"
                        noise = int(26 * rng.random()) - 10
                        if is_dumpsite:
                            base_lvl = 70 + int(22 * rng.random())
                            if rng.random() < 0.2:
                                base_lvl = 55 + int(15 * rng.random())
                        else:
                            base_lvl = 28 + int(42 * rng.random())
                            if rng.random() < 0.12:
                                base_lvl = 65 + int(20 * rng.random())
                        lvl = max(0, min(100, base_lvl + noise))
                        hour = 7 + (idx % 3)
                        minute = rng.choice([5, 10, 15, 20, 25, 30, 35, 40, 45, 50])
                        batch.set(
                            garb_col.document(),
                            {
                                "location": loc.name,
                                "date": cur.strftime("%Y-%m-%d"),
                                "time": f"{hour:02d}:{minute:02d}",
                                "garbageLevel": lvl,
                                "latitude": loc.latitude,
                                "longitude": loc.longitude,
                            },
                        )
                        write_count += 1
                        if write_count % 450 == 0:
                            batch.commit()
                            batch = db.batch()
                    cur += timedelta(days=1)
                if write_count % 450 != 0:
                    batch.commit()
                self.stdout.write(self.style.SUCCESS(f"Firestore seeded: {write_count} docs"))

                upcoming_days = 30
                cs_col = db.collection("collector_schedules")
                do_col = db.collection("dropoffSchedules")
                cur2 = start_day
                end2 = today + timedelta(days=upcoming_days)
                while cur2 <= end2:
                    iso_date = cur2.strftime("%Y-%m-%d")
                    ymd = cur2.strftime("%Y%m%d")
                    day_name = cur2.strftime("%A")
                    day_index = int(cur2.weekday())

                    try:
                        batch.delete(cs_col.document(f"{route.id}_{ymd}"))
                        write_count += 1
                        if write_count % 450 == 0:
                            batch.commit()
                            batch = db.batch()
                    except Exception:
                        pass

                    for collector_id in ("1", "2"):
                        cs_doc_id = f"{route.id}_{ymd}_{collector_id}"
                        cs_ref = cs_col.document(cs_doc_id)
                        cs_data = {
                            "date": iso_date,
                            "dayName": day_name,
                            "dayIndex": day_index,
                            "routeId": str(route.id),
                            "routeName": route.name,
                            "collectorId": collector_id,
                            "startTime": "06:00 AM",
                            "endTime": "10:00 PM",
                            "status": "scheduled",
                            "task": "Garbage Collection",
                            "updatedAt": datetime.utcnow(),
                        }
                        batch.set(cs_ref, cs_data, merge=True)
                        write_count += 1
                        if write_count % 450 == 0:
                            batch.commit()
                            batch = db.batch()

                    rng2 = random.Random(int(cur2.strftime("%Y%m%d")))
                    morning_base = 6 * 60
                    evening_base = 15 * 60
                    for idx, loc in enumerate(locations):
                        jitter = int(10 * (rng2.random() - 0.5))
                        tmin1 = morning_base + (idx * 45) + jitter
                        tmin2 = evening_base + (idx * 45) + jitter
                        hh1 = int(tmin1 // 60)
                        mm1 = int(tmin1 % 60)
                        hh2 = int(tmin2 // 60)
                        mm2 = int(tmin2 % 60)
                        time_val_1 = datetime(2000, 1, 1, hh1, mm1).strftime("%I:%M %p")
                        time_val_2 = datetime(2000, 1, 1, hh2, mm2).strftime("%I:%M %p")
                        safe_name = loc.name.replace(" ", "_").replace("/", "-")
                        do_doc_id = f"{iso_date}_{safe_name}"
                        do_ref = do_col.document(do_doc_id)
                        do_data = {
                            "date": iso_date,
                            "day": day_name,
                            "day_index": day_index,
                            "name": loc.name,
                            "times": [time_val_1, time_val_2],
                            "time": time_val_1,
                            "timestamp": datetime.combine(cur2, datetime.strptime(time_val_1, "%I:%M %p").time()),
                        }
                        batch.set(do_ref, do_data, merge=True)
                        write_count += 1
                        if write_count % 450 == 0:
                            batch.commit()
                            batch = db.batch()
                    cur2 += timedelta(days=1)

                if write_count % 450 != 0:
                    batch.commit()
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Firestore write skipped: {e}"))

        created_hist = 0
        try:
            CollectionHistory.objects.filter(route=route).delete()
            cur = start_day
            while cur <= today:
                start_offset = random.randint(-30, 45)
                start_min = max(7 * 60, 8 * 60 + start_offset)
                end_min = start_min + random.randint(90, 180)
                st = time(start_min // 60, start_min % 60)
                et = time(end_min // 60, end_min % 60)
                CollectionHistory.objects.create(route=route, date=cur, start_time=st, end_time=et, notes="seed")
                created_hist += 1
                cur += timedelta(days=1)
            self.stdout.write(self.style.SUCCESS(f"SQL seeded: {created_hist} CollectionHistory rows"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"SQL history seed skipped: {e}"))

        self.stdout.write(self.style.SUCCESS("Seed complete"))
