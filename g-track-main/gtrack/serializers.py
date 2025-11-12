from rest_framework import serializers
from django.contrib.auth.models import User
from .models import (
    Location, Route, RoutePoint, CollectionSchedule, 
    CollectionHistory, GarbageCollector, Resident,
    AIRoutePrediction, Notification
)

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name']

class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = '__all__'

class RoutePointSerializer(serializers.ModelSerializer):
    location_details = LocationSerializer(source='location', read_only=True)
    
    class Meta:
        model = RoutePoint
        fields = ['id', 'route', 'location', 'location_details', 'order', 'estimated_time_minutes']

class RouteSerializer(serializers.ModelSerializer):
    points = RoutePointSerializer(many=True, read_only=True)
    
    class Meta:
        model = Route
        fields = ['id', 'name', 'description', 'created_at', 'updated_at', 'points']

class CollectionScheduleSerializer(serializers.ModelSerializer):
    day_name = serializers.CharField(source='get_day_of_week_display', read_only=True)
    route_name = serializers.CharField(source='route.name', read_only=True)
    
    class Meta:
        model = CollectionSchedule
        fields = ['id', 'route', 'route_name', 'day_of_week', 'day_name', 'start_time', 'end_time', 'is_active']

class CollectionHistorySerializer(serializers.ModelSerializer):
    route_name = serializers.CharField(source='route.name', read_only=True)
    
    class Meta:
        model = CollectionHistory
        fields = ['id', 'route', 'route_name', 'date', 'start_time', 'end_time', 'notes', 
                 'weather_condition', 'traffic_condition']

class GarbageCollectorSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    assigned_routes_details = RouteSerializer(source='assigned_routes', many=True, read_only=True)
    
    class Meta:
        model = GarbageCollector
        fields = ['id', 'user', 'user_details', 'phone_number', 'assigned_routes', 'assigned_routes_details']

class ResidentSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    location_details = LocationSerializer(source='location', read_only=True)
    
    class Meta:
        model = Resident
        fields = ['id', 'user', 'user_details', 'location', 'location_details', 
                 'phone_number', 'notification_enabled']

class AIRoutePredictionSerializer(serializers.ModelSerializer):
    route_name = serializers.CharField(source='route.name', read_only=True)
    
    class Meta:
        model = AIRoutePrediction
        fields = ['id', 'route', 'route_name', 'date', 'predicted_start_time', 
                 'predicted_end_time', 'confidence_score', 'factors', 'created_at']

class NotificationSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    
    class Meta:
        model = Notification
        fields = ['id', 'user', 'user_details', 'type', 'title', 'message', 
                 'is_read', 'created_at']