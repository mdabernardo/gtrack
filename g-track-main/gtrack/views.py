from django.shortcuts import render
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, action
from rest_framework.response import Response
from django.utils import timezone
from datetime import datetime, timedelta
import json

from .models import (
    Location, Route, RoutePoint, CollectionSchedule, 
    CollectionHistory, GarbageCollector, Resident,
    AIRoutePrediction, Notification
)
from .serializers import (
    LocationSerializer, RouteSerializer, RoutePointSerializer,
    CollectionScheduleSerializer, CollectionHistorySerializer,
    GarbageCollectorSerializer, ResidentSerializer,
    AIRoutePredictionSerializer, NotificationSerializer
)
from .ai_predictor import GarbageRoutePredictor
from .firebase import firebase_manager

# Initialize the AI predictor
ai_predictor = GarbageRoutePredictor()

def index(request):
    """Render the main application page."""
    return render(request, 'index.html')

class LocationViewSet(viewsets.ModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer

class RouteViewSet(viewsets.ModelViewSet):
    queryset = Route.objects.all()
    serializer_class = RouteSerializer
    
    @action(detail=True, methods=['get'])
    def points(self, request, pk=None):
        """Get all points for a specific route."""
        route = self.get_object()
        points = RoutePoint.objects.filter(route=route).order_by('order')
        serializer = RoutePointSerializer(points, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def schedules(self, request, pk=None):
        """Get all schedules for a specific route."""
        route = self.get_object()
        schedules = CollectionSchedule.objects.filter(route=route)
        serializer = CollectionScheduleSerializer(schedules, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        """Get collection history for a specific route."""
        route = self.get_object()
        history = CollectionHistory.objects.filter(route=route).order_by('-date')
        serializer = CollectionHistorySerializer(history, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def predictions(self, request, pk=None):
        """Get AI predictions for a specific route."""
        route = self.get_object()
        days = int(request.query_params.get('days', 7))
        
        # Get existing predictions
        start_date = timezone.now().date()
        end_date = start_date + timedelta(days=days)
        existing_predictions = AIRoutePrediction.objects.filter(
            route=route,
            date__gte=start_date,
            date__lt=end_date
        ).order_by('date')
        
        # If we don't have enough predictions, generate new ones
        if existing_predictions.count() < days:
            for i in range(days):
                target_date = start_date + timedelta(days=i)
                # Check if prediction exists for this date
                if not existing_predictions.filter(date=target_date).exists():
                    # Generate new prediction
                    prediction = ai_predictor.predict_route_schedule(route.id, target_date)
                    if prediction:
                        ai_predictor.save_prediction(route.id, target_date, prediction)
            
            # Refresh the queryset
            existing_predictions = AIRoutePrediction.objects.filter(
                route=route,
                date__gte=start_date,
                date__lt=end_date
            ).order_by('date')
        
        serializer = AIRoutePredictionSerializer(existing_predictions, many=True)
        return Response(serializer.data)

class RoutePointViewSet(viewsets.ModelViewSet):
    queryset = RoutePoint.objects.all()
    serializer_class = RoutePointSerializer

class CollectionScheduleViewSet(viewsets.ModelViewSet):
    queryset = CollectionSchedule.objects.all()
    serializer_class = CollectionScheduleSerializer
    
    def get_queryset(self):
        """Filter schedules by day of week if provided."""
        queryset = CollectionSchedule.objects.all()
        day = self.request.query_params.get('day', None)
        if day is not None:
            try:
                day_int = int(day)
                queryset = queryset.filter(day_of_week=day_int)
            except ValueError:
                pass
        return queryset

class CollectionHistoryViewSet(viewsets.ModelViewSet):
    queryset = CollectionHistory.objects.all()
    serializer_class = CollectionHistorySerializer
    
    def get_queryset(self):
        """Filter history by date range if provided."""
        queryset = CollectionHistory.objects.all()
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__gte=start)
            except ValueError:
                pass
        
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__lte=end)
            except ValueError:
                pass
        
        return queryset.order_by('-date')

class GarbageCollectorViewSet(viewsets.ModelViewSet):
    queryset = GarbageCollector.objects.all()
    serializer_class = GarbageCollectorSerializer
    
    @action(detail=True, methods=['get'])
    def routes(self, request, pk=None):
        """Get all routes assigned to a specific collector."""
        collector = self.get_object()
        routes = collector.assigned_routes.all()
        serializer = RouteSerializer(routes, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def today_schedule(self, request, pk=None):
        """Get today's schedule for a specific collector."""
        collector = self.get_object()
        today = timezone.now().date()
        day_of_week = today.weekday()
        
        # Get routes assigned to this collector
        routes = collector.assigned_routes.all()
        route_ids = [route.id for route in routes]
        
        # Get schedules for today
        schedules = CollectionSchedule.objects.filter(
            route__in=route_ids,
            day_of_week=day_of_week,
            is_active=True
        )
        
        # Get AI predictions for today
        predictions = AIRoutePrediction.objects.filter(
            route__in=route_ids,
            date=today
        )
        
        # Combine schedule and prediction data
        result = []
        for schedule in schedules:
            route_data = {
                'route_id': schedule.route.id,
                'route_name': schedule.route.name,
                'scheduled_start': schedule.start_time,
                'scheduled_end': schedule.end_time,
                'predicted_start': None,
                'predicted_end': None,
                'confidence': None
            }
            
            # Add prediction data if available
            prediction = predictions.filter(route=schedule.route).first()
            if prediction:
                route_data.update({
                    'predicted_start': prediction.predicted_start_time,
                    'predicted_end': prediction.predicted_end_time,
                    'confidence': prediction.confidence_score
                })
            
            result.append(route_data)
        
        return Response(result)

class ResidentViewSet(viewsets.ModelViewSet):
    queryset = Resident.objects.all()
    serializer_class = ResidentSerializer
    
    @action(detail=True, methods=['get'])
    def nearby_routes(self, request, pk=None):
        """Get routes near a resident's location."""
        resident = self.get_object()
        if not resident.location:
            return Response(
                {"error": "Resident location not set"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Find routes with points near the resident's location
        # This is a simplified version - in a real app, you'd use geospatial queries
        # For now, we'll just return all routes
        routes = Route.objects.all()
        serializer = RouteSerializer(routes, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def upcoming_collections(self, request, pk=None):
        """Get upcoming garbage collections for a resident's location."""
        resident = self.get_object()
        if not resident.location:
            return Response(
                {"error": "Resident location not set"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get today's date and the next 7 days
        today = timezone.now().date()
        days_ahead = int(request.query_params.get('days', 7))
        
        # Find routes near the resident's location
        # This is a simplified version - in a real app, you'd use geospatial queries
        # For now, we'll just return predictions for all routes
        routes = Route.objects.all()
        route_ids = [route.id for route in routes]
        
        # Get predictions for the next 7 days
        predictions = []
        for i in range(days_ahead):
            target_date = today + timedelta(days=i)
            day_predictions = AIRoutePrediction.objects.filter(
                route__in=route_ids,
                date=target_date
            )
            
            for prediction in day_predictions:
                predictions.append({
                    'date': prediction.date,
                    'route_name': prediction.route.name,
                    'predicted_start_time': prediction.predicted_start_time,
                    'predicted_end_time': prediction.predicted_end_time,
                    'confidence': prediction.confidence_score
                })
        
        return Response(predictions)

class AIRoutePredictionViewSet(viewsets.ModelViewSet):
    queryset = AIRoutePrediction.objects.all()
    serializer_class = AIRoutePredictionSerializer
    
    def get_queryset(self):
        """Filter predictions by date range if provided."""
        queryset = AIRoutePrediction.objects.all()
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__gte=start)
            except ValueError:
                pass
        
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__lte=end)
            except ValueError:
                pass
        
        return queryset.order_by('date')

class NotificationViewSet(viewsets.ModelViewSet):
    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer
    
    def get_queryset(self):
        """Filter notifications by user if provided."""
        queryset = Notification.objects.all()
        user_id = self.request.query_params.get('user_id', None)
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        return queryset.order_by('-created_at')
    
    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark a notification as read."""
        notification = self.get_object()
        notification.is_read = True
        notification.save()
        return Response({'status': 'notification marked as read'})

@api_view(['GET'])
def get_today_predictions(request):
    """Get predictions for all routes for today."""
    today = timezone.now().date()
    
    # Get all routes
    routes = Route.objects.all()
    
    # Get predictions for today
    predictions = AIRoutePrediction.objects.filter(date=today)
    
    # If we don't have predictions for all routes, generate them
    if predictions.count() < routes.count():
        for route in routes:
            if not predictions.filter(route=route).exists():
                # Generate new prediction
                prediction = ai_predictor.predict_route_schedule(route.id, today)
                if prediction:
                    ai_predictor.save_prediction(route.id, today, prediction)
        
        # Refresh the queryset
        predictions = AIRoutePrediction.objects.filter(date=today)
    
    serializer = AIRoutePredictionSerializer(predictions, many=True)
    return Response(serializer.data)

@api_view(['GET'])
def get_week_predictions(request):
    """Get predictions for all routes for the next 7 days."""
    # Use the AI predictor to get predictions for the next 7 days
    predictions = ai_predictor.get_route_predictions(days_ahead=7)
    return Response(predictions)

@api_view(['POST'])
def train_ai_model(request):
    """Manually trigger AI model training."""
    success = ai_predictor.train_model()
    if success:
        return Response({'status': 'AI model trained successfully'})
    else:
        return Response(
            {'status': 'Not enough data to train the model'}, 
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['POST'])
def generate_daily_notifications(request):
    """Generate notifications for residents based on today's predictions."""
    today = timezone.now().date()
    # Ensure predictions exist
    routes = Route.objects.all()
    for route in routes:
        if not AIRoutePrediction.objects.filter(route=route, date=today).exists():
            prediction = ai_predictor.predict_route_schedule(route.id, today)
            if prediction:
                ai_predictor.save_prediction(route.id, today, prediction)

    # Fetch predictions
    predictions = AIRoutePrediction.objects.filter(date=today)
    route_prediction_map = {p.route_id: p for p in predictions}

    # Notify residents (simple: notify all residents; can be filtered by proximity later)
    residents = Resident.objects.filter(notification_enabled=True)
    created = 0
    pushed = 0
    for resident in residents:
        # Choose a relevant route prediction; in a real app, match by proximity
        for p in predictions:
            title = f"Garbage Collection Today: {p.route.name}"
            message = (
                f"Predicted window: {p.predicted_start_time.strftime('%H:%M')} - "
                f"{p.predicted_end_time.strftime('%H:%M')} (confidence: {int(p.confidence_score*100)}%)."
            )
            Notification.objects.create(
                user=resident.user,
                type='schedule',
                title=title,
                message=message
            )
            created += 1
            # Attempt push via Firebase if token exists
            if resident.fcm_token:
                if firebase_manager.send_push_notification(
                    token=resident.fcm_token, title=title, body=message,
                    data={'route': p.route.name, 'date': today.strftime('%Y-%m-%d')}
                ):
                    pushed += 1

    return Response({'status': 'notifications generated', 'created': created, 'pushed': pushed})