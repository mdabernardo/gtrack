from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from gtrack.ai_predictor import GarbageRoutePredictor
from gtrack.firebase_sync import sync_optimization_to_firestore
from gtrack.models import Route


class Command(BaseCommand):
    help = "Backfill reroute artifacts into Firestore (route_suggestion + reroutr) for Main Route so Road Map shows real data."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--route_name", type=str, default="Main Route")

    def handle(self, *args, **opts):
        days = int(opts.get("days") or 30)
        route_name = str(opts.get("route_name") or "Main Route").strip()

        route = Route.objects.filter(name__iexact=route_name).first() or Route.objects.first()
        if not route:
            self.stdout.write(self.style.WARNING("No SQL routes found."))
            return

        predictor = GarbageRoutePredictor()
        today = timezone.localdate()

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
            if ok:
                written += 1

        self.stdout.write(self.style.SUCCESS(f"Backfilled {written}/{days} reroute artifacts for route '{route.name}'"))
