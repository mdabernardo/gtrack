# gtrack/views.py

from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login as django_login, logout as django_logout
from django.conf import settings 
import json
from .models import Resident
from userprofile.models import UserProfile
from userprofile.forms import UserEditForm, UserProfileForm
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
import firebase_admin
from firebase_admin import auth, exceptions # Corrected import
try:
    from firebase_admin import firestore
except Exception:
    firestore = None
# Try to initialize Firebase Admin SDK via settings-based helper on import
try:
    from .firebase_setup import *  # initialization happens if not already
except Exception:
    pass
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, action
from rest_framework.response import Response
from django.utils import timezone
from datetime import datetime, timedelta
from django.core.mail import send_mail
from django.core.cache import cache

from .models import (
    Location, Route, RoutePoint, CollectionSchedule,
    CollectionHistory, GarbageCollector, Resident,
    AIRoutePrediction, Notification
)
from .serializers import (
    LocationSerializer, RouteSerializer, RoutePointSerializer,
    CollectionScheduleSerializer, CollectionHistorySerializer,
    GarbageCollectorSerializer, ResidentSerializer,
    AIRoutePredictionSerializer, NotificationSerializer
)
from .ai_predictor import GarbageRoutePredictor
from .firebase_sync import sync_prediction_to_firestore, sync_scheduling_assistance_to_firestore

# Safe import for optional Firebase notification manager
try:
    from .firebase import firebase_manager  # type: ignore
except Exception:
    firebase_manager = None

# Initialize the AI predictor
ai_predictor = GarbageRoutePredictor()

