from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

class Command(BaseCommand):
    help = 'Recompute AI route predictions and publish to Firestore.'

    def add_arguments(self, parser):
        parser.add_argument('--route', type=int, default=None, help='Compute for a specific route id')
        parser.add_argument('--days', type=int, default=7, help='Number of days ahead to compute')

    def handle(self, *args, **options):
        from gtrack.ai_predictor import GarbageRoutePredictor
        route_id = options.get('route')
        days = int(options.get('days') or 7)
        today = timezone.localdate()

        predictor = GarbageRoutePredictor()
        generated = []
        for i in range(days):
            target_date = today + timedelta(days=i)
            try:
                if route_id:
                    pred = predictor.predict_route_schedule(route_id, target_date)
                    if pred:
                        predictor.save_prediction(route_id, target_date, pred)
                        generated.append(target_date.strftime('%Y-%m-%d'))
                else:
                    from gtrack.models import Route
                    for route in Route.objects.all().iterator():
                        pred = predictor.predict_route_schedule(route.id, target_date)
                        if pred:
                            predictor.save_prediction(route.id, target_date, pred)
                            generated.append(f"{route.id}:{target_date.strftime('%Y-%m-%d')}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Failed for date={target_date} route={route_id or 'ALL'}: {e}"))

        self.stdout.write(self.style.SUCCESS('Recompute complete. Predictions saved and mirrored to Firestore.'))
        self.stdout.write(f"Generated: {', '.join(generated) if generated else 'None'}")