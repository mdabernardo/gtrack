from datetime import datetime

from django.core.management.base import BaseCommand

from gtrack.ai_predictor import GarbageRoutePredictor
from gtrack.firebase_sync import _get_firestore_client, sync_optimization_to_firestore
from gtrack.models import Route, RoutePoint


class Command(BaseCommand):
    help = "Remove demo road reports/reroutes and regenerate reroutr/route_suggestion with real Main Route (6 locations) data."

    def handle(self, *args, **opts):
        db = _get_firestore_client()
        if db is None:
            self.stdout.write(self.style.WARNING("Firestore client not available."))
            return

        route = Route.objects.filter(name__iexact="Main Route").first() or Route.objects.first()
        if not route:
            self.stdout.write(self.style.WARNING("No SQL routes found."))
            return

        pts = RoutePoint.objects.filter(route=route).order_by("order").select_related("location")
        target_names = []
        for p in pts:
            nm = (p.location.name or "").strip()
            if nm:
                target_names.append(nm)
        if not target_names:
            self.stdout.write(self.style.WARNING("Main Route has no points; unable to generate reroute suggestions."))
            return

        removed_reports = 0
        scanned_reports = 0
        batch = db.batch()
        ops = 0

        def commit_batch():
            nonlocal batch, ops
            if ops:
                try:
                    batch.commit()
                except Exception:
                    pass
                batch = db.batch()
                ops = 0

        def should_delete_report(data: dict) -> bool:
            loc = str((data or {}).get("location") or "").strip().lower()
            desc = str((data or {}).get("description") or "").strip().lower()
            if "catmon" in loc or "malabon" in loc:
                return True
            if "sample obstruction near catmon" in desc:
                return True
            if "reroute testing" in desc:
                return True
            return False

        for col in ("road_reports", "road_report"):
            try:
                docs = db.collection(col).get()
            except Exception:
                continue
            for doc in docs:
                scanned_reports += 1
                data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                if not isinstance(data, dict):
                    continue
                if not should_delete_report(data):
                    continue
                try:
                    batch.delete(db.collection(col).document(doc.id))
                    ops += 1
                    removed_reports += 1
                    if ops >= 350:
                        commit_batch()
                except Exception:
                    continue

        commit_batch()

        removed_reroutes = 0
        scanned_reroutes = 0
        batch = db.batch()
        ops = 0

        def should_delete_reroute(data: dict) -> bool:
            rn = str((data or {}).get("route_name") or (data or {}).get("routeName") or "").strip().lower()
            rid = (data or {}).get("route_id") or (data or {}).get("routeId")
            pts_list = (data or {}).get("suggested_points") or (data or {}).get("suggestedPoints") or []
            if rn in {"route", "test route reroutr", "test route"}:
                return True
            if rid is not None and str(rid).strip() and str(rid).strip() != str(route.id):
                return True
            if isinstance(pts_list, list) and len(pts_list) and len(pts_list) != len(target_names):
                return True
            return False

        for col in ("reroutr", "route_suggestion"):
            try:
                docs = db.collection(col).get()
            except Exception:
                continue
            for doc in docs:
                scanned_reroutes += 1
                data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                if not isinstance(data, dict):
                    continue
                if not should_delete_reroute(data):
                    continue
                try:
                    batch.delete(db.collection(col).document(doc.id))
                    ops += 1
                    removed_reroutes += 1
                    if ops >= 350:
                        commit_batch()
                except Exception:
                    continue

        commit_batch()

        predictor = GarbageRoutePredictor()
        result = predictor.optimize_route_by_garbage_level(route.id)
        suggested_points = result.get("suggested_points") or []
        factors = result.get("factors") or {}
        generated_at = result.get("generated_at") or datetime.utcnow().isoformat()

        ok = sync_optimization_to_firestore(
            route_id=route.id,
            route_name=route.name,
            optimization_date=datetime.utcnow().date(),
            suggested_points=suggested_points,
            factors=factors,
            generated_at=generated_at,
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Road Map data cleaned and regenerated. "
                f"road_reports scanned={scanned_reports} removed={removed_reports}; "
                f"reroutes scanned={scanned_reroutes} removed={removed_reroutes}; "
                f"generated_today={'yes' if ok else 'no'}"
            )
        )
