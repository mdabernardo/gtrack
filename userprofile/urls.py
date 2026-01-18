# gtrack/urls.py

from django.contrib import admin
from django.urls import path, include
from gtrack import views

urlpatterns = [
    # Admin site
    path('admin/', admin.site.urls),

    # Your custom views
    path('profile/', views.profile_view, name='profile'),
    path('login/', views.login_view, name='login'),
    path('signup/', views.signup_view, name='signup'),

    # Include Django's built-in authentication URLs.
    # This provides the 'password_change' URL name.
    path('accounts/', include('django.contrib.auth.urls')),
]