from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
import random

try:
    import firebase_admin
    from firebase_admin import firestore
except Exception:
    firebase_admin = None
    firestore = None

from gtrack.models import Route, RoutePoint
from gtrack.firebase_sync import _get_firestore_client
from gtrack.ai_predictor import GarbageRoutePredictor


class Command(BaseCommand):
    help = "Clear and seed Firestore garbagelevel + route_suggestion dataset for Dec 2025 through Jan 2027."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=str, default="2025-12-01")
        parser.add_argument("--end", type=str, default="2027-01-31")
        parser.add_argument("--route_id", type=int, default=0, help="0 = all routes")
        parser.add_argument("--clear_only", action="store_true")

    def handle(self, *args, **opts):
        start_s = opts.get("start") or "2025-12-01"
        end_s = opts.get("end") or "2027-01-31"
        route_id = int(opts.get("route_id") or 0)
        clear_only = bool(opts.get("clear_only"))

        try:
            start_date = datetime.strptime(start_s, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_s, "%Y-%m-%d").date()
        except Exception:
            self.stderr.write("Invalid --start/--end. Use YYYY-MM-DD.")
            return

        db = _get_firestore_client()
        if db is None:
            self.stderr.write("Firestore not available.")
            return

        routes = Route.objects.all().order_by("id")
        if route_id:
            routes = routes.filter(id=route_id)
        routes = list(routes)
        if not routes:
            self.stderr.write("No routes found.")
            return

        route_points = {}
        all_names = set()
        for r in routes:
            pts = list(RoutePoint.objects.filter(route=r).order_by("order").select_related("location"))
            route_points[r.id] = pts
            for p in pts:
                nm = (p.location.name or "").strip()
                if nm:
                    all_names.add(nm)

        if not all_names:
            self.stderr.write("No route points to seed.")
            return

        self.stdout.write(f"Routes: {len(routes)} • Unique points: {len(all_names)}")

        def commit_batch(batch, pending):
            if pending <= 0:
                return 0
            try:
                batch.commit()
                return pending
            except Exception:
                return 0

        self.stdout.write("Clearing old route_suggestion + garbagelevel docs in date range…")
        deleted = 0
        batch = db.batch()
        ops = 0
        try:
            for snap in db.collection("route_suggestion").get():
                data = snap.to_dict() if hasattr(snap, "to_dict") else {}
                dt = str((data or {}).get("date") or "").strip()
                if not dt:
                    continue
                try:
                    d = datetime.strptime(dt, "%Y-%m-%d").date()
                except Exception:
                    continue
                if d < start_date or d > end_date:
                    continue
                if route_id and not str(snap.id).startswith(str(route_id) + "_"):
                    continue
                batch.delete(db.collection("route_suggestion").document(snap.id))
                ops += 1
                if ops >= 400:
                    deleted += commit_batch(batch, ops)
                    batch = db.batch()
                    ops = 0
            if ops:
                deleted += commit_batch(batch, ops)
        except Exception:
            pass

        batch = db.batch()
        ops = 0
        try:
            for snap in db.collection("garbagelevel").get():
                data = snap.to_dict() if hasattr(snap, "to_dict") else {}
                dt = str((data or {}).get("date") or "").strip()
                if not dt:
                    continue
                try:
                    d = datetime.strptime(dt, "%Y-%m-%d").date()
                except Exception:
                    continue
                if d < start_date or d > end_date:
                    continue
                loc = str((data or {}).get("location") or "").strip().lower()
                if loc and loc not in {x.lower() for x in all_names}:
                    continue
                batch.delete(db.collection("garbagelevel").document(snap.id))
                ops += 1
                if ops >= 400:
                    deleted += commit_batch(batch, ops)
                    batch = db.batch()
                    ops = 0
            if ops:
                deleted += commit_batch(batch, ops)
        except Exception:
            pass

        self.stdout.write(f"Cleared ~{deleted} docs.")
        if clear_only:
            self.stdout.write(self.style.SUCCESS("Done (clear_only)."))
            return

        self.stdout.write("Seeding garbagelevel docs…")
        batch = db.batch()
        ops = 0
        seeded_g = 0
        cur = start_date
        while cur <= end_date:
            rng = random.Random(int(cur.strftime("%Y%m%d")))
            for nm in sorted(all_names):
                base = 20 + int(60 * rng.random())
                if rng.random() < 0.10:
                    base = 70 + int(25 * rng.random())
                lvl = max(0, min(100, base))
                hour = 7 + (rng.randint(0, 3))
                minute = rng.choice([0, 10, 20, 30, 40, 50])
                payload = {
                    "location": nm,
                    "date": cur.strftime("%Y-%m-%d"),
                    "time": f"{hour:02d}:{minute:02d}",
                    "garbageLevel": lvl,
                }
                batch.set(db.collection("garbagelevel").document(), payload)
                ops += 1
                seeded_g += 1
                if ops >= 450:
                    commit_batch(batch, ops)
                    batch = db.batch()
                    ops = 0
            cur += timedelta(days=1)
        if ops:
            commit_batch(batch, ops)

        self.stdout.write(f"Seeded garbagelevel: {seeded_g} docs.")

        self.stdout.write("Generating route_suggestion docs (rotate start)…")
        predictor = GarbageRoutePredictor()
        seeded_rs = 0
        cur = start_date
        while cur <= end_date:
            for r in routes:
                try:
                    predictor.optimize_route_by_garbage_level(
                        r.id,
                        explain=False,
                        mirror=True,
                        optimization_date=cur,
                        min_level=None,
                        start_policy="rotate",
                    )
                    seeded_rs += 1
                except Exception:
                    pass
            cur += timedelta(days=1)

        self.stdout.write(self.style.SUCCESS(f"Seeded route_suggestion: {seeded_rs} docs."))

