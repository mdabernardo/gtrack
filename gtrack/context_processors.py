import os
from django.conf import settings
from userprofile.models import UserProfile


def google_oauth(request):
    """
    Expose whether Google OAuth is configured so templates can guard the Gmail button.
    Checks env vars and settings SOCIALACCOUNT_PROVIDERS.
    """
    client_id = os.getenv('GOOGLE_CLIENT_ID', '')

    # Fallback to settings if provided via SOCIALACCOUNT_PROVIDERS
    try:
        client_id = client_id or settings.SOCIALACCOUNT_PROVIDERS['google']['APP'].get('client_id', '')
    except Exception:
        pass

    return {
        'google_oauth_configured': bool(client_id),
        'google_client_id': client_id,  # useful for debugging or conditional rendering
    }

def user_profile_context(request):
    """Expose the logged-in user's profile and avatar state globally."""
    user_profile = None
    has_custom_avatar = False
    try:
        if request.user.is_authenticated:
            user_profile = UserProfile.objects.filter(user=request.user).first()
            if user_profile and getattr(user_profile, 'photo', None):
                name = getattr(user_profile.photo, 'name', '')
                has_custom_avatar = name and ('default' not in name)
    except Exception:
        # Fail-safe: never break template rendering
        user_profile = None
        has_custom_avatar = False

    return {
        'user_profile': user_profile,
        'has_custom_avatar': has_custom_avatar,
    }