# Redirect root to a sensible landing page
def root_redirect(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return redirect('login')

# API ViewSets merged from g-track-main
class LocationViewSet(viewsets.ModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer

class RouteViewSet(viewsets.ModelViewSet):
    queryset = Route.objects.all()
    serializer_class = RouteSerializer

    @action(detail=True, methods=['get'])
    def points(self, request, pk=None):
        route = self.get_object()
        points = RoutePoint.objects.filter(route=route).order_by('order')
        serializer = RoutePointSerializer(points, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def schedules(self, request, pk=None):
        route = self.get_object()
        schedules = CollectionSchedule.objects.filter(route=route)
        serializer = CollectionScheduleSerializer(schedules, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        route = self.get_object()
        history = CollectionHistory.objects.filter(route=route).order_by('-date')
        serializer = CollectionHistorySerializer(history, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def predictions(self, request, pk=None):
        route = self.get_object()
        days = int(request.query_params.get('days', 7))
        start_date = timezone.now().date()
        end_date = start_date + timedelta(days=days)
        existing = AIRoutePrediction.objects.filter(
            route=route, date__gte=start_date, date__lt=end_date
        ).order_by('date')
        if existing.count() < days:
            for i in range(days):
                target_date = start_date + timedelta(days=i)
                if not existing.filter(date=target_date).exists():
                    pred = ai_predictor.predict_route_schedule(route.id, target_date)
                    if pred:
                        ai_predictor.save_prediction(route.id, target_date, pred)
            existing = AIRoutePrediction.objects.filter(
                route=route, date__gte=start_date, date__lt=end_date
            ).order_by('date')
        serializer = AIRoutePredictionSerializer(existing, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def eta(self, request, pk=None):
        route = self.get_object()
        date_str = request.query_params.get('date')
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.now().date()
        except Exception:
            target_date = timezone.now().date()
        pred = ai_predictor.predict_route_schedule(route.id, target_date)
        if not pred:
            return Response({'error': 'no_prediction'}, status=status.HTTP_404_NOT_FOUND)
        start = pred['predicted_start_time']
        points = RoutePoint.objects.filter(route=route).order_by('order').select_related('location')
        etas = []
        cum = 0
        for p in points:
            eta_hour = (start.hour*60 + start.minute + cum) // 60
            eta_min = (start.hour*60 + start.minute + cum) % 60
            eta_str = f"{int(eta_hour)%24:02d}:{int(eta_min):02d}"
            etas.append({
                'point_id': p.id,
                'location_name': p.location.name,
                'order': p.order,
                'eta': eta_str,
            })
            cum += int(p.estimated_time_minutes or 5)
        return Response({
            'route_id': route.id,
            'route_name': route.name,
            'date': target_date,
            'predicted_start': pred['predicted_start_time'],
            'predicted_end': pred['predicted_end_time'],
            'etas': etas,
        })

    @action(detail=True, methods=['get'])
    def optimize(self, request, pk=None):
        route = self.get_object()
        try:
            result = ai_predictor.optimize_route_by_garbage_level(route.id)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(result)

    @action(detail=True, methods=['post'])
    def recompute(self, request, pk=None):
        route = self.get_object()
        try:
            days = int(request.data.get('days', request.query_params.get('days', 7)))
        except Exception:
            days = 7
        today = timezone.localdate()
        generated = []
        for i in range(days):
            target_date = today + timedelta(days=i)
            pred = ai_predictor.predict_route_schedule(route.id, target_date)
            if pred:
                ai_predictor.save_prediction(route.id, target_date, pred)
                generated.append(target_date.strftime('%Y-%m-%d'))
        return Response({'status': 'ok', 'generated': generated})

    @action(detail=True, methods=['post'])
    def reoptimize(self, request, pk=None):
        route = self.get_object()
        try:
            result = ai_predictor.optimize_route_by_garbage_level(route.id)
            return Response({'status': 'ok', 'optimization': result})
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def scheduling_assistance(self, request, pk=None):
        route = self.get_object()
        date_str = request.data.get('date') or request.query_params.get('date')
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.localdate()
        except Exception:
            target_date = timezone.localdate()
        pred = ai_predictor.predict_route_schedule(route.id, target_date)
        if not pred:
            return Response({'error': 'no_prediction'}, status=status.HTTP_404_NOT_FOUND)
        start_str = pred['predicted_start_time'].strftime('%H:%M')
        end_str = pred['predicted_end_time'].strftime('%H:%M')
        confidence = float(pred.get('confidence_score', 0.0))
        factors = pred.get('factors', {})
        mirrored = False
        try:
            mirrored = bool(
                sync_scheduling_assistance_to_firestore(
                    route_id=route.id,
                    route_name=route.name,
                    assistance_date=target_date,
                    predicted_start_time=start_str,
                    predicted_end_time=end_str,
                    confidence_score=confidence,
                    factors=factors,
                )
            )
        except Exception:
            mirrored = False
        return Response({
            'status': 'mirrored' if mirrored else 'computed_only',
            'route_id': route.id,
            'route_name': route.name,
            'date': target_date.strftime('%Y-%m-%d'),
            'predicted_start': start_str,
            'predicted_end': end_str,
            'confidence': confidence,
            'factors': factors,
        })

class RoutePointViewSet(viewsets.ModelViewSet):
    queryset = RoutePoint.objects.all()
    serializer_class = RoutePointSerializer

class CollectionScheduleViewSet(viewsets.ModelViewSet):
    queryset = CollectionSchedule.objects.all()
    serializer_class = CollectionScheduleSerializer

    def get_queryset(self):
        queryset = CollectionSchedule.objects.all()
        day = self.request.query_params.get('day', None)
        if day is not None:
            try:
                day_int = int(day)
                queryset = queryset.filter(day_of_week=day_int)
            except ValueError:
                pass
        return queryset

class CollectionHistoryViewSet(viewsets.ModelViewSet):
    queryset = CollectionHistory.objects.all()
    serializer_class = CollectionHistorySerializer

    def get_queryset(self):
        queryset = CollectionHistory.objects.all()
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__gte=start)
            except ValueError:
                pass
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__lte=end)
            except ValueError:
                pass
        return queryset.order_by('-date')

class GarbageCollectorViewSet(viewsets.ModelViewSet):
    queryset = GarbageCollector.objects.all()
    serializer_class = GarbageCollectorSerializer

    @action(detail=True, methods=['get'])
    def routes(self, request, pk=None):
        collector = self.get_object()
        routes = collector.assigned_routes.all()
        serializer = RouteSerializer(routes, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def today_schedule(self, request, pk=None):
        collector = self.get_object()
        today = timezone.now().date()
        day_of_week = today.weekday()
        routes = collector.assigned_routes.all()
        route_ids = [r.id for r in routes]
        schedules = CollectionSchedule.objects.filter(
            route__in=route_ids, day_of_week=day_of_week, is_active=True
        )
        predictions = AIRoutePrediction.objects.filter(route__in=route_ids, date=today)
        result = []
        for schedule in schedules:
            data = {
                'route_id': schedule.route.id,
                'route_name': schedule.route.name,
                'scheduled_start': schedule.start_time,
                'scheduled_end': schedule.end_time,
                'predicted_start': None,
                'predicted_end': None,
                'confidence': None,
            }
            prediction = predictions.filter(route=schedule.route).first()
            if prediction:
                data.update({
                    'predicted_start': prediction.predicted_start_time,
                    'predicted_end': prediction.predicted_end_time,
                    'confidence': prediction.confidence_score,
                })
            result.append(data)
        return Response(result)

class ResidentViewSet(viewsets.ModelViewSet):
    queryset = Resident.objects.all()
    serializer_class = ResidentSerializer

    def get_queryset(self):
        qs = Resident.objects.all()
        # Optional filter by verification status
        is_verified_param = self.request.query_params.get('is_verified')
        pending_param = self.request.query_params.get('pending')
        if is_verified_param is not None:
            val = is_verified_param.lower() in ['1', 'true', 'yes']
            qs = qs.filter(is_verified=val)
        elif pending_param is not None:
            val = pending_param.lower() in ['1', 'true', 'yes']
            if val:
                qs = qs.filter(is_verified=False)
        return qs

    @action(detail=True, methods=['post'])
    def verify(self, request, pk=None):
        resident = self.get_object()
        resident.is_verified = True
        resident.save()
        return Response({'status': 'verified'})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        # Basic reject: keep as not verified; could extend with reason field
        resident = self.get_object()
        resident.is_verified = False
        resident.save()
        return Response({'status': 'rejected'})

    @action(detail=True, methods=['get'])
    def upcoming_collections(self, request, pk=None):
        resident = self.get_object()
        if not resident.location:
            return Response({"error": "Resident location not set"}, status=status.HTTP_400_BAD_REQUEST)
        today = timezone.now().date()
        days_ahead = int(request.query_params.get('days', 7))
        predictions = []
        for route in Route.objects.all():
            for i in range(days_ahead):
                target_date = today + timedelta(days=i)
                pred = AIRoutePrediction.objects.filter(route=route, date=target_date).first()
                if not pred:
                    result = ai_predictor.predict_route_schedule(route.id, target_date)
                    if result:
                        ai_predictor.save_prediction(route.id, target_date, result)
                        pred = AIRoutePrediction.objects.filter(route=route, date=target_date).first()
                if pred:
                    predictions.append(AIRoutePredictionSerializer(pred).data)
        return Response(predictions)

class AIRoutePredictionViewSet(viewsets.ModelViewSet):
    queryset = AIRoutePrediction.objects.all()
    serializer_class = AIRoutePredictionSerializer

    def get_queryset(self):
        qs = AIRoutePrediction.objects.all()
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d').date()
                qs = qs.filter(date__gte=start)
            except ValueError:
                pass
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d').date()
                qs = qs.filter(date__lte=end)
            except ValueError:
                pass
        return qs.order_by('date')

class NotificationViewSet(viewsets.ModelViewSet):
    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer

    def get_queryset(self):
        qs = Notification.objects.all()
        user_id = self.request.query_params.get('user_id')
        if user_id:
            qs = qs.filter(user_id=user_id)
        return qs

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.save()
        return Response({'status': 'marked as read'})

@api_view(['GET'])
def get_today_predictions(request):
    today = timezone.now().date()
    data = AIRoutePrediction.objects.filter(date=today)
    return Response(AIRoutePredictionSerializer(data, many=True).data)

@api_view(['GET'])
def get_week_predictions(request):
    start_date = timezone.now().date()
    end_date = start_date + timedelta(days=7)
    data = AIRoutePrediction.objects.filter(date__gte=start_date, date__lt=end_date)
    return Response(AIRoutePredictionSerializer(data, many=True).data)

@api_view(['POST'])
def train_ai_model(request):
    success = ai_predictor.train_model()
    return Response({'status': 'trained' if success else 'no_data'})

@api_view(['POST'])
def generate_daily_notifications(request):
    today = timezone.now().date()
    created = 0
    for resident in Resident.objects.filter(notification_enabled=True):
        Notification.objects.create(
            user=resident.user,
            type='schedule',
            title='Today\'s Collection Reminder',
            message='Garbage collection is scheduled today.',
        )
        created += 1
        # Optional push via firebase_manager if available
        if firebase_manager:
            try:
                # Expecting firebase_manager to have a method send_message(token, title, body)
                if resident.fcm_token:
                    firebase_manager.send_message(resident.fcm_token, 'Garbage Collection', 'Scheduled today')
            except Exception:
                pass
    return Response({'status': 'notifications generated', 'created': created})

def login_view(request):
    """
    Handles user login using Firebase Authentication and syncs the session with Django.
    """
    # If already authenticated, go straight to dashboard
    if request.method == 'GET' and request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        if not email or not password:
            messages.error(request, "Email and password are required.")
            return redirect('login')

        # Fixed admin override: allow direct login for configured admin Gmail(s)
        try:
            fixed_emails = getattr(settings, 'ADMIN_FIXED_EMAILS', [])
            if not isinstance(fixed_emails, (list, tuple, set)):
                fixed_emails = [getattr(settings, 'ADMIN_FIXED_EMAIL', '')]
            allowed_emails = {e.strip().lower() for e in fixed_emails if e}
            if (
                getattr(settings, 'ENABLE_ADMIN_FIXED_LOGIN', False)
                and email.strip().lower() in allowed_emails
                and password == getattr(settings, 'ADMIN_FIXED_PASSWORD', '')
            ):
                admin_email = email.strip().lower()
                try:
                    django_user = User.objects.get(email__iexact=admin_email)
                except User.DoesNotExist:
                    # Username derived from email local-part for clarity; ensure uniqueness
                    local = (admin_email.split('@')[0] or 'gtrack_admin')[:30]
                    base = f"admin_{local}"
                    candidate = base
                    i = 1
                    while User.objects.filter(username__iexact=candidate).exists():
                        candidate = f"{base}{i}"
                        i += 1
                    django_user = User.objects.create_user(
                        username=candidate,
                        email=admin_email,
                        password=None,
                    )
                # Ensure admin privileges and prevent password login
                changed = False
                if not django_user.is_staff:
                    django_user.is_staff = True
                    changed = True
                if not django_user.is_superuser:
                    django_user.is_superuser = True
                    changed = True
                django_user.set_unusable_password()
                # Save changes (including password field)
                if changed:
                    django_user.save(update_fields=['is_staff', 'is_superuser'])
                else:
                    django_user.save()

                django_login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')
                messages.success(request, "Logged in as admin.")
                # Respect 'next' parameter when present
                next_url = request.POST.get('next') or request.GET.get('next')
                try:
                    from django.utils.http import url_has_allowed_host_and_scheme
                    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                        return redirect(next_url)
                except Exception:
                    pass
                return redirect('dashboard')
        except Exception:
            # Fall through to Firebase if override fails
            pass

        # Firebase-only authentication: sign in via Identity Toolkit REST API
        firebase_api_key = settings.FIREBASE_CLIENT_CONFIG.get('apiKey')
        if not firebase_api_key:
            messages.error(request, "Firebase API key is not configured.")
            return redirect('login')

        try:
            import requests
            resp = requests.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={firebase_api_key}",
                json={
                    "email": email,
                    "password": password,
                    "returnSecureToken": True,
                },
                timeout=15,
            )
            data = resp.json()
            if resp.status_code != 200:
                # Common error messages from Firebase
                err = data.get('error', {}).get('message', 'LOGIN_FAILED')
                messages.error(request, f"Firebase login failed: {err}")
                return redirect('login')

            id_token = data.get('idToken')
            uid = data.get('localId')
            if not id_token or not uid:
                messages.error(request, "Invalid Firebase response.")
                return redirect('login')

            # Verify ID token using Admin SDK when available; otherwise trust REST response (dev fallback)
            if getattr(firebase_admin, '_apps', None):
                decoded = auth.verify_id_token(id_token)
                uid = decoded.get('uid') or uid
                email_from_token = decoded.get('email') or email
            else:
                email_from_token = email

            # Map to Django user by Firebase UID (creates if missing)
            django_user, _ = User.objects.get_or_create(
                username=uid,
                defaults={"email": email_from_token or ""}
            )
            django_login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, f"Login successful!")
            # Respect 'next' parameter when present
            next_url = request.POST.get('next') or request.GET.get('next')
            try:
                from django.utils.http import url_has_allowed_host_and_scheme
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                    return redirect(next_url)
            except Exception:
                pass
            return redirect('dashboard')
        except exceptions.FirebaseError as e:
            messages.error(request, f"Firebase error: {getattr(e, 'code', str(e))}")
            return redirect('login')
        except Exception as e:
            messages.error(request, f"Unexpected error during login: {str(e)}")
            return redirect('login')

    context = {
        'firebase_config_json': json.dumps(settings.FIREBASE_CLIENT_CONFIG),
        'admin_login_enabled': getattr(settings, 'ENABLE_ADMIN_FIXED_LOGIN', False),
        'admin_allowed_emails': getattr(settings, 'ADMIN_FIXED_EMAILS', ()),
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
            firebase_api_key = settings.FIREBASE_CLIENT_CONFIG.get('apiKey')
            if getattr(firebase_admin, '_apps', None):
                # 1. Create a new user via Admin SDK
                user_record = auth.create_user(
                    email=email,
                    password=password
                )
                uid = user_record.uid
                email_val = user_record.email or email
            else:
                # 1. Create user via Firebase REST API (signUp)
                import requests
                resp = requests.post(
                    f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={firebase_api_key}",
                    json={"email": email, "password": password, "returnSecureToken": True},
                    timeout=15,
                )
                data = resp.json()
                if resp.status_code != 200:
                    err = data.get('error', {}).get('message', 'SIGNUP_FAILED')
                    messages.error(request, f"Firebase signup failed: {err}")
                    return redirect('signup')
                uid = data.get('localId')
                email_val = data.get('email') or email

            # 2. Sync to Django user keyed by Firebase UID
            django_user, created = User.objects.get_or_create(
                username=uid,
                defaults={'email': email_val}
            )
            if created:
                django_user.set_password(password)
                django_user.save()

            # 3. Create Resident profile if missing
            try:
                Resident.objects.get(user=django_user)
            except Resident.DoesNotExist:
                Resident.objects.create(
                    user=django_user,
                    address=address,
                    phone_number=phone_number
                )

            # 4. Log in
            django_login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, "Account created successfully! You are now logged in.")
            return redirect('dashboard')

        except exceptions.FirebaseError as e:
            code = getattr(e, 'code', 'firebase_error')
            messages.error(request, f"An authentication error occurred: {code}")
            return redirect('signup')
        except Exception as e:
            messages.error(request, f"Unexpected signup error: {str(e)}")
            return redirect('signup')
    
    context = {
        'firebase_client_config': settings.FIREBASE_CLIENT_CONFIG
    }
    return render(request, 'signup.html', context)

def crud_management(request):
    """
    Renders the CRUD management page.
    """
    # You will replace 'crud_management_page.html' with the actual template
    # for your CRUD interface when you build it.
    return render(request, 'crud_management.html', {})


def logout_view(request):
    """
    Logs out the user and redirects them to the login page.
    """
    if request.user.is_authenticated:
        django_logout(request)
        messages.info(request, "You have been logged out.")
    return redirect('login')


@api_view(['POST'])
def firebase_login(request):
    """Verify a Firebase ID token, allow only admin Gmail accounts, create/find the Django user, and log them in."""
    try:
        data = request.data or {}
        id_token = data.get('idToken')
        if not id_token:
            return Response({'status': 'error', 'message': 'Missing idToken'}, status=400)
        decoded = auth.verify_id_token(id_token)
        uid = decoded.get('uid')
        email = decoded.get('email')
        if not uid:
            return Response({'status': 'error', 'message': 'Invalid token'}, status=400)
        # Restrict to configured admin emails only
        try:
            fixed_emails = getattr(settings, 'ADMIN_FIXED_EMAILS', ())
            allowed = {e.strip().lower() for e in fixed_emails if e}
        except Exception:
            allowed = set()
        if not email or email.strip().lower() not in allowed:
            return Response({'status': 'error', 'message': 'Email not allowed'}, status=403)
        # Find or create the Django user keyed by Firebase UID
        user, _ = User.objects.get_or_create(username=uid, defaults={'email': email or ''})
        django_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        return Response({'status': 'ok'})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=400)

    context = {
        'firebase_config_json': json.dumps(settings.FIREBASE_CLIENT_CONFIG)
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

def crud_management(request):
    """
    Renders the CRUD management page.
    """
    # You will replace 'crud_management_page.html' with the actual template
    # for your CRUD interface when you build it.
    return render(request, 'crud_management.html', {})


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
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    google_maps_api_key = settings.FIREBASE_CLIENT_CONFIG.get('apiKey', '')
    context = {
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
        'google_maps_api_key': google_maps_api_key,
    }
    return render(request, 'track_trucks.html', context)

def help_view(request):
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    google_maps_api_key = settings.FIREBASE_CLIENT_CONFIG.get('apiKey', '')
    context = {
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
        'google_maps_api_key': google_maps_api_key,
    }
    return render(request, 'help.html', context)

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
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    context = {
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
    }
    return render(request, 'notification.html', context)

@login_required
def warning_view(request):
    return render(request, 'warning.html')

@login_required
def profile_view(request):
    """
    Edit-on-load profile page allowing user to update details and photo.
    Persists changes to User, Resident, and UserProfile (photo).
    """
    user = request.user
    # Ensure related instances exist
    resident, _ = Resident.objects.get_or_create(user=user)
    user_profile, _ = UserProfile.objects.get_or_create(user=user)

    if request.method == 'POST':
        user_form = UserEditForm(request.POST, instance=user)
        profile_form = UserProfileForm(request.POST, request.FILES, instance=user_profile)

        # Map additional resident fields from POST
        resident_phone = request.POST.get('phone_number', '')
        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            # Persist resident fields
            resident.phone_number = resident_phone
            resident.save()
            messages.success(request, 'Profile updated successfully.')
            return redirect('profile')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        user_form = UserEditForm(instance=user)
        profile_form = UserProfileForm(instance=user_profile)

    context = {
        'user_form': user_form,
        'profile_form': profile_form,
        'resident': resident,
        'user_profile': user_profile,
    }
    return render(request, 'profile.html', context)

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

def driver_verification_view(request):
    """Create driver Firebase Auth accounts and store metadata in Firestore."""

    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        address = request.POST.get('address', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        age = request.POST.get('age', '').strip()
        sex = request.POST.get('sex', '').strip()
        gmail_email = request.POST.get('gmail_email', '').strip()
        password = request.POST.get('password', '').strip()

        # Basic validation
        if not (full_name and address and contact_number and age and sex and gmail_email and password):
            messages.error(request, 'All fields are required.')
            return render(request, 'driver_verification.html', {})

        # Save uploaded ID to MEDIA_ROOT
        id_media_relpath = None
        try:
            file = request.FILES.get('valid_id')
            if file:
                import os
                from django.conf import settings
                from django.core.files.storage import default_storage
                from django.utils.text import slugify
                # Create a safe filename
                base_name = slugify(full_name or 'driver')
                ext = os.path.splitext(file.name)[1]
                filename = f"drivers_ids/{base_name}_{timezone.now().strftime('%Y%m%d%H%M%S')}{ext}"
                file_path = os.path.join(settings.MEDIA_ROOT, filename)
                # Ensure directory exists
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with default_storage.open(filename, 'wb+') as destination:
                    for chunk in file.chunks():
                        destination.write(chunk)
                id_media_relpath = filename
        except Exception as e:
            messages.warning(request, f"Failed to store ID file: {e}")

        # Ensure Firebase Admin SDK is initialized
        import firebase_admin as _fb
        if not _fb._apps:
            # Attempt to initialize using settings/env as last resort
            try:
                from django.conf import settings as _settings
                from firebase_admin import credentials as _creds, initialize_app as _init
                import os as _os, json as _json
                cred = None
                admin_json = _os.getenv('FIREBASE_ADMIN_JSON')
                if admin_json:
                    try:
                        cred = _creds.Certificate(_json.loads(admin_json))
                    except Exception:
                        cred = None
                if not cred and getattr(_settings, 'FIREBASE_CREDENTIALS_PATH', None):
                    path = _settings.FIREBASE_CREDENTIALS_PATH
                    if path and _os.path.exists(path):
                        cred = _creds.Certificate(path)
                ga_path = _os.getenv('GOOGLE_APPLICATION_CREDENTIALS') or _os.getenv('GOOGLE_APPLICATIONS_CREDENTIALS')
                if not cred and ga_path and _os.path.exists(ga_path):
                    cred = _creds.Certificate(ga_path)
                if cred:
                    _init(cred)
                else:
                    raise Exception('No credentials found')
            except Exception:
                messages.error(request, 'Firebase Admin SDK is not initialized. Set FIREBASE_CREDENTIALS_PATH or GOOGLE_APPLICATION_CREDENTIALS.')
                return render(request, 'driver_verification.html', {})

        # Create or update Firebase Auth user
        try:
            user_record = None
            try:
                user_record = auth.get_user_by_email(gmail_email)
            except exceptions.NotFoundError:
                user_record = None
            except Exception:
                user_record = None

            if user_record is None:
                user_record = auth.create_user(email=gmail_email, password=password, display_name=full_name)
            else:
                # Update password to the provided one (admin action)
                user_record = auth.update_user(user_record.uid, password=password, display_name=full_name)

            uid = user_record.uid

            # Write collector profile to Firestore (moved from 'drivers' to 'collectors')
            try:
                if firestore:
                    db = firestore.client()
                    db.collection('collectors').document(uid).set({
                        'uid': uid,
                        'fullName': full_name,
                        'address': address,
                        'contactNumber': contact_number,
                        'age': int(age) if str(age).isdigit() else age,
                        'sex': sex,
                        'gmail': gmail_email,
                        'idMediaPath': id_media_relpath,
                        'createdAt': firestore.SERVER_TIMESTAMP,
                        'status': 'verified',
                        # Emergency-only: store a temporary plaintext password with expiry
                        'emergencyTempPassword': password,
                        'emergencyTempPasswordSetAt': firestore.SERVER_TIMESTAMP,
                        'emergencyTempPasswordExpiresAt': (timezone.now() + timedelta(hours=24)),
                    }, merge=True)
            except Exception as e:
                messages.warning(request, f"Collector profile saved locally but failed to write to Firestore: {e}")

            messages.success(request, f"Driver account created/updated for {gmail_email}. Temporary password set.")
            return redirect('driver_verification')
        except Exception as e:
            messages.error(request, f"Failed to create driver account: {e}")
            return render(request, 'driver_verification.html', {})

    return render(request, 'driver_verification.html', {})


@api_view(['GET'])
def firebase_test_collectors_one(request):
    """Simple health check: read Firestore doc collectors/1 to verify connection."""
    try:
        import firebase_admin as _fb
        # Ensure Admin SDK initialized
        if not _fb._apps:
            return Response({'status': 'error', 'message': 'Firebase Admin SDK not initialized'}, status=500)
        try:
            from firebase_admin import firestore as _fs
        except Exception:
            return Response({'status': 'error', 'message': 'Firestore module unavailable'}, status=500)

        db = _fs.client()
        doc_ref = db.collection('collectors').document('1')
        snap = doc_ref.get()
        if snap.exists:
            return Response({'status': 'ok', 'exists': True, 'data': snap.to_dict()})
        else:
            return Response({'status': 'ok', 'exists': False, 'data': None})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)

@api_view(['GET'])
def firebase_collectors_list(request):
    """List documents from Firestore 'collectors' collection and return JSON."""
    try:
        import firebase_admin as _fb
        if not _fb._apps:
            return Response({'status': 'error', 'message': 'Firebase Admin SDK not initialized'}, status=500)
        try:
            from firebase_admin import firestore as _fs
        except Exception:
            return Response({'status': 'error', 'message': 'Firestore module unavailable'}, status=500)

        db = _fs.client()
        limit_param = request.GET.get('limit')
        try:
            limit = int(limit_param) if limit_param else None
        except Exception:
            limit = None

        query = db.collection('collectors')
        docs = query.limit(limit).get() if limit else query.get()
        items = []
        for d in docs:
            data = d.to_dict() or {}
            data['id'] = d.id
            items.append(data)
        return Response({'status': 'ok', 'count': len(items), 'items': items})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)

@api_view(['GET'])
def firebase_collectors_get(request, doc_id: str):
    """Get a single collector document by ID from Firestore."""
    try:
        import firebase_admin as _fb
        if not _fb._apps:
            return Response({'status': 'error', 'message': 'Firebase Admin SDK not initialized'}, status=500)
        try:
            from firebase_admin import firestore as _fs
        except Exception:
            return Response({'status': 'error', 'message': 'Firestore module unavailable'}, status=500)

        db = _fs.client()
        ref = db.collection('collectors').document(str(doc_id))
        snap = ref.get()
        if not snap.exists:
            return Response({'status': 'ok', 'exists': False, 'data': None})
        data = snap.to_dict() or {}
        data['id'] = snap.id
        return Response({'status': 'ok', 'exists': True, 'data': data})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)

@api_view(['GET'])
def firebase_dropoffs_list(request):
    """List documents from Firestore 'dropoffs' collection and return JSON."""
    try:
        import firebase_admin as _fb
        if not _fb._apps:
            return Response({'status': 'error', 'message': 'Firebase Admin SDK not initialized'}, status=500)
        try:
            from firebase_admin import firestore as _fs
        except Exception:
            return Response({'status': 'error', 'message': 'Firestore module unavailable'}, status=500)

        db = _fs.client()
        limit_param = request.GET.get('limit')
        try:
            limit = int(limit_param) if limit_param else None
        except Exception:
            limit = None

        query = db.collection('dropoffs')
        docs = query.limit(limit).get() if limit else query.get()
        items = []
        for d in docs:
            data = d.to_dict() or {}
            data['id'] = d.id
            items.append(data)
        return Response({'status': 'ok', 'count': len(items), 'items': items})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)

