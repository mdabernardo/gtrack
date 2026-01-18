from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from gtrack.ai_predictor import GarbageRoutePredictor
from gtrack.models import Route
from gtrack.firebase_sync import sync_scheduling_assistance_to_firestore

class Command(BaseCommand):
    help = 'Generate and mirror scheduling assistance artifacts to Firestore for upcoming days.'

    def add_arguments(self, parser):
        parser.add_argument('--route', type=int, default=None, help='Specific route id to process')
        parser.add_argument('--days', type=int, default=7, help='Number of days ahead to generate')

    def handle(self, *args, **options):
        route_id = options.get('route')
        days = int(options.get('days') or 7)
        predictor = GarbageRoutePredictor()
        today = timezone.localdate()

        routes = Route.objects.all()
        if route_id:
            routes = routes.filter(id=route_id)

        total = 0
        for route in routes:
            for i in range(days):
                target_date = today + timedelta(days=i)
                pred = predictor.predict_route_schedule(route.id, target_date)
                if not pred:
                    continue
                start_str = pred['predicted_start_time'].strftime('%H:%M')
                end_str = pred['predicted_end_time'].strftime('%H:%M')
                confidence = float(pred.get('confidence_score', 0.0))
                factors = pred.get('factors', {})
                try:
                    ok = sync_scheduling_assistance_to_firestore(
                        route_id=route.id,
                        route_name=route.name,
                        assistance_date=target_date,
                        predicted_start_time=start_str,
                        predicted_end_time=end_str,
                        confidence_score=confidence,
                        factors=factors,
                    )
                    if ok:
                        total += 1
                except Exception:
                    pass
        self.stdout.write(self.style.SUCCESS(f'Scheduling assistance mirrored: {total} entries'))