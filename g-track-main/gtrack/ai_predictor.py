import json
import os
from datetime import datetime, timedelta
from collections import defaultdict
from django.conf import settings
from .models import CollectionHistory, Route, AIRoutePrediction

class GarbageRoutePredictor:
    """
    AI model for predicting garbage truck routes and schedules based on historical data.
    Uses statistical aggregation to forecast optimal routes and collection times.
    """
    
    def __init__(self):
        self.model_path = os.path.join(settings.BASE_DIR, 'models', 'route_stats.json')
        self.route_stats = {}
        self.load_model()
    
    def load_model(self):
        """Load the trained statistics if they exist, otherwise create new ones."""
        try:
            if os.path.exists(self.model_path):
                with open(self.model_path, 'r') as f:
                    self.route_stats = json.load(f)
                    print("Route statistics loaded successfully")
            else:
                print("No existing statistics found, will train when needed")
        except Exception as e:
            print(f"Error loading statistics: {e}")
    
    def save_model(self):
        """Save the computed statistics to disk."""
        try:
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            with open(self.model_path, 'w') as f:
                json.dump(self.route_stats, f, indent=2)
            print("Route statistics saved successfully")
        except Exception as e:
            print(f"Error saving statistics: {e}")
    
    def prepare_data(self, route_id=None):
        """
        Prepare historical data for analysis.
        
        Args:
            route_id: Optional route ID to filter data for a specific route
            
        Returns:
            dict: Aggregated statistics by route, day of week, and conditions
        """
        # Get historical collection data
        history_queryset = CollectionHistory.objects.all()
        if route_id:
            history_queryset = history_queryset.filter(route_id=route_id)
        
        # Aggregate data by route and conditions
        stats = defaultdict(lambda: defaultdict(list))
        
        for record in history_queryset:
            # Convert time to minutes from midnight for easier calculation
            start_minutes = record.start_time.hour * 60 + record.start_time.minute
            
            # Handle end_time which could be None
            end_minutes = None
            if record.end_time:
                end_minutes = record.end_time.hour * 60 + record.end_time.minute
                duration = end_minutes - start_minutes
            else:
                duration = 120  # Default 2 hours if no end time
            
            # Create key for aggregation
            key = f"route_{record.route_id}"
            day_key = f"day_{record.date.weekday()}"
            weather_key = record.weather_condition or 'normal'
            traffic_key = record.traffic_condition or 'normal'
            
            # Store data points
            stats[key][day_key].append({
                'start_minutes': start_minutes,
                'duration': duration,
                'weather': weather_key,
                'traffic': traffic_key,
                'month': record.date.month
            })
        
        return dict(stats)
    
    def train_model(self):
        """Compute statistical aggregations from historical data."""
        raw_stats = self.prepare_data()
        
        if not raw_stats:
            print("Not enough data to compute statistics")
            return False
        
        # Compute aggregated statistics
        self.route_stats = {}
        
        for route_key, route_data in raw_stats.items():
            route_id = route_key.split('_')[1]
            self.route_stats[route_id] = {}
            
            for day_key, day_data in route_data.items():
                if not day_data:
                    continue
                    
                day_of_week = day_key.split('_')[1]
                
                # Calculate averages and variations
                start_times = [d['start_minutes'] for d in day_data]
                durations = [d['duration'] for d in day_data]
                
                avg_start = sum(start_times) / len(start_times)
                avg_duration = sum(durations) / len(durations)
                
                # Calculate standard deviation manually
                start_variance = sum((x - avg_start) ** 2 for x in start_times) / len(start_times)
                start_std = start_variance ** 0.5
                
                duration_variance = sum((x - avg_duration) ** 2 for x in durations) / len(durations)
                duration_std = duration_variance ** 0.5
                
                # Weather and traffic impact analysis
                weather_impact = self._analyze_condition_impact(day_data, 'weather')
                traffic_impact = self._analyze_condition_impact(day_data, 'traffic')
                
                self.route_stats[route_id][day_of_week] = {
                    'avg_start_minutes': avg_start,
                    'start_std': start_std,
                    'avg_duration': avg_duration,
                    'duration_std': duration_std,
                    'sample_count': len(day_data),
                    'weather_impact': weather_impact,
                    'traffic_impact': traffic_impact
                }
        
        # Save the computed statistics
        self.save_model()
        return True
    
    def _analyze_condition_impact(self, day_data, condition_type):
        """Analyze the impact of weather or traffic conditions on timing."""
        condition_stats = defaultdict(list)
        
        for data_point in day_data:
            condition = data_point[condition_type]
            condition_stats[condition].append(data_point['start_minutes'])
        
        # Calculate average start time for each condition
        impact = {}
        for condition, start_times in condition_stats.items():
            if start_times:
                impact[condition] = sum(start_times) / len(start_times)
        
        return impact
    
    def predict_route_schedule(self, route_id, target_date):
        """
        Predict the start and end times for a specific route on a given date.
        
        Args:
            route_id: The ID of the route to predict
            target_date: The date for which to make the prediction
            
        Returns:
            dict: Predicted start and end times, confidence score, and factors
        """
        # Ensure statistics are computed
        if not self.route_stats:
            success = self.train_model()
            if not success:
                return None
        
        route_id_str = str(route_id)
        target_day = str(target_date.weekday())
        
        # Check if we have statistics for this route and day
        if route_id_str not in self.route_stats:
            return self._default_prediction(target_date)
        
        route_stats = self.route_stats[route_id_str]
        if target_day not in route_stats:
            return self._default_prediction(target_date)
        
        day_stats = route_stats[target_day]
        
        # Base prediction from historical averages
        predicted_start_minutes = day_stats['avg_start_minutes']
        predicted_duration = day_stats['avg_duration']
        
        # Adjust for weather conditions (simplified)
        weather_impact = day_stats.get('weather_impact', {})
        if 'rain' in weather_impact:
            predicted_start_minutes += 15  # Delay for rain
            predicted_duration += 30  # Longer duration in rain
        
        # Adjust for traffic conditions
        traffic_impact = day_stats.get('traffic_impact', {})
        if 'heavy' in traffic_impact:
            predicted_start_minutes += 10  # Delay for heavy traffic
            predicted_duration += 20  # Longer duration in traffic
        
        # Calculate confidence based on sample size and consistency
        sample_count = day_stats['sample_count']
        start_std = day_stats['start_std']
        
        # Higher confidence with more samples and lower variance
        confidence_score = min(0.95, 0.3 + (sample_count / 50) + (1 / (1 + start_std / 60)))
        
        # Convert minutes to time
        start_hour = int(predicted_start_minutes // 60) % 24
        start_minute = int(predicted_start_minutes % 60)
        predicted_start_time = datetime.strptime(f"{start_hour}:{start_minute}", "%H:%M").time()
        
        # Calculate end time
        predicted_end_minutes = predicted_start_minutes + predicted_duration
        end_hour = int(predicted_end_minutes // 60) % 24
        end_minute = int(predicted_end_minutes % 60)
        predicted_end_time = datetime.strptime(f"{end_hour}:{end_minute}", "%H:%M").time()
        
        # Identify important factors
        factors = {
            'historical_average': 0.6,
            'day_of_week': 0.3,
            'sample_size': min(1.0, sample_count / 20),
            'weather_conditions': 0.1,
            'traffic_conditions': 0.1
        }
        
        prediction_result = {
            'predicted_start_time': predicted_start_time,
            'predicted_end_time': predicted_end_time,
            'confidence_score': float(confidence_score),
            'factors': factors
        }
        
        return prediction_result
    
    def _default_prediction(self, target_date):
        """Provide a default prediction when no historical data is available."""
        # Default times based on day of week
        day_of_week = target_date.weekday()
        
        if day_of_week < 5:  # Weekday
            start_time = datetime.strptime("08:00", "%H:%M").time()
            end_time = datetime.strptime("16:00", "%H:%M").time()
        else:  # Weekend
            start_time = datetime.strptime("09:00", "%H:%M").time()
            end_time = datetime.strptime("15:00", "%H:%M").time()
        
        return {
            'predicted_start_time': start_time,
            'predicted_end_time': end_time,
            'confidence_score': 0.3,  # Low confidence for default prediction
            'factors': {'default_schedule': 1.0}
        }
    
    def save_prediction(self, route_id, target_date, prediction_result):
        """
        Save the prediction to the database.
        
        Args:
            route_id: The ID of the route
            target_date: The date of the prediction
            prediction_result: The prediction result dictionary
            
        Returns:
            AIRoutePrediction: The saved prediction object
        """
        try:
            route = Route.objects.get(id=route_id)
            
            # Check if prediction already exists for this route and date
            existing_prediction = AIRoutePrediction.objects.filter(
                route=route,
                date=target_date
            ).first()
            
            if existing_prediction:
                # Update existing prediction
                existing_prediction.predicted_start_time = prediction_result['predicted_start_time']
                existing_prediction.predicted_end_time = prediction_result['predicted_end_time']
                existing_prediction.confidence_score = prediction_result['confidence_score']
                existing_prediction.factors = prediction_result['factors']
                existing_prediction.save()
                return existing_prediction
            else:
                # Create new prediction
                new_prediction = AIRoutePrediction.objects.create(
                    route=route,
                    date=target_date,
                    predicted_start_time=prediction_result['predicted_start_time'],
                    predicted_end_time=prediction_result['predicted_end_time'],
                    confidence_score=prediction_result['confidence_score'],
                    factors=prediction_result['factors']
                )
                return new_prediction
        except Exception as e:
            print(f"Error saving prediction: {e}")
            return None
    
    def get_route_predictions(self, days_ahead=7):
        """
        Generate predictions for all routes for the next specified number of days.
        
        Args:
            days_ahead: Number of days to predict ahead
            
        Returns:
            dict: Dictionary of route predictions by date and route
        """
        predictions = {}
        routes = Route.objects.all()
        
        for i in range(days_ahead):
            target_date = datetime.now().date() + timedelta(days=i)
            date_predictions = {}
            
            for route in routes:
                prediction = self.predict_route_schedule(route.id, target_date)
                if prediction:
                    # Save prediction to database
                    saved_prediction = self.save_prediction(route.id, target_date, prediction)
                    if saved_prediction:
                        date_predictions[route.name] = {
                            'start_time': saved_prediction.predicted_start_time.strftime('%H:%M'),
                            'end_time': saved_prediction.predicted_end_time.strftime('%H:%M'),
                            'confidence': saved_prediction.confidence_score,
                            'key_factors': list(saved_prediction.factors.keys())[:3]
                        }
            
            predictions[target_date.strftime('%Y-%m-%d')] = date_predictions
        
        return predictions