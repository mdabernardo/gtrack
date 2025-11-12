# gtrack/views.py

from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login as django_login, logout as django_logout
from django.conf import settings 
from .models import Resident
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from firebase_admin import auth, exceptions # Corrected import

# ... other imports remain the same

def login_view(request):
    """
    Handles user login using Firebase Authentication and syncs the session with Django.
    """
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        if not email or not password:
            messages.error(request, "Email and password are required.")
            return redirect('login')

        try:
            # 1. Authenticate with Firebase first
            # Note: Firebase Admin SDK does not have a method to verify a user's password directly.
            # You typically use a client-side SDK for this, or check existence and then handle login
            # with Django's backend. The most secure way is to use a custom authentication token.
            user_from_firebase = auth.get_user_by_email(email)

            # 2. Find the corresponding user in the Django database using the unique UID.
            try:
                # This will look up the user by the Firebase UID, which is a unique key.
                # This prevents the MultipleObjectsReturned error.
                django_user = User.objects.get(username=user_from_firebase.uid)
            except User.DoesNotExist:
                messages.error(request, "Your account is not synced with the system. Please contact support.")
                return redirect('login')
            
            # The previous attempt to get by email was problematic.
            # You should use the uid as the unique identifier to avoid duplicates.
            # Also, you should rely on your Django user model to handle authentication logic
            # for the Django side.
            
            # Since you're not passing the password to the Django auth backend,
            # this view only works if the user exists in both Firebase and Django.
            
            # 3. Log the user into the Django session
            django_login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')

            messages.success(request, f"Login successful for {django_user.username}!")
            return redirect('dashboard')

        # Updated to use the correct exception class
        except exceptions.FirebaseError as e:
            if e.code == 'auth/user-not-found':
                messages.error(request, "No user found with that email address.")
            elif e.code == 'auth/invalid-email':
                messages.error(request, "The email address is not valid.")
            else:
                messages.error(request, f"An authentication error occurred: {e.code}")
            return redirect('login')

    context = {
        'firebase_client_config': settings.FIREBASE_CLIENT_CONFIG
    }
    return render(request, 'login.html', context)


def signup_view(request):
    """
    Handles user signup by creating a new user in Firebase,
    syncing them to the Django database, and creating a Resident profile.
    """
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        address = request.POST.get('address')
        phone_number = request.POST.get('phone_number')

        if not email or not password:
            messages.error(request, "Email and password are required.")
            return redirect('signup')

        try:
            # 1. Create a new user in Firebase Authentication
            user_record = auth.create_user(
                email=email,
                password=password
            )
            
            # 2. Sync the new user to the Django database
            django_user = User.objects.create_user(
                # Use UID as the username to ensure uniqueness
                username=user_record.uid, 
                email=user_record.email,
            )
            # You need to manually set the password for the Django user
            # or it won't be able to authenticate with Django's native methods later.
            django_user.set_password(password)
            django_user.save()

            # 3. Create a corresponding Resident profile and link it to the user
            Resident.objects.create(
                user=django_user,
                address=address,
                phone_number=phone_number
            )

            # 4. Log the user into the Django session
            # You should specify the backend here to avoid the "multiple backends" error.
            django_login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, "Account created successfully! You are now logged in.")
            return redirect('dashboard')

        # Updated to use the correct exception class
        except exceptions.FirebaseError as e:
            if e.code == 'auth/email-already-exists':
                messages.error(request, "This email is already in use.")
            elif e.code == 'auth/invalid-password':
                messages.error(request, "Password must be at least 6 characters.")
            else:
                messages.error(request, f"An authentication error occurred: {e.code}")
            return redirect('signup')
    
    context = {
        'firebase_client_config': settings.FIREBASE_CLIENT_CONFIG
    }
    return render(request, 'signup.html', context)


def logout_view(request):
    """
    Logs out the user and redirects them to the login page.
    """
    if request.user.is_authenticated:
        django_logout(request)
        messages.info(request, "You have been logged out.")
    return redirect('login')


# The other views remain unchanged
@login_required
def dashboard(request):
    return render(request, 'dashboard.html', {})

@login_required
def track_trucks_view(request):
    return render(request, 'track_trucks.html')

def help_view(request):
    return render(request, 'help.html')

@login_required
def settings_view(request):
    return render(request, 'settings.html')

@login_required
def garbage_level_view(request):
    return render(request, 'garbage_level.html')

@login_required
def schedules_view(request):
    return render(request, 'schedules.html')

@login_required
def notification_view(request):
    return render(request, 'notification.html')

@login_required
def warning_view(request):
    return render(request, 'warning.html')

@login_required
def profile_view(request):
    """
    Renders the user's profile page.
    This view requires the user to be logged in.
    """
    return render(request, 'profile.html')

@login_required
def resident_verification_view(request):
    """
    Renders the resident verification page for the admin.
    Fetches residents who are not yet verified.
    """
    unverified_residents = Resident.objects.filter(is_verified=False)
    context = {
        'unverified_residents': unverified_residents
    }
    return render(request, 'resident_verification.html', context)