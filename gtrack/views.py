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
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        if not email or not password:
            messages.error(request, "Email and password are required.")
            return redirect('login')

        # First, try standard Django authentication by username/email so superusers can log in.
        try:
            candidate = User.objects.get(username=email)
            if candidate.check_password(password):
                django_login(request, candidate, backend='django.contrib.auth.backends.ModelBackend')
                messages.success(request, f"Login successful for {candidate.username}!")
                return redirect('dashboard')
        except User.DoesNotExist:
            pass

        users_by_email = User.objects.filter(email=email)
        for u in users_by_email:
            if u.check_password(password):
                django_login(request, u, backend='django.contrib.auth.backends.ModelBackend')
                messages.success(request, f"Login successful for {u.username}!")
                return redirect('dashboard')

        # If no Django user matched, try Firebase lookup if Admin SDK is initialized; otherwise fall back to Django-only auth.
        try:
            if getattr(firebase_admin, '_apps', None):
                # 1) Firebase lookup by email (does not validate password server-side)
                user_from_firebase = auth.get_user_by_email(email)
                # 2) Map to Django user by Firebase UID
                try:
                    django_user = User.objects.get(username=user_from_firebase.uid)
                except User.DoesNotExist:
                    messages.error(request, "Your account is not synced with the system. Please contact support.")
                    return redirect('login')
                # 3) Log into Django
                django_login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')
                messages.success(request, f"Login successful for {django_user.username}!")
                return redirect('dashboard')
            else:
                # Fallback: Django-only authentication by email and password
                users = User.objects.filter(email=email)
                if not users.exists():
                    messages.error(request, "Invalid email or password.")
                    return redirect('login')
                matching_user = None
                for u in users:
                    if u.check_password(password):
                        matching_user = u
                        break
                if not matching_user:
                    messages.error(request, "Invalid email or password.")
                    return redirect('login')
                django_login(request, matching_user, backend='django.contrib.auth.backends.ModelBackend')
                messages.success(request, f"Login successful for {matching_user.username}!")
                return redirect('dashboard')
        except exceptions.FirebaseError as e:
            # Firebase-specific errors
            if e.code == 'auth/user-not-found':
                messages.error(request, "No user found with that email address.")
            elif e.code == 'auth/invalid-email':
                messages.error(request, "The email address is not valid.")
            else:
                messages.error(request, f"An authentication error occurred: {e.code}")
            return redirect('login')
        except ValueError:
            # Handles: "The default Firebase app does not exist" when Admin SDK isn’t initialized
            try:
                django_user = User.objects.get(email=email)
            except User.DoesNotExist:
                messages.error(request, "Invalid email or password.")
                return redirect('login')
            if not django_user.check_password(password):
                messages.error(request, "Invalid email or password.")
                return redirect('login')
            django_login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, f"Login successful for {django_user.username}!")
            return redirect('dashboard')

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


@api_view(['POST'])
def firebase_login(request):
    """Verify a Firebase ID token, create/find the Django user, and log them in."""
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