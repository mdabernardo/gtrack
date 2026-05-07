# project-level urls.py

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from gtrack.views import profile_view, signup_view

# Assuming your views are in gtrack/views.py
from . import views  
from rest_framework.routers import DefaultRouter

# API router configuration
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
    # Root path: redirect users based on authentication state
    path('', views.root_redirect, name='root'),

    path('admin/', admin.site.urls),
    
    # URL patterns for your custom Firebase-Django authentication
    path('login/', views.login_view, name='login'),
    path('signup/', views.signup_view, name='signup'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('resident-verification/', views.resident_verification_view, name='resident_verification'),
    path('driver-verification/', views.driver_verification_view, name='driver_verification'),
    # Firestore connectivity check (reads collectors/1)
    path('firebase-test/', views.firebase_test_collectors_one, name='firebase_test_collectors_one'),

    # App pages
    path('dashboard/', views.dashboard, name='dashboard'),
    path('track-trucks/', views.track_trucks_view, name='track_trucks'),
    path('track_trucks/', RedirectView.as_view(pattern_name='track_trucks', permanent=False)),
    path('help/', views.help_view, name='help'),
    path('settings/', views.settings_view, name='settings'),
    path('garbage-level/', views.garbage_level_view, name='garbage_level'),
    path('road-map/', views.road_map_view, name='road_map'),
    path('history/', views.history_view, name='history'),
    path('collector-schedules/', views.collector_schedules_view, name='collector_schedules'),
    path('collector-route-suggestions/', views.collector_route_suggestions_view, name='collector_route_suggestions'),
    path('schedules/', views.schedules_view, name='schedules'),
    path('notification/', views.notification_view, name='notification'),
    path('warning/', views.warning_view, name='warning'),
    path('crud_management/', views.crud_management, name='crud_management'),

    # Firebase auth integration
    path('api/auth/firebase_login/', views.firebase_login, name='firebase_login'),
    # Firestore collectors collection
    path('api/firebase/collectors/', views.firebase_collectors_list, name='firebase_collectors_list'),
    path('api/firebase/collectors/<str:doc_id>/', views.firebase_collectors_get, name='firebase_collectors_get'),
    # Firestore dropoffs collection
    path('api/firebase/dropoffs/', views.firebase_dropoffs_list, name='firebase_dropoffs_list'),
    path('api/firebase/dropoffs/<str:doc_id>/', views.firebase_dropoffs_get, name='firebase_dropoffs_get'),
    # Migration: copy drivers -> collectors (admin-only). Add ?delete=true to remove drivers after copy
    path('api/firebase/migrate-drivers-to-collectors/', views.firebase_migrate_drivers_to_collectors, name='firebase_migrate_drivers_to_collectors'),

    # Password reset via email code (OTP)
    path('api/password-reset/send_code/', views.password_reset_send_code, name='password_reset_send_code'),
    path('api/password-reset/verify_code/', views.password_reset_verify_code, name='password_reset_verify_code'),

    # API endpoints
    path('api/', include(router.urls)),
    path('api/today-predictions/', views.get_today_predictions, name='today_predictions'),
    path('api/week-predictions/', views.get_week_predictions, name='week_predictions'),
    path('api/train-model/', views.train_ai_model, name='train_model'),
    path('api/generate-notifications/', views.generate_daily_notifications, name='generate_notifications'),
    path('api/generate-verification-notifications/', views.generate_verification_notifications, name='generate_verification_notifications'),
    path('api/resident-verification/submit/', views.resident_verification_submit, name='resident_verification_submit'),
    path('api/resident-verification/requests/', views.resident_verification_requests, name='resident_verification_requests'),
    path('api/resident-verification/<str:doc_id>/verify/', views.resident_verification_verify, name='resident_verification_verify'),
    path('api/resident-verification/<str:doc_id>/reject/', views.resident_verification_reject, name='resident_verification_reject'),
    path('api/sync-predictions/', views.sync_predictions_to_firebase, name='sync_predictions'),
    path('api/routes/recompute_all/', views.recompute_all_routes, name='recompute_all_routes'),
    path('api/check-road-reports/', views.check_road_reports, name='check_road_reports'),
    path('api/approve-reroute/', views.approve_reroute, name='approve_reroute'),
    path('api/route-suggestion/generate/', views.generate_route_suggestion, name='generate_route_suggestion'),

    # You can likely remove or comment out this line as you're now using custom views
    
]

# This is a critical step for serving static and media files during development.
# It should only be used in a development environment.
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
