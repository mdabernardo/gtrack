import os
import json
from datetime import datetime, timedelta
from collections import defaultdict
from django.conf import settings
from .models import CollectionHistory, Route, AIRoutePrediction
from .firebase_sync import sync_prediction_to_firestore, fetch_garbagelevel_items, sync_optimization_to_firestore

# Try optional TensorFlow predictor
try:
    from .tf_predictor import TensorFlowArrivalPredictor
    _tf_predictor = TensorFlowArrivalPredictor()
except Exception:
    _tf_predictor = None

class GarbageRoutePredictor:
    """
    Simplified AI predictor for garbage truck routes.
    - Trains basic statistics from CollectionHistory
    - Predicts start/end times per route/day
    - Persists predictions to AIRoutePrediction
    """
    def __init__(self):
        self.model_path = os.path.join(settings.BASE_DIR, 'models', 'route_stats.json')
        self.route_stats = {}
        self.load_model()

    def load_model(self):
        try:
            if os.path.exists(self.model_path):
                with open(self.model_path, 'r') as f:
                    self.route_stats = json.load(f)
        except Exception:
            self.route_stats = {}

    def save_model(self):
        try:
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            with open(self.model_path, 'w') as f:
                json.dump(self.route_stats, f, indent=2)
        except Exception:
            pass

    def prepare_data(self, route_id=None):
        qs = CollectionHistory.objects.all()
        if route_id:
            qs = qs.filter(route_id=route_id)
        stats = defaultdict(lambda: defaultdict(list))
        for rec in qs:
            start_minutes = rec.start_time.hour * 60 + rec.start_time.minute
            duration = 120
            if rec.end_time:
                end_minutes = rec.end_time.hour * 60 + rec.end_time.minute
                duration = max(30, end_minutes - start_minutes)
            key = f"route_{rec.route_id}"
            day_key = f"day_{rec.date.weekday()}"
            stats[key][day_key].append({
                'start_minutes': start_minutes,
                'duration': duration
            })
        return dict(stats)

    def train_model(self):
        raw_stats = self.prepare_data()
        if not raw_stats:
            return False
        self.route_stats = {}
        for route_key, route_data in raw_stats.items():
            route_id = route_key.split('_')[1]
            self.route_stats[route_id] = {}
            for day_key, day_data in route_data.items():
                if not day_data:
                    continue
                start_times = [d['start_minutes'] for d in day_data]
                durations = [d['duration'] for d in day_data]
                avg_start = sum(start_times) / len(start_times)
                avg_duration = sum(durations) / len(durations)
                self.route_stats[route_id][day_key.split('_')[1]] = {
                    'avg_start_minutes': avg_start,
                    'avg_duration': avg_duration,
                    'sample_count': len(day_data)
                }
        self.save_model()
        return True

    def predict_route_schedule(self, route_id, target_date):
        # Prefer TensorFlow if available
        if _tf_predictor:
            try:
                tf_pred = _tf_predictor.predict_route_schedule(route_id, target_date, context={})
                if tf_pred:
                    return tf_pred
            except Exception:
                pass
        # Fallback to historical stats
        if not self.route_stats:
            if not self.train_model():
                return None
        rid = str(route_id)
        day = str(target_date.weekday())
        route_stats = self.route_stats.get(rid)
        if not route_stats:
            return self._default_prediction(target_date)
        day_stats = route_stats.get(day)
        if not day_stats:
            return self._default_prediction(target_date)
        start_min = day_stats['avg_start_minutes']
        duration = day_stats['avg_duration']
        start_hour = int(start_min // 60) % 24
        start_minute = int(start_min % 60)
        predicted_start_time = datetime.strptime(f"{start_hour}:{start_minute}", "%H:%M").time()
        end_min = start_min + duration
        end_hour = int(end_min // 60) % 24
        end_minute = int(end_min % 60)
        predicted_end_time = datetime.strptime(f"{end_hour}:{end_minute}", "%H:%M").time()
        return {
            'predicted_start_time': predicted_start_time,
            'predicted_end_time': predicted_end_time,
            'confidence_score': 0.5,
            'factors': {'historical_average': 1.0}
        }

    def _default_prediction(self, target_date):
        if target_date.weekday() < 5:
            start_time = datetime.strptime("08:00", "%H:%M").time()
            end_time = datetime.strptime("16:00", "%H:%M").time()
        else:
            start_time = datetime.strptime("09:00", "%H:%M").time()
            end_time = datetime.strptime("15:00", "%H:%M").time()
        return {
            'predicted_start_time': start_time,
            'predicted_end_time': end_time,
            'confidence_score': 0.2,
            'factors': {'default': True}
        }

    def save_prediction(self, route_id, target_date, prediction):
        try:
            AIRoutePrediction.objects.update_or_create(
                route_id=route_id,
                date=target_date,
                defaults={
                    'predicted_start_time': prediction['predicted_start_time'],
                    'predicted_end_time': prediction['predicted_end_time'],
                    'confidence_score': prediction.get('confidence_score', 0.0),
                    'factors': prediction.get('factors', {})
                }
            )
            # Push to Firestore (best-effort)
            try:
                route = Route.objects.get(id=route_id)
                start_str = prediction['predicted_start_time'].strftime('%H:%M')
                end_str = prediction['predicted_end_time'].strftime('%H:%M')
                sync_prediction_to_firestore(
                    route_id=route_id,
                    route_name=route.name,
                    prediction_date=target_date,
                    predicted_start_time=start_str,
                    predicted_end_time=end_str,
                    confidence_score=prediction.get('confidence_score', 0.0),
                    factors=prediction.get('factors', {})
                )
            except Exception:
                pass
            return True
        except Exception:
            return False

    def get_route_predictions(self, days_ahead=7):
        today = datetime.today().date()
        predictions = []
        for route in Route.objects.all():
            for i in range(days_ahead):
                target_date = today + timedelta(days=i)
                pred = self.predict_route_schedule(route.id, target_date)
                if pred:
                    self.save_prediction(route.id, target_date, pred)
                    predictions.append({
                        'route_id': route.id,
                        'route_name': route.name,
                        'date': target_date,
                        'predicted_start_time': pred['predicted_start_time'],
                        'predicted_end_time': pred['predicted_end_time'],
                        'confidence_score': pred['confidence_score']
                    })
        return predictions

    def optimize_route_by_garbage_level(self, route_id):
        """Return a suggested ordering of route points based on Firestore 'garbagelevel'.
        - Reads current `Route.points` and Firestore `garbagelevel` documents
        - Scores by `garbageLevel` (higher = higher priority)
        - Returns a JSON-friendly dict with suggested_points sorted by score
        """
        try:
            route = Route.objects.get(id=route_id)
        except Route.DoesNotExist:
            return {
                'route_id': route_id,
                'route_name': None,
                'suggested_points': [],
                'factors': {'error': 'route_not_found'},
                'generated_at': datetime.utcnow().isoformat()
            }

        # Fetch points with location details
        points = route.points.select_related('location').all()
        if not points:
            return {
                'route_id': route.id,
                'route_name': route.name,
                'suggested_points': [],
                'factors': {'warning': 'no_route_points'},
                'generated_at': datetime.utcnow().isoformat()
            }

        # Fetch garbage level items from Firestore
        items = fetch_garbagelevel_items() or []
        # Build a simple name->level map (case-insensitive)
        name_to_level = {}
        for it in items:
            loc_name = str(it.get('location') or '').strip().lower()
            level = it.get('garbageLevel')
            if level is None:
                level = it.get('garbage_level') or it.get('level')
            try:
                level_val = float(level) if level is not None else None
            except Exception:
                level_val = None
            if loc_name and level_val is not None:
                name_to_level[loc_name] = level_val

        suggested = []
        matched_count = 0
        for p in points:
            loc = p.location
            key = (loc.name or '').strip().lower()
            level_val = name_to_level.get(key)
            if level_val is not None:
                matched_count += 1
            score = float(level_val) if level_val is not None else 0.0
            suggested.append({
                'point_id': p.id,
                'location_id': loc.id,
                'location_name': loc.name,
                'latitude': loc.latitude,
                'longitude': loc.longitude,
                'original_order': p.order,
                'score': score,
            })

        # Sort by score descending if we have data; otherwise keep original order
        if items:
            suggested.sort(key=lambda x: x['score'], reverse=True)
        else:
            suggested.sort(key=lambda x: x['original_order'])

        # Assign new order positions
        for idx, sp in enumerate(suggested, start=1):
            sp['order'] = idx

        result = {
            'route_id': route.id,
            'route_name': route.name,
            'suggested_points': suggested,
            'factors': {
                'source': 'firestore.garbagelevel',
                'items_count': len(items),
                'matched_points': matched_count,
            },
            'generated_at': datetime.utcnow().isoformat()
        }

        # Mirror to Firestore (best-effort)
        try:
            sync_optimization_to_firestore(
                route_id=route.id,
                route_name=route.name,
                optimization_date=datetime.utcnow().date(),
                suggested_points=suggested,
                factors=result['factors'],
                generated_at=result['generated_at'],
            )
        except Exception:
            pass

        return result