@api_view(['GET'])
def firebase_dropoffs_get(request, doc_id: str):
    """Get a single drop-off document by ID from Firestore."""
    try:
        import firebase_admin as _fb
        if not _fb._apps:
            return Response({'status': 'error', 'message': 'Firebase Admin SDK not initialized'}, status=500)
        try:
            from firebase_admin import firestore as _fs
        except Exception:
            return Response({'status': 'error', 'message': 'Firestore module unavailable'}, status=500)

        db = _fs.client()
        ref = db.collection('dropoffs').document(str(doc_id))
        snap = ref.get()
        if not snap.exists:
            return Response({'status': 'ok', 'exists': False, 'data': None})
        data = snap.to_dict() or {}
        data['id'] = snap.id
        return Response({'status': 'ok', 'exists': True, 'data': data})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)

@api_view(['POST'])
def firebase_migrate_drivers_to_collectors(request):
    """Admin-only: Copy Firestore 'drivers' docs into 'collectors'. Optional delete.

    Query param `delete=true` will delete the source 'drivers' docs after copy.
    """
    try:
        # Basic admin guard
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated and user.is_staff):
            return Response({'status': 'error', 'message': 'Forbidden'}, status=403)

        import firebase_admin as _fb
        if not _fb._apps:
            return Response({'status': 'error', 'message': 'Firebase Admin SDK not initialized'}, status=500)
        try:
            from firebase_admin import firestore as _fs
        except Exception:
            return Response({'status': 'error', 'message': 'Firestore module unavailable'}, status=500)

        delete_after = str(request.GET.get('delete', 'false')).lower() in ('1', 'true', 'yes')

        db = _fs.client()
        src = db.collection('drivers').get()
        migrated = 0
        errors = 0
        already = 0
        for s in src:
            try:
                data = s.to_dict() or {}
                dest_ref = db.collection('collectors').document(s.id)
                dest_snap = dest_ref.get()
                if dest_snap.exists:
                    already += 1
                # Merge data and annotate migration time
                dest_ref.set({**data, 'migratedAt': _fs.SERVER_TIMESTAMP}, merge=True)
                migrated += 1
                if delete_after:
                    db.collection('drivers').document(s.id).delete()
            except Exception:
                errors += 1

        return Response({
            'status': 'ok',
            'migrated': migrated,
            'alreadyPresent': already,
            'errors': errors,
            'deletedSource': bool(delete_after)
        })
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)

