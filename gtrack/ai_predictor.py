import os
import json
from datetime import datetime, timedelta
from collections import defaultdict
from django.conf import settings
from .models import CollectionHistory, Route, AIRoutePrediction
from .firebase_sync import (
    sync_prediction_to_firestore, 
    fetch_garbagelevel_items, 
    sync_optimization_to_firestore,
    fetch_road_reports
)

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
        stats = self.prepare_data_from_firestore(route_id=route_id)
        if stats:
            return stats
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

    def prepare_data_from_firestore(self, route_id=None):
        items = fetch_garbagelevel_items() or []
        if not items:
            return {}
        routes_qs = Route.objects.all()
        if route_id:
            routes_qs = routes_qs.filter(id=route_id)
        loc_to_routes = defaultdict(list)
        for r in routes_qs:
            pts = r.points.select_related('location').all()
            for p in pts:
                nm = (p.location.name or '').strip().lower()
                if nm:
                    loc_to_routes[nm].append(r.id)
        route_date_bounds = {}
        def parse_minutes(t):
            if t is None:
                return None
            s = str(t).strip()
            for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p"):
                try:
                    dt = datetime.strptime(s, fmt)
                    return dt.hour * 60 + dt.minute
                except Exception:
                    continue
            try:
                v = float(s)
                return int(v)
            except Exception:
                return None
        def parse_date(d):
            if d is None:
                return None
            s = str(d).strip()
            for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
                try:
                    return datetime.strptime(s, fmt).date()
                except Exception:
                    continue
            return None
        for it in items:
            loc = str(it.get('location') or '').strip().lower()
            mins = parse_minutes(it.get('time'))
            dt = parse_date(it.get('date'))
            if not loc or mins is None or dt is None:
                continue
            rids = loc_to_routes.get(loc) or []
            for rid in rids:
                key = (rid, dt.isoformat())
                b = route_date_bounds.get(key)
                if not b:
                    route_date_bounds[key] = {'min': mins, 'max': mins, 'weekday': dt.weekday()}
                else:
                    if mins < b['min']:
                        b['min'] = mins
                    if mins > b['max']:
                        b['max'] = mins
        stats = defaultdict(lambda: defaultdict(list))
        for (rid, _dstr), b in route_date_bounds.items():
            start_minutes = b['min']
            duration = max(30, b['max'] - b['min'])
            day_key = f"day_{b['weekday']}"
            stats[f"route_{rid}"][day_key].append({'start_minutes': start_minutes, 'duration': duration})
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

    def optimize_route_by_garbage_level(self, route_id, explain=False, mirror=True):
        """Optimize visit order using garbage level priority and travel distance.
        - Reads current `Route.points` and Firestore `garbagelevel` documents
        - Scores by `garbageLevel` (higher = higher priority)
        - Builds a distance-aware path using a weighted nearest-neighbor heuristic
          that biases toward higher garbage levels
        - Returns a JSON-friendly dict with `suggested_points` in optimized order
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
        
        # Fetch road reports (e.g. obstructions, traffic)
        reports = fetch_road_reports() or []
        report_map = {}
        for r in reports:
            # Assume 'location' field contains the name
            r_loc = str(r.get('location') or '').strip().lower()
            if r_loc:
                report_map[r_loc] = r

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

        trace = None
        if explain:
            trace = {
                'inputs': {},
                'points': [],
                'steps': [],
            }

        # Prepare candidate points
        suggested = []
        matched_count = 0
        for p in points:
            loc = p.location
            key = (loc.name or '').strip().lower()
            
            # Garbage level score
            level_val = name_to_level.get(key)
            if level_val is not None:
                matched_count += 1
            score = float(level_val) if level_val is not None else 0.0
            
            # Road report check
            report = report_map.get(key)
            has_issue = report is not None
            
            suggested.append({
                'point_id': p.id,
                'location_id': loc.id,
                'location_name': loc.name,
                'latitude': loc.latitude,
                'longitude': loc.longitude,
                'original_order': p.order,
                'score': score,
                'has_issue': has_issue,
                'issue_details': report.get('description', 'Reported issue') if report else None
            })

        if trace is not None:
            trace['points'] = [
                {
                    'location_name': sp.get('location_name'),
                    'original_order': sp.get('original_order'),
                    'score': float(sp.get('score') or 0.0),
                    'has_issue': bool(sp.get('has_issue')),
                    'issue_details': sp.get('issue_details'),
                }
                for sp in suggested
            ]

        # Heuristic: weighted nearest neighbor
        # - Start from highest score point (or first by original order)
        # - At each step, choose next point with minimal effective_cost = distance_km / (1 + alpha*score)
        # - alpha controls priority influence; alpha=1.0 gives meaningful bias
        # - Road reports introduce a penalty multiplier to distance (making it "further" effectively)
        def haversine_km(lat1, lon1, lat2, lon2):
            from math import radians, sin, cos, asin, sqrt
            R = 6371.0
            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)
            a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
            c = 2 * asin(sqrt(a))
            return R * c

        alpha = 1.0
        issue_penalty = 10.0 # significant penalty for reported locations
        
        remaining = suggested[:]
        optimized = []
        # pick start - prefer points WITHOUT issues
        if items:
            # Filter out issues for start node if possible
            safe_candidates = [x for x in remaining if not x['has_issue']]
            if safe_candidates:
                start = max(safe_candidates, key=lambda x: x['score'])
                if trace is not None:
                    trace['steps'].append({
                        'type': 'start',
                        'selected': start.get('location_name'),
                        'reason': 'highest_score_without_issue',
                    })
            else:
                start = max(remaining, key=lambda x: x['score'])
                if trace is not None:
                    trace['steps'].append({
                        'type': 'start',
                        'selected': start.get('location_name'),
                        'reason': 'highest_score',
                    })
        else:
            start = min(remaining, key=lambda x: x['original_order'])
            if trace is not None:
                trace['steps'].append({
                    'type': 'start',
                    'selected': start.get('location_name'),
                    'reason': 'original_order',
                })
            
        optimized.append(start)
        remaining.remove(start)
        total_distance_km = 0.0
        while remaining:
            curr = optimized[-1]
            # compute effective cost for each candidate
            best, best_cost, best_dist = None, None, None
            step_candidates = []
            for cand in remaining:
                dist = haversine_km(curr['latitude'], curr['longitude'], cand['latitude'], cand['longitude'])
                
                # Apply penalty if candidate has road issue
                penalty = issue_penalty if cand['has_issue'] else 1.0
                
                # effective cost
                cost = (dist * penalty) / (1.0 + alpha * float(cand.get('score', 0.0)))
                if trace is not None:
                    step_candidates.append({
                        'to': cand.get('location_name'),
                        'distance_km': round(float(dist or 0.0), 3),
                        'penalty': float(penalty),
                        'score': float(cand.get('score') or 0.0),
                        'cost': round(float(cost or 0.0), 6),
                        'has_issue': bool(cand.get('has_issue')),
                    })
                
                if best_cost is None or cost < best_cost:
                    best, best_cost, best_dist = cand, cost, dist
            if trace is not None:
                step_candidates.sort(key=lambda x: x.get('cost', 0.0))
                trace['steps'].append({
                    'type': 'step',
                    'from': curr.get('location_name'),
                    'selected': best.get('location_name') if best else None,
                    'selected_distance_km': round(float(best_dist or 0.0), 3) if best_dist is not None else None,
                    'selected_cost': round(float(best_cost or 0.0), 6) if best_cost is not None else None,
                    'candidates': step_candidates,
                })
            optimized.append(best)
            remaining.remove(best)
            total_distance_km += float(best_dist or 0.0)

        # Assign new order positions
        for idx, sp in enumerate(optimized, start=1):
            sp['order'] = idx

        result = {
            'route_id': route.id,
            'route_name': route.name,
            'suggested_points': optimized,
            'factors': {
                'source': 'firestore.garbagelevel + road_report',
                'items_count': len(items),
                'reports_count': len(reports),
                'matched_points': matched_count,
                'alpha': alpha,
                'issue_penalty': issue_penalty,
                'heuristic': 'weighted_nearest_neighbor_with_penalties',
                'total_distance_km': round(total_distance_km, 3),
            },
            'generated_at': datetime.utcnow().isoformat()
        }
        if trace is not None:
            trace['inputs'] = {
                'items_count': len(items),
                'reports_count': len(reports),
                'matched_points': matched_count,
                'alpha': alpha,
                'issue_penalty': issue_penalty,
                'heuristic': 'weighted_nearest_neighbor_with_penalties',
            }
            result['trace'] = trace

        if mirror:
            try:
                sync_optimization_to_firestore(
                    route_id=route.id,
                    route_name=route.name,
                    optimization_date=datetime.utcnow().date(),
                    suggested_points=optimized,
                    factors=result['factors'],
                    generated_at=result['generated_at'],
                )
            except Exception:
                pass

        return result
