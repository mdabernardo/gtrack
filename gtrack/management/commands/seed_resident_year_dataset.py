from datetime import date, datetime, timedelta
import random

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.utils import timezone

from gtrack.firebase_sync import _get_firestore_client
from gtrack.models import Route, RoutePoint


class Command(BaseCommand):
    help = "Seed resident-facing Firestore datasets (garbagelevel + dropoffSchedules + scheduling_assistance + collector_schedules) up to an end date."

    def add_arguments(self, parser):
        parser.add_argument("--route_name", type=str, default="Main Route")
        parser.add_argument("--start", type=str, default="")
        parser.add_argument("--end", type=str, default="")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--past_days", type=int, default=0)
        parser.add_argument("--only_collector_schedules", action="store_true")
        parser.add_argument("--only_dropoff_schedules", action="store_true")
        parser.add_argument("--catmon6", action="store_true")

    def handle(self, *args, **opts):
        db = _get_firestore_client()
        if db is None:
            raise CommandError(
                "Firestore client not available. Set FIREBASE_ADMIN_JSON (recommended) "
                "or FIREBASE_CREDENTIALS_PATH / GOOGLE_APPLICATION_CREDENTIALS to a service account key."
            )

        route_name = str(opts.get("route_name") or "Main Route").strip()
        route = Route.objects.filter(name__iexact=route_name).first() or Route.objects.first()
        if not route:
            self.stdout.write(self.style.WARNING("No SQL routes found."))
            return

        use_catmon6 = bool(opts.get("catmon6"))
        if use_catmon6:
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
            locations = []
            for i, name in enumerate(target_names):
                lat = base_lat + (i * 0.0010)
                lng = base_lng + ((i % 3) * 0.0013)
                locations.append({"name": name, "latitude": lat, "longitude": lng})
        else:
            pts = RoutePoint.objects.filter(route=route).order_by("order").select_related("location")
            locations = [
                {"name": (p.location.name or "").strip(), "latitude": p.location.latitude, "longitude": p.location.longitude}
                for p in pts
                if p.location and (p.location.name or "").strip()
            ]
        if not locations:
            self.stdout.write(self.style.WARNING("Selected route has no points."))
            return

        start_str = str(opts.get("start") or "").strip()
        if start_str:
            try:
                start_day = datetime.strptime(start_str, "%Y-%m-%d").date()
            except Exception:
                start_day = timezone.localdate()
        else:
            past_days = int(opts.get("past_days") or 0)
            past_days = max(0, past_days)
            start_day = timezone.localdate() - timedelta(days=past_days)

        end_str = str(opts.get("end") or "").strip()
        if end_str:
            try:
                end_day = datetime.strptime(end_str, "%Y-%m-%d").date()
            except Exception:
                end_day = start_day + timedelta(days=int(opts.get("days") or 30))
        else:
            end_day = start_day + timedelta(days=int(opts.get("days") or 30))

        if end_day < start_day:
            self.stdout.write(self.style.WARNING("End date is before start date."))
            return

        cs_col = db.collection("collector_schedules")
        only_cs = bool(opts.get("only_collector_schedules"))
        only_drop = bool(opts.get("only_dropoff_schedules"))
        garb_col = db.collection("garbagelevel") if (not only_cs and not only_drop) else None
        drop_col = db.collection("dropoffSchedules") if not only_cs else None
        sched_col = db.collection("scheduling_assistance") if (not only_cs and not only_drop) else None

        written = 0
        written_garb = 0
        written_drop = 0
        written_sched = 0
        written_cs = 0
        batch = db.batch()
        ops = 0

        def _weighted_choice(rng: random.Random, items):
            total = 0.0
            for _, w in items:
                try:
                    total += float(w)
                except Exception:
                    continue
            if total <= 0:
                return items[0][0] if items else None
            r = rng.random() * total
            upto = 0.0
            for v, w in items:
                ww = 0.0
                try:
                    ww = float(w)
                except Exception:
                    ww = 0.0
                upto += ww
                if upto >= r:
                    return v
            return items[-1][0]

        def _time_str_from_minutes(total_minutes: int) -> str:
            total_minutes = int(total_minutes)
            hh = int(total_minutes // 60) % 24
            mm = int(total_minutes % 60)
            return datetime(2000, 1, 1, hh, mm).strftime("%I:%M %p")

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

            if only_drop:
                drop_rng = random.Random(rng_seed + 74017)
                names = [str(x.get("name") or "").strip() for x in locations if str(x.get("name") or "").strip()]
                dumpsite_name = None
                for n in names:
                    if n.lower() == "dumpsite":
                        dumpsite_name = n
                        break

                weighted = []
                for n in names:
                    if dumpsite_name and n == dumpsite_name:
                        weighted.append((n, 0.45))
                    else:
                        weighted.append((n, 0.11))
                start_loc = _weighted_choice(drop_rng, weighted) or (dumpsite_name or (names[0] if names else None))

                remaining = [n for n in names if n != start_loc]
                drop_rng.shuffle(remaining)
                ordered = [start_loc] + remaining if start_loc else remaining

                morning_base = (6 * 60) + drop_rng.randint(0, 40)
                afternoon_base = (13 * 60) + drop_rng.randint(0, 90)
                for idx, name in enumerate(ordered):
                    safe_name = name.replace(" ", "_").replace("/", "-")

                    step1 = 35 + drop_rng.randint(-4, 12)
                    step2 = 45 + drop_rng.randint(-6, 14)
                    tmin1 = morning_base + (idx * step1) + drop_rng.randint(-5, 7)
                    tmin2 = afternoon_base + (idx * step2) + drop_rng.randint(-6, 10)

                    latest = 21 * 60
                    tmin1 = max(6 * 60, min(latest, tmin1))
                    tmin2 = max(6 * 60, min(latest, tmin2))

                    time_val_1 = _time_str_from_minutes(tmin1)
                    time_val_2 = _time_str_from_minutes(tmin2)

                    if dumpsite_name and name == dumpsite_name and drop_rng.random() < 0.55:
                        tmin3 = afternoon_base + (idx * step2) + 40 + drop_rng.randint(-10, 15)
                        tmin3 = max(6 * 60, min(latest, tmin3))
                        times = [time_val_1, time_val_2, _time_str_from_minutes(tmin3)]
                    else:
                        times = [time_val_1, time_val_2]

                    drop_doc_id = f"{iso}_{safe_name}"
                    batch.set(
                        drop_col.document(drop_doc_id),
                        {
                            "date": iso,
                            "day": day_name,
                            "day_index": day_index,
                            "name": name,
                            "times": times,
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

                cur += timedelta(days=1)
                continue

            daily_levels = []
            for idx, loc in enumerate(locations):
                name = (str(loc.get("name") or "")).strip()
                safe_name = name.replace(" ", "_").replace("/", "-")

                is_dumpsite = name.lower() == "dumpsite"
                day_rng = random.Random(rng_seed + (idx * 997))
                if is_dumpsite:
                    base = 75 + int(20 * day_rng.random())
                    if day_rng.random() < 0.15:
                        base = 52 + int(18 * day_rng.random())
                else:
                    base = 25 + int(40 * day_rng.random())
                    if day_rng.random() < 0.12:
                        base = 70 + int(25 * day_rng.random())
                lvl = max(0, min(100, int(base)))

                hour = 7 + (idx % 3)
                minute = (5 * (((rng_seed // 7) + idx * 3) % 12 + 1)) % 60
                time_val_1 = datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p")
                time_val_2 = datetime(2000, 1, 1, (hour + 8) % 24, minute).strftime("%I:%M %p")

                lat_val = loc.get("latitude")
                lng_val = loc.get("longitude")
                lat_val = float(lat_val) if lat_val is not None else None
                lng_val = float(lng_val) if lng_val is not None else None

                daily_levels.append(
                    {
                        "name": name,
                        "latitude": lat_val,
                        "longitude": lng_val,
                        "garbageLevel": int(lvl),
                        "time_am": time_val_1,
                        "time_pm": time_val_2,
                    }
                )

                if not only_cs and not only_drop:
                    garb_doc_id = f"{iso}_{safe_name}"
                    batch.set(
                        garb_col.document(garb_doc_id),
                        {
                            "location": name,
                            "date": iso,
                            "time": f"{hour:02d}:{minute:02d}",
                            "garbageLevel": lvl,
                            "latitude": lat_val,
                            "longitude": lng_val,
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

            if not only_cs and not only_drop:
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

            if not only_drop:
                for collector_id in ("1", "2"):
                    cs_doc_id = f"{route.id}_{ymd}_{collector_id}"
                    status = "scheduled"
                    stops = list(daily_levels)
                    stop_rng = random.Random(rng_seed + int(collector_id) * 1337)
                    stops.sort(key=lambda x: (-(int(x.get("garbageLevel") or 0) + stop_rng.randint(-6, 6)), str(x.get("name") or "")))
                    start_minutes = 6 * 60
                    for i, st in enumerate(stops):
                        tmin = start_minutes + (i * 50) + stop_rng.randint(-4, 6)
                        hh = int(tmin // 60) % 24
                        mm = int(tmin % 60)
                        st["plannedTime"] = datetime(2000, 1, 1, hh, mm).strftime("%I:%M %p")
                    payload = {
                        "date": iso,
                        "dayName": day_name,
                        "dayIndex": day_index,
                        "day_name": day_name,
                        "day_index": day_index,
                        "routeId": str(route.id),
                        "routeName": route.name,
                        "route_id": int(route.id),
                        "route_name": route.name,
                        "collectorId": collector_id,
                        "collector_id": collector_id,
                        "collectorIdInt": int(collector_id),
                        "startTime": "06:00 AM",
                        "endTime": "10:00 PM",
                        "status": status,
                        "task": "Garbage Collection",
                        "pickupPlan": {
                            "area": "Catmon, Malabon" if use_catmon6 else "",
                            "locations": stops,
                            "dominantLocation": "Dumpsite",
                        },
                        "updatedAt": datetime.utcnow(),
                    }
                    if cur.month == 12 and str(collector_id) == "1":
                        payload["status"] = "full"
                        payload["capacity_percent"] = 100
                        payload["recommended_action"] = "go_to_dropoff_then_continue_or_delegate"
                    batch.set(
                        cs_col.document(cs_doc_id),
                        payload,
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
