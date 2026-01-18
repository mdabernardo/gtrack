import os
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect
from django.urls import reverse

from allauth.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


def _allowed_admin_emails():
    # Read allowlist from env: ADMIN_GOOGLE_EMAILS="admin1@gmail.com, admin2@gmail.com"
    raw = os.getenv('ADMIN_GOOGLE_EMAILS', '')
    single = os.getenv('ADMIN_GOOGLE_EMAIL', '')
    emails = []
    if raw:
        emails.extend([e.strip().lower() for e in raw.split(',') if e.strip()])
    if single:
        emails.append(single.strip().lower())
    # Deduplicate
    return set(emails)


class AdminOnlySocialAdapter(DefaultSocialAccountAdapter):
    """
    Allow Google login ONLY for allowlisted admin Gmail addresses.
    If the allowlisted email logs in and no Django user exists, create one
    and mark it as staff/superuser. All other emails are blocked.
    """

    def is_open_for_signup(self, request, sociallogin):
        # Permit signup only for explicitly allowlisted admin emails
        email = None
        if sociallogin.user and getattr(sociallogin.user, 'email', None):
            email = sociallogin.user.email
        if not email and sociallogin.account:
            email = sociallogin.account.extra_data.get('email')
        allow = email and email.strip().lower() in _allowed_admin_emails()
        return bool(allow)

    def pre_social_login(self, request, sociallogin):
        # If already authenticated, allow linking or proceed.
        if request.user.is_authenticated:
            return

        # Extract email
        email = None
        if sociallogin.user and getattr(sociallogin.user, 'email', None):
            email = sociallogin.user.email
        if not email and sociallogin.account:
            email = sociallogin.account.extra_data.get('email')

        allowed = email and email.strip().lower() in _allowed_admin_emails()
        if not allowed:
            messages.error(request, "Google login is restricted to the admin Gmail address.")
            raise ImmediateHttpResponse(HttpResponseRedirect(reverse('account_login')))

        # Ensure a Django user exists for the allowed email
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=email)
            # Ensure admin flags
            changed = False
            if not user.is_staff:
                user.is_staff = True
                changed = True
            if not user.is_superuser:
                user.is_superuser = True
                changed = True
            if changed:
                user.save(update_fields=['is_staff', 'is_superuser'])
        except User.DoesNotExist:
            # Create a new admin user for the allowed email
            username_base = (email or 'admin').split('@')[0]
            # Ensure uniqueness by appending a suffix if needed
            base = username_base[:30] or 'admin'
            candidate = base
            i = 1
            while User.objects.filter(username__iexact=candidate).exists():
                candidate = f"{base}{i}"
                i += 1
            user = User.objects.create_user(
                username=candidate,
                email=email,
                password=None,
            )
            user.is_staff = True
            user.is_superuser = True
            user.save()

        # Attach the admin user and proceed
        sociallogin.user = user