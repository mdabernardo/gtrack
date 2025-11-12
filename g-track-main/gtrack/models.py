from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class Location(models.Model):
    name = models.CharField(max_length=255)
    latitude = models.FloatField()
    longitude = models.FloatField()
    address = models.TextField()
    
    def __str__(self):
        return self.name

class Route(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.name

class RoutePoint(models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='points')
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    order = models.IntegerField()
    estimated_time_minutes = models.IntegerField(default=5)
    
    class Meta:
        ordering = ['order']
    
    def __str__(self):
        return f"{self.route.name} - Point {self.order}"

class CollectionSchedule(models.Model):
    DAYS_OF_WEEK = [
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    ]
    
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='schedules')
    day_of_week = models.IntegerField(choices=DAYS_OF_WEEK)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return f"{self.route.name} - {self.get_day_of_week_display()} ({self.start_time} - {self.end_time})"

class CollectionHistory(models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='history')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    weather_condition = models.CharField(max_length=100, blank=True, null=True)
    traffic_condition = models.CharField(max_length=100, blank=True, null=True)
    
    def __str__(self):
        return f"{self.route.name} - {self.date}"

class GarbageCollector(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=20)
    assigned_routes = models.ManyToManyField(Route, blank=True, related_name='collectors')
    
    def __str__(self):
        return self.user.get_full_name() or self.user.username

class Resident(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    fcm_token = models.CharField(max_length=255, blank=True, null=True)
    notification_enabled = models.BooleanField(default=True)
    
    def __str__(self):
        return self.user.get_full_name() or self.user.username

class AIRoutePrediction(models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='predictions')
    date = models.DateField()
    predicted_start_time = models.TimeField()
    predicted_end_time = models.TimeField()
    confidence_score = models.FloatField(default=0.0)  # 0.0 to 1.0
    factors = models.JSONField(default=dict)  # Store factors that influenced the prediction
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.route.name} - {self.date} Prediction"

class Notification(models.Model):
    NOTIFICATION_TYPES = [
        ('schedule', 'Schedule Reminder'),
        ('delay', 'Delay Alert'),
        ('change', 'Route Change'),
        ('general', 'General Information'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES)
    title = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.title} - {self.created_at.strftime('%Y-%m-%d %H:%M')}"