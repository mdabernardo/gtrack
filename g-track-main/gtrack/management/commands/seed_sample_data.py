from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import date, datetime, time, timedelta
import random

from gtrack.models import Location, Route, RoutePoint, CollectionSchedule, CollectionHistory, Resident


class Command(BaseCommand):
    help = "Seed sample data: locations, routes, schedules, and 30 days of history"

    def handle(self, *args, **options):
        # Create sample locations
        locs = []
        for i in range(5):
            loc, _ = Location.objects.get_or_create(
                name=f"Barangay {i+1}",
                defaults={
                    'latitude': 14.6 + i*0.01,
                    'longitude': 121.0 + i*0.01,
                    'address': f"Sample Address {i+1}"
                }
            )
            locs.append(loc)

        # Create a route
        route, _ = Route.objects.get_or_create(name="Route A", defaults={'description': 'Central route'})

        # Create route points
        for idx, loc in enumerate(locs):
            RoutePoint.objects.get_or_create(route=route, location=loc, order=idx+1, defaults={'estimated_time_minutes': 10})

        # Create schedules (Mon/Wed/Fri 7:00-10:00)
        for day in [0, 2, 4]:
            CollectionSchedule.objects.get_or_create(
                route=route,
                day_of_week=day,
                defaults={'start_time': time(7, 0), 'end_time': time(10, 0), 'is_active': True}
            )

        # Seed 30 days of history
        today = timezone.now().date()
        for i in range(30):
            d = today - timedelta(days=i)
            dow = d.weekday()
            if dow in [0, 2, 4]:
                # Simulate variability: start time around 7:00 +/- 30 minutes
                delta = random.randint(-20, 30)
                start_min = 7*60 + delta
                end_min = start_min + random.randint(150, 200)  # 2.5-3.3 hours
                start_t = time(start_min // 60, start_min % 60)
                end_t = time((end_min // 60) % 24, end_min % 60)
                CollectionHistory.objects.get_or_create(
                    route=route,
                    date=d,
                    defaults={
                        'start_time': start_t,
                        'end_time': end_t,
                        'notes': 'Auto-generated sample',
                        'weather_condition': random.choice(['sunny', 'cloudy', 'rainy']),
                        'traffic_condition': random.choice(['light', 'normal', 'heavy'])
                    }
                )

        # Create a sample resident
        user, _ = User.objects.get_or_create(username='resident1', defaults={'email': 'resident@example.com'})
        res, _ = Resident.objects.get_or_create(user=user, defaults={'location': locs[0], 'notification_enabled': True})

        self.stdout.write(self.style.SUCCESS('Seeded sample data successfully'))