@api_view(['POST'])
def sync_predictions_to_firebase(request):
    """Backfill all existing AI predictions into Firestore."""
    synced = 0
    errors = 0
    for pred in AIRoutePrediction.objects.all():
        try:
            route = pred.route
            start_str = pred.predicted_start_time.strftime('%H:%M')
            end_str = pred.predicted_end_time.strftime('%H:%M')
            ok = sync_prediction_to_firestore(
                route_id=route.id,
                route_name=route.name,
                prediction_date=pred.date,
                predicted_start_time=start_str,
                predicted_end_time=end_str,
                confidence_score=pred.confidence_score,
                factors=pred.factors,
            )
            if ok:
                synced += 1
            else:
                errors += 1
        except Exception:
            errors += 1
    return Response({'status': 'done', 'synced': synced, 'errors': errors})

@api_view(['POST'])
def generate_verification_notifications(request):
    """Create admin notifications for residents pending verification."""
    unverified = Resident.objects.filter(is_verified=False)
    admin_users = User.objects.filter(is_staff=True)
    created = 0
    for admin in admin_users:
        count = unverified.count()
        if count == 0:
            continue
        title = 'Pending Resident Verifications'
        message = f'There are {count} residents awaiting verification.'
        Notification.objects.create(
            user=admin,
            type='general',
            title=title,
            message=message,
        )
        created += 1
    return Response({'status': 'ok', 'created': created, 'pending_count': unverified.count()})

