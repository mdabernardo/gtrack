from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

# Create a router for our API views
router = DefaultRouter()
router.register(r'locations', views.LocationViewSet)
router.register(r'routes', views.RouteViewSet)
router.register(r'route-points', views.RoutePointViewSet)
router.register(r'schedules', views.CollectionScheduleViewSet)
router.register(r'history', views.CollectionHistoryViewSet)
router.register(r'collectors', views.GarbageCollectorViewSet)
router.register(r'residents', views.ResidentViewSet)
router.register(r'predictions', views.AIRoutePredictionViewSet)
router.register(r'notifications', views.NotificationViewSet)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include(router.urls)),
    path('api/today-predictions/', views.get_today_predictions, name='today-predictions'),
    path('api/week-predictions/', views.get_week_predictions, name='week-predictions'),
    path('api/train-model/', views.train_ai_model, name='train-model'),
    path('api/generate-notifications/', views.generate_daily_notifications, name='generate-notifications'),
    path('', views.index, name='index'),
]