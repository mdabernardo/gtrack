from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.conf import settings
import json


def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username') or request.POST.get('email')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('/admin/')  # Redirect to admin dashboard
        else:
            firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
            return render(request, 'login.html', {
                'error': 'Invalid credentials',
                'firebase_config_json': firebase_config_json,
            })
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    return render(request, 'login.html', {
        'firebase_config_json': firebase_config_json,
    })
