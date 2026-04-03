from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from gtrack.firebase_sync import _get_firestore_client
from gtrack.models import Route, RoutePoint


class Command(BaseCommand):
    help = "Seed resident-facing Firestore datasets (garbagelevel + dropoffSchedules + scheduling_assistance + collector_schedules) up to an end date."

    def add_arguments(self, parser):
        parser.add_argument("--route_name", type=str, default="Main Route")
        parser.add_argument("--start", type=str, default="")
        parser.add_argument("--end", type=str, default="2027-01-31")

    def handle(self, *args, **opts):
        db = _get_firestore_client()
        if db is None:
            self.stdout.write(self.style.WARNING("Firestore client not available."))
            return

        route_name = str(opts.get("route_name") or "Main Route").strip()
        route = Route.objects.filter(name__iexact=route_name).first() or Route.objects.first()
        if not route:
            self.stdout.write(self.style.WARNING("No SQL routes found."))
            return

        pts = RoutePoint.objects.filter(route=route).order_by("order").select_related("location")
        locations = [p.location for p in pts if p.location and (p.location.name or "").strip()]
        if not locations:
            self.stdout.write(self.style.WARNING("Selected route has no points."))
            return

        start_str = str(opts.get("start") or "").strip()
        end_str = str(opts.get("end") or "2027-01-31").strip()
        try:
            end_day = datetime.strptime(end_str, "%Y-%m-%d").date()
        except Exception:
            end_day = date(2027, 1, 31)

        if start_str:
            try:
                start_day = datetime.strptime(start_str, "%Y-%m-%d").date()
            except Exception:
                start_day = timezone.localdate()
        else:
            start_day = timezone.localdate()

        if end_day < start_day:
            self.stdout.write(self.style.WARNING("End date is before start date."))
            return

        garb_col = db.collection("garbagelevel")
        drop_col = db.collection("dropoffSchedules")
        sched_col = db.collection("scheduling_assistance")
        cs_col = db.collection("collector_schedules")

        written = 0
        written_garb = 0
        written_drop = 0
        written_sched = 0
        written_cs = 0
        batch = db.batch()
        ops = 0

        def commit():
            nonlocal batch, ops
            if ops:
                batch.commit()
                batch = db.batch()
                ops = 0

        cur = start_day
        while cur <= end_day:
            iso = cur.strftime("%Y-%m-%d")
            ymd = cur.strftime("%Y%m%d")
            day_name = cur.strftime("%A")
            day_index = int(cur.weekday())

            rng_seed = int(cur.strftime("%Y%m%d"))
            for idx, loc in enumerate(locations):
                name = (loc.name or "").strip()
                safe_name = name.replace(" ", "_").replace("/", "-")

                is_dumpsite = name.lower() == "dumpsite"
                base = (70 + ((rng_seed + idx * 17) % 25)) if is_dumpsite else (30 + ((rng_seed + idx * 19) % 45))
                spike = ((rng_seed + idx * 31) % 100) < (18 if is_dumpsite else 10)
                if spike:
                    base = min(100, base + (15 if is_dumpsite else 30))
                lvl = max(0, min(100, int(base)))

                hour = 7 + (idx % 3)
                minute = (5 * (((rng_seed // 7) + idx * 3) % 12 + 1)) % 60
                time_val_1 = datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p")
                time_val_2 = datetime(2000, 1, 1, (hour + 8) % 24, minute).strftime("%I:%M %p")

                garb_doc_id = f"{iso}_{safe_name}"
                batch.set(
                    garb_col.document(garb_doc_id),
                    {
                        "location": name,
                        "date": iso,
                        "time": f"{hour:02d}:{minute:02d}",
                        "garbageLevel": lvl,
                        "latitude": float(loc.latitude) if loc.latitude is not None else None,
                        "longitude": float(loc.longitude) if loc.longitude is not None else None,
                    },
                    merge=True,
                )
                ops += 1
                written += 1
                written_garb += 1

                drop_doc_id = f"{iso}_{safe_name}"
                batch.set(
                    drop_col.document(drop_doc_id),
                    {
                        "date": iso,
                        "day": day_name,
                        "day_index": day_index,
                        "name": name,
                        "times": [time_val_1, time_val_2],
                        "time": time_val_1,
                        "timestamp": datetime.combine(cur, datetime.strptime(time_val_1, "%I:%M %p").time()),
                    },
                    merge=True,
                )
                ops += 1
                written += 1
                written_drop += 1

                if ops >= 450:
                    commit()

            sched_doc_id = f"{route.id}_{ymd}"
            batch.set(
                sched_col.document(sched_doc_id),
                {
                    "route_id": route.id,
                    "route_name": route.name,
                    "date": iso,
                    "predicted_start": "06:00 AM",
                    "predicted_end": "10:00 PM",
                    "confidence": 0.8,
                    "etas": [],
                    "updated_at": datetime.utcnow(),
                },
                merge=True,
            )
            ops += 1
            written += 1
            written_sched += 1
            if ops >= 450:
                commit()

            for collector_id in ("1", "2"):
                cs_doc_id = f"{route.id}_{ymd}_{collector_id}"
                batch.set(
                    cs_col.document(cs_doc_id),
                    {
                        "date": iso,
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
                    },
                    merge=True,
                )
                ops += 1
                written += 1
                written_cs += 1
                if ops >= 450:
                    commit()

            cur += timedelta(days=1)

        commit()
        self.stdout.write(
            self.style.SUCCESS(
                "Seed complete: "
                f"garbagelevel={written_garb} dropoffSchedules={written_drop} "
                f"scheduling_assistance={written_sched} collector_schedules={written_cs} "
                f"total={written} through {end_day.isoformat()}"
            )
        )
