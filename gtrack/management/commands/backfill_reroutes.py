from datetime import datetime, timedelta
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from gtrack.ai_predictor import GarbageRoutePredictor
from gtrack.firebase_sync import _get_firestore_client, sync_optimization_to_firestore, sync_reroute_to_firestore
from gtrack.models import Route


class Command(BaseCommand):
    help = "Backfill reroute artifacts into Firestore (route_suggestion + reroutr) for Main Route so Road Map shows real data."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--route_name", type=str, default="Main Route")
        parser.add_argument("--from_reroutr", action="store_true")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--cap_road_reports", type=int, default=0)
        parser.add_argument("--dry_run", action="store_true")

    def handle(self, *args, **opts):
        if opts.get("from_reroutr"):
            db = _get_firestore_client()
            if db is None:
                self.stdout.write(self.style.WARNING("Firestore client not available."))
                return

            limit = int(opts.get("limit") or 0)
            dry_run = bool(opts.get("dry_run"))

            try:
                docs = db.collection("reroutr").get()
            except Exception:
                docs = []

            written = 0
            scanned = 0
            for doc in docs:
                scanned += 1
                if limit and scanned > limit:
                    break
                data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                if not isinstance(data, dict):
                    continue

                payload = dict(data)
                data_string = payload.get("data_string")
                if isinstance(data_string, str) and data_string.strip():
                    try:
                        parsed = json.loads(data_string)
                        if isinstance(parsed, dict):
                            payload = dict(parsed)
                    except Exception:
                        payload = dict(data)

                if "data_string" in payload:
                    del payload["data_string"]

                if not dry_run:
                    try:
                        db.collection("route_suggestion").document(doc.id).set(payload, merge=True)
                    except Exception:
                        continue
                written += 1

            mode = "DRY RUN" if dry_run else "WROTE"
            self.stdout.write(self.style.SUCCESS(f"{mode}: {written}/{scanned} reroutr docs copied to route_suggestion"))
            return

        days = int(opts.get("days") or 30)
        route_name = str(opts.get("route_name") or "Main Route").strip()
        cap_road_reports = int(opts.get("cap_road_reports") or 0)

        route = Route.objects.filter(name__iexact=route_name).first() or Route.objects.first()
        if not route:
            self.stdout.write(self.style.WARNING("No SQL routes found."))
            return

        predictor = GarbageRoutePredictor()
        today = timezone.localdate()

        db = _get_firestore_client()
        if cap_road_reports and db is not None:
            try:
                merged = []
                for col_name in ("road_reports", "road_report"):
                    try:
                        snaps = db.collection(col_name).get()
                    except Exception:
                        snaps = []
                    for snap in snaps:
                        try:
                            ut = getattr(snap, "update_time", None)
                        except Exception:
                            ut = None
                        merged.append((ut, col_name, snap.id))

                merged.sort(
                    key=lambda x: (
                        x[0] if x[0] is not None else datetime.min.replace(tzinfo=timezone.get_current_timezone()),
                        str(x[2] or ""),
                    ),
                    reverse=True,
                )
                keep = set((c, i) for _, c, i in merged[:cap_road_reports])
                to_delete = [(c, i) for _, c, i in merged if (c, i) not in keep]
                if to_delete:
                    if not bool(opts.get("dry_run")):
                        batch = db.batch()
                        ops = 0
                        for col_name, doc_id in to_delete:
                            batch.delete(db.collection(col_name).document(str(doc_id)))
                            ops += 1
                            if ops >= 450:
                                try:
                                    batch.commit()
                                except Exception:
                                    pass
                                batch = db.batch()
                                ops = 0
                        if ops:
                            try:
                                batch.commit()
                            except Exception:
                                pass
                    self.stdout.write(self.style.SUCCESS(f"Road reports capped: kept={len(keep)} deleted={len(to_delete)}"))
            except Exception:
                self.stdout.write(self.style.WARNING("Unable to cap road reports (Firestore operation failed)."))

        written = 0
        for i in range(days):
            d = today - timedelta(days=i)
            result = predictor.optimize_route_by_garbage_level(route.id)
            suggested_points = result.get("suggested_points") or []
            factors = result.get("factors") or {}
            factors = dict(factors)
            factors["generated_for_date"] = d.isoformat()
            factors["source"] = "backfill_reroutes"
            generated_at = datetime(d.year, d.month, d.day, 8, (i % 55), 0).isoformat()

            ok = sync_optimization_to_firestore(
                route_id=route.id,
                route_name=route.name,
                optimization_date=d,
                suggested_points=suggested_points,
                factors=factors,
                generated_at=generated_at,
            )
            reroute_id = f"backfill_{route.id}_{d.strftime('%Y%m%d')}"
            rr_name = ""
            try:
                rr_name = suggested_points[0].get("location_name") or suggested_points[0].get("locationName") or ""
            except Exception:
                rr_name = ""
            rr_payload = {
                "id": reroute_id,
                "location": str(rr_name or route.name),
                "description": "Backfilled reroute record for Road Map display.",
                "status": "processed",
                "source": "backfill_reroutes",
            }
            ok2 = sync_reroute_to_firestore(
                reroute_id=reroute_id,
                route_id=route.id,
                route_name=route.name,
                reroute_date=d,
                suggested_points=suggested_points,
                factors=factors,
                generated_at=generated_at,
                road_report=rr_payload,
            )
            if ok or ok2:
                written += 1

        self.stdout.write(self.style.SUCCESS(f"Backfilled {written}/{days} reroute artifacts for route '{route.name}'"))
