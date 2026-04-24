from datetime import datetime, timedelta
import random

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gtrack.firebase_sync import _get_firestore_client
from gtrack.models import Route, RoutePoint


class Command(BaseCommand):
    help = "Seed resident-facing Firestore datasets (garbagelevel + dropoffSchedules + scheduling_assistance + collector_schedules)."

    def add_arguments(self, parser):
        parser.add_argument("--route_name", type=str, default="Main Route")
        parser.add_argument("--route_ids", type=str, default="")
        parser.add_argument("--start", type=str, default="")
        parser.add_argument("--end", type=str, default="")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--past_days", type=int, default=0)
        parser.add_argument("--only_collector_schedules", action="store_true")
        parser.add_argument("--only_dropoff_schedules", action="store_true")
        parser.add_argument("--only_scheduling_assistance", action="store_true")
        parser.add_argument("--catmon6", action="store_true")

    def handle(self, *args, **opts):
        db = _get_firestore_client()
        if db is None:
            raise CommandError(
                "Firestore client not available. Set FIREBASE_ADMIN_JSON (recommended) "
                "or FIREBASE_CREDENTIALS_PATH / GOOGLE_APPLICATION_CREDENTIALS to a service account key."
            )

        route_name = str(opts.get("route_name") or "Main Route").strip()
        route_ids_raw = str(opts.get("route_ids") or "").strip()
        explicit_route_ids = []
        if route_ids_raw:
            for part in route_ids_raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    explicit_route_ids.append(int(part))
                except Exception:
                    continue

        routes = list(Route.objects.filter(name__iexact=route_name))
        if not routes and not explicit_route_ids:
            first = Route.objects.first()
            if not first:
                self.stdout.write(self.style.WARNING("No SQL routes found."))
                return
            routes = [first]

        start_str = str(opts.get("start") or "").strip()
        if start_str:
            try:
                start_day = datetime.strptime(start_str, "%Y-%m-%d").date()
            except Exception:
                start_day = timezone.localdate()
        else:
            past_days = max(0, int(opts.get("past_days") or 0))
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

        only_cs = bool(opts.get("only_collector_schedules"))
        only_drop = bool(opts.get("only_dropoff_schedules"))
        only_sched = bool(opts.get("only_scheduling_assistance"))
        use_catmon6 = bool(opts.get("catmon6"))

        cs_col = db.collection("collector_schedules") if (not only_drop and not only_sched) else None
        garb_col = db.collection("garbagelevel") if (not only_cs and not only_drop and not only_sched) else None
        drop_col = db.collection("dropoffSchedules") if (not only_cs and not only_sched) else None
        sched_col = db.collection("scheduling_assistance") if (not only_cs and not only_drop) else None

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

        def weighted_choice(rng: random.Random, items):
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

        def time_str_from_minutes(total_minutes: int) -> str:
            hh = int(total_minutes // 60) % 24
            mm = int(total_minutes % 60)
            return datetime(2000, 1, 1, hh, mm).strftime("%I:%M %p")

        def time_24h_from_minutes(total_minutes: int) -> str:
            hh = int(total_minutes // 60) % 24
            mm = int(total_minutes % 60)
            return f"{hh:02d}:{mm:02d}"

        def build_locations(route: Route | None):
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
                out = []
                for i, nm in enumerate(target_names):
                    out.append(
                        {
                            "name": nm,
                            "latitude": base_lat + (i * 0.0010),
                            "longitude": base_lng + ((i % 3) * 0.0013),
                        }
                    )
                return out
            if route is None:
                return []
            pts = RoutePoint.objects.filter(route=route).order_by("order").select_related("location")
            out = []
            for p in pts:
                if not getattr(p, "location", None):
                    continue
                nm = (p.location.name or "").strip()
                if not nm:
                    continue
                out.append({"name": nm, "latitude": p.location.latitude, "longitude": p.location.longitude})
            return out

        def round_plan(names, dumpsite_name, seed: int, round_idx: int):
            rng = random.Random(seed + 74017 + (round_idx * 13331))
            if round_idx == 0:
                start_min, start_max, end_min = (5 * 60) + 50, 6 * 60, 10 * 60
                dumpsite_weight = 0.38
            elif round_idx == 1:
                start_min, start_max, end_min = (12 * 60), (13 * 60), (17 * 60)
                dumpsite_weight = 0.24
            else:
                start_min, start_max, end_min = (17 * 60) + 30, (18 * 60) + 30, (22 * 60)
                dumpsite_weight = 0.20

            weighted = []
            for nm in names:
                if dumpsite_name and nm == dumpsite_name:
                    weighted.append((nm, dumpsite_weight))
                else:
                    weighted.append((nm, max(0.01, (1.0 - dumpsite_weight) / max(1, len(names) - (1 if dumpsite_name else 0)))))

            start_loc = weighted_choice(rng, weighted) or (dumpsite_name or (names[0] if names else None))
            remaining = [nm for nm in names if nm != start_loc]
            rng.shuffle(remaining)
            ordered = [start_loc] + remaining if start_loc else remaining

            base = rng.randint(start_min, start_max)
            span = max(10, end_min - base)
            step = int(span // max(1, len(ordered) - 1))

            out = {}
            used = set()
            for idx, nm in enumerate(ordered):
                nm_rng = random.Random(seed + (round_idx * 99991) + (idx * 31337) + (abs(hash(nm)) % 100000))
                jitter = nm_rng.randint(-12, 18)
                tmin = base + (idx * step) + jitter
                tmin = max(start_min, min(end_min, tmin))
                while tmin in used:
                    tmin = min(end_min, tmin + 1)
                used.add(tmin)
                out[nm] = int(tmin)

            return ordered, out

        seed_targets = []
        if explicit_route_ids:
            base_route = routes[0] if routes else None
            base_locations = build_locations(base_route)
            for rid in explicit_route_ids:
                seed_targets.append((int(rid), route_name, base_locations))
        else:
            for r in routes:
                seed_targets.append((int(r.id), r.name, build_locations(r)))

        for route_id, route_display_name, locations in seed_targets:
            if not locations:
                continue

            names = [str(x.get("name") or "").strip() for x in locations if str(x.get("name") or "").strip()]
            if not names:
                continue

            dumpsite_name = next((n for n in names if n.lower() == "dumpsite"), None)
            loc_by_name = {str(x.get("name") or "").strip(): x for x in locations if str(x.get("name") or "").strip()}

            cur = start_day
            while cur <= end_day:
                iso = cur.strftime("%Y-%m-%d")
                ymd = cur.strftime("%Y%m%d")
                day_name = cur.strftime("%A")
                day_index = int(cur.weekday())

                seed = int(cur.strftime("%Y%m%d")) + (int(route_id) * 1000000)
                ordered_morning, minutes_morning = round_plan(names, dumpsite_name, seed, 0)
                _, minutes_afternoon = round_plan(names, dumpsite_name, seed, 1)
                _, minutes_evening = round_plan(names, dumpsite_name, seed, 2)

                times_by_name = {}
                for nm in names:
                    t1 = time_str_from_minutes(minutes_morning.get(nm, 6 * 60))
                    t2 = time_str_from_minutes(minutes_afternoon.get(nm, 13 * 60))
                    t3 = time_str_from_minutes(minutes_evening.get(nm, 19 * 60))
                    times_by_name[nm] = [t1, t2, t3]

                if only_drop:
                    for nm in ordered_morning:
                        safe_name = nm.replace(" ", "_").replace("/", "-")
                        times = times_by_name.get(nm) or ["06:00 AM", "01:00 PM", "07:00 PM"]
                        doc_id = f"{iso}_{safe_name}"
                        batch.set(
                            drop_col.document(doc_id),
                            {
                                "date": iso,
                                "day": day_name,
                                "day_index": day_index,
                                "name": nm,
                                "times": list(times),
                                "time": str(times[0]),
                                "timestamp": datetime.combine(cur, datetime.strptime(times[0], "%I:%M %p").time()),
                            },
                            merge=False,
                        )
                        ops += 1
                        written += 1
                        written_drop += 1
                        if ops >= 450:
                            commit()
                    cur += timedelta(days=1)
                    continue

                if only_sched:
                    sched_etas = []
                    for idx, nm in enumerate(ordered_morning):
                        times = times_by_name.get(nm) or ["06:00 AM", "01:00 PM", "07:00 PM"]
                        sched_etas.append(
                            {
                                "point_id": None,
                                "location_name": nm,
                                "order": idx + 1,
                                "eta": str(times[0]),
                                "times": list(times),
                            }
                        )
                    doc_id = f"{route_id}_{ymd}"
                    batch.set(
                        sched_col.document(doc_id),
                        {
                            "route_id": int(route_id),
                            "route_name": route_display_name,
                            "date": iso,
                            "predicted_start": "06:00 AM",
                            "predicted_end": "10:00 PM",
                            "confidence": 0.8,
                            "factors": {"source": "seed", "rounds": 3},
                            "etas": sched_etas,
                            "updated_at": datetime.utcnow(),
                        },
                        merge=False,
                    )
                    ops += 1
                    written += 1
                    written_sched += 1
                    if ops >= 450:
                        commit()
                    cur += timedelta(days=1)
                    continue

                daily_levels = []
                for idx, nm in enumerate(ordered_morning):
                    loc = loc_by_name.get(nm) or {}
                    lat_val = loc.get("latitude")
                    lng_val = loc.get("longitude")
                    lat_val = float(lat_val) if lat_val is not None else None
                    lng_val = float(lng_val) if lng_val is not None else None

                    day_rng = random.Random(seed + (idx * 997))
                    is_dumpsite = nm.lower() == "dumpsite"
                    if is_dumpsite:
                        base = 75 + int(20 * day_rng.random())
                        if day_rng.random() < 0.15:
                            base = 52 + int(18 * day_rng.random())
                    else:
                        base = 25 + int(40 * day_rng.random())
                        if day_rng.random() < 0.12:
                            base = 70 + int(25 * day_rng.random())
                    lvl = max(0, min(100, int(base)))

                    times = times_by_name.get(nm) or ["06:00 AM", "01:00 PM", "07:00 PM"]
                    time_key = time_24h_from_minutes(minutes_morning.get(nm, 6 * 60))

                    daily_levels.append(
                        {
                            "name": nm,
                            "latitude": lat_val,
                            "longitude": lng_val,
                            "garbageLevel": int(lvl),
                            "time_am": times[0],
                            "time_pm": times[1],
                            "times": list(times),
                            "_time_24h": time_key,
                        }
                    )

                    safe_name = nm.replace(" ", "_").replace("/", "-")
                    if garb_col is not None:
                        batch.set(
                            garb_col.document(f"{iso}_{safe_name}"),
                            {
                                "location": nm,
                                "date": iso,
                                "time": time_key,
                                "garbageLevel": int(lvl),
                                "latitude": lat_val,
                                "longitude": lng_val,
                            },
                            merge=False,
                        )
                        ops += 1
                        written += 1
                        written_garb += 1

                    if drop_col is not None:
                        batch.set(
                            drop_col.document(f"{iso}_{safe_name}"),
                            {
                                "date": iso,
                                "day": day_name,
                                "day_index": day_index,
                                "name": nm,
                                "times": list(times),
                                "time": str(times[0]),
                                "timestamp": datetime.combine(cur, datetime.strptime(times[0], "%I:%M %p").time()),
                            },
                            merge=False,
                        )
                        ops += 1
                        written += 1
                        written_drop += 1

                    if ops >= 450:
                        commit()

                if sched_col is not None:
                    sched_etas = []
                    for idx, nm in enumerate(ordered_morning):
                        times = times_by_name.get(nm) or ["06:00 AM", "01:00 PM", "07:00 PM"]
                        sched_etas.append(
                            {
                                "point_id": None,
                                "location_name": nm,
                                "order": idx + 1,
                                "eta": str(times[0]),
                                "times": list(times),
                            }
                        )
                    batch.set(
                        sched_col.document(f"{route_id}_{ymd}"),
                        {
                            "route_id": int(route_id),
                            "route_name": route_display_name,
                            "date": iso,
                            "predicted_start": "06:00 AM",
                            "predicted_end": "10:00 PM",
                            "confidence": 0.8,
                            "factors": {"source": "seed", "rounds": 3},
                            "etas": sched_etas,
                            "updated_at": datetime.utcnow(),
                        },
                        merge=False,
                    )
                    ops += 1
                    written += 1
                    written_sched += 1
                    if ops >= 450:
                        commit()

                if cs_col is not None:
                    for collector_id in ("1", "2"):
                        stops = [dict(x) for x in daily_levels]
                        stop_rng = random.Random(seed + int(collector_id) * 1337)
                        stops.sort(
                            key=lambda x: (
                                -(int(x.get("garbageLevel") or 0) + stop_rng.randint(-6, 6)),
                                str(x.get("name") or ""),
                            )
                        )
                        start_minutes = 6 * 60
                        end_minutes = 22 * 60
                        step = (end_minutes - start_minutes) // max(1, len(stops) - 1)
                        for i, st in enumerate(stops):
                            tmin = start_minutes + (i * step) + stop_rng.randint(10, 40)
                            tmin = max(start_minutes, min(end_minutes, tmin))
                            st.pop("_time_24h", None)
                            st.pop("time_am", None)
                            st.pop("time_pm", None)
                            st.pop("times", None)

                        payload = {
                            "date": iso,
                            "dayName": day_name,
                            "dayIndex": day_index,
                            "day_name": day_name,
                            "day_index": day_index,
                            "routeId": str(route_id),
                            "routeName": route_display_name,
                            "route_id": int(route_id),
                            "route_name": route_display_name,
                            "collectorId": str(collector_id),
                            "collector_id": str(collector_id),
                            "collectorIdInt": int(collector_id),
                            "startTime": "06:00 AM",
                            "endTime": "10:00 PM",
                            "status": "scheduled",
                            "task": "Garbage Collection",
                            "pickupPlan": {
                                "area": "Catmon, Malabon" if use_catmon6 else "",
                                "locations": stops,
                                "dominantLocation": "Dumpsite",
                            },
                            "updatedAt": datetime.utcnow(),
                        }
                        cs_doc_id = f"{route_id}_{ymd}_{collector_id}"
                        batch.set(cs_col.document(cs_doc_id), payload, merge=False)
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
