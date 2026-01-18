from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    # The Django Admin site URL
    path('admin/', admin.site.urls),
    
    # The URL patterns for your 'gtrack' app
    path('', include('gtrack.mainurls')),

    # This is the crucial line for allauth.
    # It includes all of the authentication-related URLs (like 'account_logout', 'account_login', etc.)
    # under the '/accounts/' path.
    path('accounts/', include('allauth.urls')),
]
