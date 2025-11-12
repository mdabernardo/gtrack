from django.contrib import admin
from .models import (
    Location, Route, RoutePoint, CollectionSchedule,
    CollectionHistory, GarbageCollector, Resident,
    AIRoutePrediction, Notification
)

@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "latitude", "longitude")
    search_fields = ("name", "address")

@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "updated_at")
    search_fields = ("name",)

@admin.register(RoutePoint)
class RoutePointAdmin(admin.ModelAdmin):
    list_display = ("route", "location", "order", "estimated_time_minutes")
    list_filter = ("route",)

@admin.register(CollectionSchedule)
class CollectionScheduleAdmin(admin.ModelAdmin):
    list_display = ("route", "day_of_week", "start_time", "end_time", "is_active")
    list_filter = ("day_of_week", "is_active")

@admin.register(CollectionHistory)
class CollectionHistoryAdmin(admin.ModelAdmin):
    list_display = ("route", "date", "start_time", "end_time", "weather_condition", "traffic_condition")
    list_filter = ("route", "date")

@admin.register(GarbageCollector)
class GarbageCollectorAdmin(admin.ModelAdmin):
    list_display = ("user", "phone_number")
    filter_horizontal = ("assigned_routes",)

@admin.register(Resident)
class ResidentAdmin(admin.ModelAdmin):
    list_display = ("user", "location", "notification_enabled")

@admin.register(AIRoutePrediction)
class AIRoutePredictionAdmin(admin.ModelAdmin):
    list_display = ("route", "date", "predicted_start_time", "predicted_end_time", "confidence_score")
    list_filter = ("route", "date")

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "type", "title", "is_read", "created_at")
    list_filter = ("type", "is_read")