from datetime import datetime

from django.core.management.base import BaseCommand

from gtrack.ai_predictor import GarbageRoutePredictor
from gtrack.firebase_sync import _get_firestore_client
from gtrack.models import Route, RoutePoint


class Command(BaseCommand):
    help = "Reset reroutr so it contains only one document per road report (no date-based history)."

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

        reports = []
        for col in ("road_reports", "road_report"):
            try:
                docs = db.collection(col).get()
            except Exception:
                continue
            for doc in docs:
                data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                if not isinstance(data, dict):
                    continue
                d = dict(data)
                d["id"] = doc.id
                d["__collection__"] = col
                reports.append(d)

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

        removed_reroutes = 0
        scanned_reroutes = 0
        try:
            reroute_docs = db.collection("reroutr").get()
        except Exception:
            reroute_docs = []
        for doc in reroute_docs:
            scanned_reroutes += 1
            try:
                batch.delete(db.collection("reroutr").document(doc.id))
                ops += 1
                removed_reroutes += 1
                if ops >= 350:
                    commit_batch()
            except Exception:
                continue
        commit_batch()

        predictor = GarbageRoutePredictor()
        allowed = {
            "sitio 6 basketball court",
            "gulayan",
            "sm hoa",
            "lucas compound",
            "justice",
            "dumpsite",
        }

        written = 0
        failed = 0
        first_error = None
        for r in reports:
            report_id = str(r.get("id") or "").strip()
            report_col = str(r.get("__collection__") or "road_report").strip() or "road_report"
            if not report_id:
                continue

            reroute_id = f"{report_col}_{report_id}".replace("/", "_")
            result = predictor.optimize_route_by_garbage_level(route.id, explain=True, mirror=False, road_reports=[r])
            suggested_points = result.get("suggested_points") or []
            filtered = []
            for p in suggested_points:
                nm = str(p.get("location_name") or p.get("locationName") or "").strip().lower()
                if nm in allowed:
                    filtered.append(p)
            if filtered:
                suggested_points = filtered

            payload = {
                "id": reroute_id,
                "route_id": route.id,
                "routeId": str(route.id),
                "route_name": route.name,
                "routeName": route.name,
                "date": datetime.utcnow().date().strftime("%Y-%m-%d"),
                "suggested_points": list(suggested_points),
                "suggestedPoints": list(suggested_points),
                "factors": result.get("factors") or {},
                "generated_at": result.get("generated_at") or datetime.utcnow().isoformat(),
                "generatedAt": result.get("generated_at") or datetime.utcnow().isoformat(),
                "status": "approved",
                "road_report": dict(r),
            }
            trace = result.get("trace")
            if isinstance(trace, dict):
                payload["trace"] = trace

            try:
                db.collection("reroutr").document(reroute_id).set(payload, merge=True)
                written += 1
            except Exception as e:
                failed += 1
                if first_error is None:
                    first_error = f"{type(e).__name__}: {e}"

        self.stdout.write(
            self.style.SUCCESS(
                f"reroutr reset: deleted={removed_reroutes}/{scanned_reroutes} rebuilt={written}/{len(reports)} failed={failed} error={first_error or 'none'}"
            )
        )
