from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from gtrack.ai_predictor import GarbageRoutePredictor
from gtrack.models import Route, RoutePoint
from gtrack.firebase_sync import sync_scheduling_assistance_to_firestore

class Command(BaseCommand):
    help = 'Generate and mirror scheduling assistance artifacts to Firestore for upcoming days.'

    def add_arguments(self, parser):
        parser.add_argument('--route', type=int, default=None, help='Specific route id to process')
        parser.add_argument('--days', type=int, default=7, help='Number of days ahead to generate')
        parser.add_argument('--past_days', type=int, default=0, help='Number of past days to generate (history)')

    def handle(self, *args, **options):
        route_id = options.get('route')
        days = int(options.get('days') or 7)
        past_days = int(options.get('past_days') or 0)
        predictor = GarbageRoutePredictor()
        today = timezone.localdate()

        routes = Route.objects.all()
        if route_id:
            routes = routes.filter(id=route_id)

        total = 0
        for route in routes:
            points = RoutePoint.objects.filter(route=route).order_by('order').select_related('location')
            for offset in range(-past_days, days):
                target_date = today + timedelta(days=offset)
                pred = predictor.predict_route_schedule(route.id, target_date)
                if not pred:
                    continue
                start_str = pred['predicted_start_time'].strftime('%H:%M')
                end_str = pred['predicted_end_time'].strftime('%H:%M')
                confidence = float(pred.get('confidence_score', 0.0))
                factors = pred.get('factors', {})
                etas = []
                cum = 0
                start_tm = pred['predicted_start_time']
                for p in points:
                    eta_hour = (start_tm.hour * 60 + start_tm.minute + cum) // 60
                    eta_min = (start_tm.hour * 60 + start_tm.minute + cum) % 60
                    eta_str = f"{int(eta_hour)%24:02d}:{int(eta_min):02d}"
                    etas.append({
                        'point_id': p.id,
                        'location_name': p.location.name,
                        'order': p.order,
                        'eta': eta_str,
                    })
                    cum += int(p.estimated_time_minutes or 5)
                try:
                    ok = sync_scheduling_assistance_to_firestore(
                        route_id=route.id,
                        route_name=route.name,
                        assistance_date=target_date,
                        predicted_start_time=start_str,
                        predicted_end_time=end_str,
                        confidence_score=confidence,
                        factors=factors,
                        etas=etas,
                    )
                    if ok:
                        total += 1
                except Exception:
                    pass
        self.stdout.write(self.style.SUCCESS(f'Scheduling assistance mirrored: {total} entries'))
