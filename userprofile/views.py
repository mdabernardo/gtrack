from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import login as django_login
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.decorators import login_required
from firebase_admin import auth, exceptions
from .forms import UserProfileForm
from .models import UserProfile, Resident # Ensure you have a Resident model

#
# The new signup_view function
#
def signup_view(request):
    """
    Handles user registration by creating a user in Firebase and Django,
    then logging them in.
    """
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        address = request.POST.get('address')
        phone_number = request.POST.get('phone_number')

        try:
            # Create the user in Firebase Authentication
            user_record = auth.create_user(
                email=email,
                password=password
            )
            
            # Create the corresponding user in Django's database
            django_user = Resident.objects.create(
                user_id=user_record.uid,
                address=address,
                phone_number=phone_number,
                is_verified=False
            )
            
            # Log the user into Django using the correct backend
            django_login(request, django_user, backend=ModelBackend())
            
            messages.success(request, 'Account created successfully!')
            return redirect('dashboard')  # Redirect to your desired page

        except exceptions.FirebaseError as e:
            if e.code == 'email-already-exists':
                messages.error(request, 'This email is already in use.')
            else:
                messages.error(request, f'Firebase error: {e.code}')
            return redirect('signup')
        except Exception as e:
            messages.error(request, f'An unexpected error occurred: {e}')
            return redirect('signup')

    return render(request, 'signup.html')
#
# Your existing profile_view function
#
@login_required
def profile_view(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            return redirect('profile')
    else:
        form = UserProfileForm(instance=profile)

    return render(request, 'profile.html', {'form': form})