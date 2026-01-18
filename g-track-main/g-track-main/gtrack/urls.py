# project-level urls.py

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from gtrack.views import profile_view, signup_view

# Assuming your views are in gtrack/views.py
from gtrack import views  

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # URL patterns for your custom Firebase-Django authentication
    path('login/', views.login_view, name='login'),
    path('signup/', views.signup_view, name='signup'),
    path('logout/', views.logout_view, name='logout'),
    path('signup/', signup_view, name='signup'),
    path('profile/', profile_view, name='profile'),
    
    # URL patterns for your gtrack application
    path('dashboard/', views.dashboard, name='dashboard'),
    path('track-trucks/', views.track_trucks_view, name='track_trucks'),
    path('help/', views.help_view, name='help'),
    path('settings/', views.settings_view, name='settings'),
    path('garbage-level/', views.garbage_level_view, name='garbage_level'),
    path('schedules/', views.schedules_view, name='schedules'),
    path('notification/', views.notification_view, name='notification'),
    path('warning/', views.warning_view, name='warning'),
    path('profile/', views.profile_view, name='profile'),
    path('resident-verification/', views.resident_verification_view, name='resident_verification'),
    
    # You can likely remove or comment out this line as you're now using custom views
    # path('accounts/', include('allauth.urls')),
    
]

# This is a critical step for serving static and media files during development.
# It should only be used in a development environment.
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)