@api_view(['POST'])
def recompute_all_routes(request):
    try:
        days = int(request.data.get('days', request.query_params.get('days', 7)))
    except Exception:
        days = 7
    today = timezone.localdate()
    summary = []
    for route in Route.objects.all():
        generated = []
        for i in range(days):
            target_date = today + timedelta(days=i)
            pred = ai_predictor.predict_route_schedule(route.id, target_date)
            if pred:
                ai_predictor.save_prediction(route.id, target_date, pred)
                generated.append(target_date.strftime('%Y-%m-%d'))
        summary.append({
            'route_id': route.id,
            'route_name': route.name,
            'generated': generated,
        })
    return Response({'status': 'ok', 'routes': summary})

# ---------------------------------------------
# Password reset via email code (OTP) endpoints
# ---------------------------------------------
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
import random

@csrf_exempt
@api_view(['POST'])
def password_reset_send_code(request):
    """Generate a 6-digit code, cache it for 15 minutes, and email it."""
    try:
        email = (request.data.get('email') or request.POST.get('email') or '').strip()
        if not email:
            return Response({'status': 'error', 'message': 'Missing email'}, status=400)
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Avoid disclosure whether email exists; still respond success-like
            return Response({'status': 'ok', 'message': 'If the email exists, a code was sent.'})

        code = str(random.randint(100000, 999999))
        cache_key = f"pwd_reset:{email}"
        cache.set(cache_key, code, timeout=15 * 60)

        subject = 'Your Password Reset Code'
        message = (
            f"Hello,\n\n"
            f"Your password reset code is: {code}\n"
            f"This code expires in 15 minutes.\n\n"
            f"If you did not request this, you can ignore this email."
        )
        try:
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
        except Exception as e:
            return Response({'status': 'error', 'message': f'Email send failed: {str(e)}'}, status=500)

        return Response({'status': 'ok'})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=400)

@csrf_exempt
@api_view(['POST'])
def password_reset_verify_code(request):
    """Verify the code and set a new password for the user."""
    try:
        data = request.data or {}
        email = (data.get('email') or request.POST.get('email') or '').strip()
        code = (data.get('code') or request.POST.get('code') or '').strip()
        new_password = (data.get('new_password') or request.POST.get('new_password') or '').strip()

        if not email or not code or not new_password:
            return Response({'status': 'error', 'message': 'Missing email, code, or new_password'}, status=400)

        cache_key = f"pwd_reset:{email}"
        cached_code = cache.get(cache_key)
        if not cached_code or cached_code != code:
            return Response({'status': 'error', 'message': 'Invalid or expired code'}, status=400)

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({'status': 'error', 'message': 'User does not exist'}, status=404)

        user.set_password(new_password)
        user.save()
        cache.delete(cache_key)

        return Response({'status': 'ok'})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=400)
