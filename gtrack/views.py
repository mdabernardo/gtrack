# gtrack/views.py

from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login as django_login, logout as django_logout
from django.conf import settings 
import json
import os
import uuid
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
from .firebase_sync import (
    sync_prediction_to_firestore, 
    sync_scheduling_assistance_to_firestore,
    sync_collector_schedule_if_missing,
    fetch_scheduling_assistance_items,
    fetch_road_reports,
    mark_road_report_processed,
    create_firestore_notification,
    sync_reroute_to_firestore,
)

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
        explain = str(request.query_params.get('explain', '') or '').strip().lower() in ('1', 'true', 'yes')
        mirror = str(request.query_params.get('mirror', '') or '').strip().lower() not in ('0', 'false', 'no')
        iso = request.query_params.get('date')
        report_id = request.query_params.get('report_id') or request.query_params.get('reportId')
        report_collection = request.query_params.get('report_collection') or request.query_params.get('reportCollection')
        report_location = request.query_params.get('location_name') or request.query_params.get('locationName') or request.query_params.get('location')
        min_level = request.query_params.get('min_level')
        start_policy = request.query_params.get('start_policy') or 'highest_score'
        opt_date = None
        if iso:
            try:
                opt_date = datetime.strptime(str(iso).strip(), '%Y-%m-%d').date()
            except Exception:
                opt_date = None

        road_reports = None
        if report_id:
            report_doc = None
            if firestore:
                try:
                    db = firestore.client()
                    targets = [str(report_collection)] if report_collection else ["road_reports", "road_report"]
                    for col_name in targets:
                        if not col_name:
                            continue
                        snap = db.collection(col_name).document(str(report_id)).get()
                        if snap.exists:
                            d = snap.to_dict() or {}
                            if isinstance(d, dict):
                                d = dict(d)
                                d["id"] = snap.id
                                d["__collection__"] = col_name
                                report_doc = d
                                break
                except Exception:
                    report_doc = None
            if not report_doc:
                report_doc = {
                    "id": str(report_id),
                    "__collection__": str(report_collection) if report_collection else None,
                    "location": str(report_location or ""),
                }
            road_reports = [report_doc]
        try:
            result = ai_predictor.optimize_route_by_garbage_level(
                route.id,
                explain=explain,
                mirror=mirror,
                road_reports=road_reports,
                optimization_date=opt_date,
                min_level=min_level,
                start_policy=start_policy,
            )
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
        points = RoutePoint.objects.filter(route=route).order_by('order').select_related('location')
        etas = []
        cum = 0
        start_tm = pred['predicted_start_time']
        for p in points:
            eta_hour = (start_tm.hour * 60 + start_tm.minute + cum) // 60
            eta_min = (start_tm.hour * 60 + start_tm.minute + cum) % 60
            eta_str = f"{int(eta_hour)%24:02d}:{int(eta_min):02d}"
            etas.append({
                'point_id': p.id,
                'location_name': p.location.name,
                'order': p.order,
                'eta': eta_str,
            })
            cum += int(p.estimated_time_minutes or 5)
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
                    etas=etas,
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
            'etas': etas,
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
        Notification.objects.create(
            user=resident.user,
            type='verification',
            title='Resident Verification',
            message='Your account has been verified.',
        )
        return Response({'status': 'verified'})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        # Basic reject: keep as not verified; could extend with reason field
        resident = self.get_object()
        resident.is_verified = False
        resident.save()
        reason = ""
        try:
            reason = (request.data or {}).get("reason") or ""
        except Exception:
            reason = ""
        message = 'Your account verification was rejected.'
        if reason:
            message = f'{message} Reason: {reason}'
        Notification.objects.create(
            user=resident.user,
            type='verification',
            title='Resident Verification',
            message=message,
        )
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
        request_user = getattr(self.request, "user", None)
        if request_user and request_user.is_authenticated and not request_user.is_staff:
            qs = qs.filter(user=request_user)
        elif user_id:
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

        def _norm_email(raw: str) -> str:
            return ''.join((raw or '').split()).strip().lower()

        fixed_emails = getattr(settings, 'ADMIN_FIXED_EMAILS', [])
        if not isinstance(fixed_emails, (list, tuple, set)):
            fixed_emails = [getattr(settings, 'ADMIN_FIXED_EMAIL', '')]
        allowed_emails = {_norm_email(e) for e in fixed_emails if e}

        # Fixed admin override: allow direct login for configured admin Gmail(s)
        try:
            fixed_password = getattr(settings, 'ADMIN_FIXED_PASSWORD', '') or ''
            if (
                _norm_email(email) in allowed_emails
                and fixed_password
                and password == fixed_password
            ):
                admin_email = _norm_email(email)
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
                    django_user.save(update_fields=['is_staff', 'is_superuser', 'password'])
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
            if _norm_email(email_from_token or "") in allowed_emails:
                django_user.is_staff = True
                django_user.is_superuser = True
                django_user.set_unusable_password()
                django_user.save(update_fields=["is_staff", "is_superuser", "password"])
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
        'admin_login_enabled': bool(getattr(settings, 'ADMIN_FIXED_PASSWORD', '') or ''),
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

def _is_staff_user(user) -> bool:
    try:
        return bool(user and getattr(user, "is_authenticated", False) and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)))
    except Exception:
        return False


@api_view(['POST', 'GET'])
def check_road_reports(request):
    """
    Checks for new road reports and sends notifications.
    """
    if not _is_staff_user(getattr(request, "user", None)):
        return Response({'status': 'forbidden'}, status=403)
    reports = fetch_road_reports(only_new=True)
    if not reports:
        return Response({'status': 'no_new_reports'})
    
    processed_count = 0
    notifications_sent = 0
    admin_users = (User.objects.filter(is_staff=True) | User.objects.filter(is_superuser=True)).distinct()
    collectors = GarbageCollector.objects.all()
    affected_locations = []
    
    # Process each report
    for report in reports:
        loc_name = report.get('location', 'Unknown Location')
        desc = report.get('description', 'Road issue reported')
        report_id = report.get('id')
        report_collection = report.get('__collection__')
        
        title = f"Road Alert: {loc_name}"
        body = f"Issue reported at {loc_name}: {desc}. Please review in Road Map to generate an alternative route."
        
        # 1. Create Web Notifications for all residents with notifications enabled
        residents = Resident.objects.filter(notification_enabled=True)
        for res in residents:
            Notification.objects.create(
                user=res.user,
                type='delay',
                title=title,
                message=body
            )
            
            # 2. Send Mobile Notification (FCM)
            if res.fcm_token and firebase_manager:
                try:
                    firebase_manager.send_push_notification(
                        token=res.fcm_token,
                        title=title,
                        body=body,
                        data={'type': 'road_report', 'location': loc_name}
                    )
                except Exception:
                    pass
        
        # 3. Notify Drivers (Garbage Collectors)
        for collector in collectors:
            # Web Notification
            Notification.objects.create(
                user=collector.user,
                type='delay',
                title=title,
                message=body
            )
            
            # Mobile Notification (FCM) - Try to fetch token from Firestore
            if firebase_manager and firestore:
                try:
                    db = firestore.client()
                    # Assuming username is the UID
                    doc_ref = db.collection('collectors').document(collector.user.username)
                    doc = doc_ref.get()
                    if doc.exists:
                        data = doc.to_dict()
                        token = data.get('fcmToken') or data.get('fcm_token')
                        if token:
                            firebase_manager.send_push_notification(
                                token=token,
                                title=title,
                                body=body,
                                data={'type': 'road_report', 'location': loc_name}
                            )
                except Exception as e:
                    print(f"Error notifying driver {collector}: {e}")

        for admin in admin_users:
            try:
                Notification.objects.create(
                    user=admin,
                    type='change',
                    title=title,
                    message=body
                )
            except Exception:
                pass
        
        notifications_sent += residents.count() + collectors.count() + admin_users.count()

        # 4. Mirror notification to Firestore 'notifications' collection
        create_firestore_notification(
            title=title,
            body=body,
            target="residents",
            route_id=None,
            disruption_type="road_report",
            location_name=loc_name,
        )
        create_firestore_notification(
            title=title,
            body=body,
            target="collectors",
            route_id=None,
            disruption_type="road_report",
            location_name=loc_name,
        )
        create_firestore_notification(
            title=title,
            body=body,
            target="admin",
            route_id=None,
            disruption_type="road_report",
            location_name=loc_name,
        )
        
        processed_count += 1
        if loc_name and loc_name not in affected_locations:
            affected_locations.append(str(loc_name))

    return Response({'status': 'processed', 'reports_count': processed_count, 'notifications_sent': notifications_sent})

@api_view(['POST', 'GET'])
def approve_reroute(request):
    if not _is_staff_user(getattr(request, "user", None)):
        return Response({'status': 'forbidden'}, status=403)
    data = request.data if request.method == 'POST' else request.query_params
    route_id = data.get('route_id') or data.get('routeId')
    try:
        route_id_int = int(route_id)
    except Exception:
        route_id_int = None
    route_name = data.get('route_name') or data.get('routeName') or 'Main Route'
    location_name = data.get('location_name') or data.get('locationName') or data.get('location') or 'multiple locations'
    report_id = data.get('report_id') or data.get('reportId') or data.get('road_report_id')
    report_collection = data.get('report_collection') or data.get('reportCollection') or data.get('road_report_collection')
    iso_date = data.get('date') or data.get('reroute_date') or data.get('rerouteDate')
    handoff = str(data.get('handoff') or data.get('truck_full') or '').strip().lower() in ('1', 'true', 'yes')
    full_collector_id = str(data.get('full_collector_id') or data.get('collector_id') or '').strip()
    if handoff and full_collector_id not in ("1", "2"):
        full_collector_id = "1"

    title = f"Route Rerouted: {route_name}"
    body = f"New route approved due to road report near {location_name}. Please follow the updated path."

    residents = Resident.objects.filter(notification_enabled=True)
    collectors = GarbageCollector.objects.all()
    admin_users = (User.objects.filter(is_staff=True) | User.objects.filter(is_superuser=True)).distinct()

    created_web = 0
    pushed = 0

    reroute_written = False
    handoff_applied = False
    handoff_summary = None
    try:
        if route_id_int is not None and report_id:
            route_obj = Route.objects.filter(id=route_id_int).first()
            if route_obj:
                route_name = route_obj.name or route_name
            reroute_date = timezone.localdate()
            if iso_date:
                try:
                    reroute_date = datetime.strptime(str(iso_date).strip(), '%Y-%m-%d').date()
                except Exception:
                    reroute_date = timezone.localdate()
            report_doc = None
            if firestore and report_id:
                try:
                    db = firestore.client()
                    targets = [str(report_collection)] if report_collection else ["road_reports", "road_report"]
                    for col_name in targets:
                        if not col_name:
                            continue
                        snap = db.collection(col_name).document(str(report_id)).get()
                        if snap.exists:
                            d = snap.to_dict() or {}
                            if isinstance(d, dict):
                                d = dict(d)
                                d["id"] = snap.id
                                d["__collection__"] = col_name
                                report_doc = d
                                break
                except Exception:
                    report_doc = None

            if not report_doc:
                report_doc = {"id": str(report_id), "__collection__": str(report_collection) if report_collection else None, "location": str(location_name)}

            result = ai_predictor.optimize_route_by_garbage_level(
                route_id_int,
                explain=True,
                mirror=False,
                road_reports=[report_doc],
            )
            suggested_points = result.get("suggested_points") or []
            factors = result.get("factors") or {}
            generated_at = result.get("generated_at")
            trace = result.get("trace")

            allowed = {
                "sitio 6 basketball court",
                "gulayan",
                "sm hoa",
                "lucas compound",
                "justice",
                "dumpsite",
            }
            filtered = []
            for p in suggested_points:
                nm = str(p.get("location_name") or p.get("locationName") or "").strip().lower()
                if nm in allowed:
                    filtered.append(p)
            if filtered:
                suggested_points = filtered

            rr = dict(report_doc) if isinstance(report_doc, dict) else {}
            rr.setdefault("id", str(report_id))
            rr.setdefault("__collection__", str(report_collection) if report_collection else None)
            if request.user and getattr(request.user, "is_authenticated", False):
                rr["approved_by"] = str(getattr(request.user, "username", "") or "")
            reroute_id = f"{rr.get('__collection__') or 'road_report'}_{rr.get('id') or report_id}"
            reroute_id = str(reroute_id).replace("/", "_")

            reroute_written = False
            if firestore:
                try:
                    db = firestore.client()

                    reroute_payload = {
                        "approved": True,
                        "source": "approve_reroute",
                        "reportId": str(report_id),
                        "reportCollection": str(rr.get("__collection__") or report_collection or ""),
                        "location_name": str(rr.get("location") or location_name or ""),
                        "locationName": str(rr.get("location") or location_name or ""),
                        "id": str(reroute_id),
                        "route_id": int(route_id_int),
                        "routeId": str(route_id_int),
                        "route_name": str(route_name),
                        "routeName": str(route_name),
                        "date": reroute_date.strftime("%Y-%m-%d"),
                        "suggested_points": list(suggested_points),
                        "suggestedPoints": list(suggested_points),
                        "factors": dict(factors or {}),
                        "generated_at": generated_at,
                        "generatedAt": generated_at,
                        "status": "approved",
                        "updated_at": firestore.SERVER_TIMESTAMP,
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                        "road_report": dict(rr),
                    }
                    if isinstance(trace, dict):
                        reroute_payload["trace"] = trace

                    db.collection("reroutr").document(str(reroute_id)).set(reroute_payload, merge=True)
                    reroute_written = True

                    update_payload = {
                        "reroute": {
                            "rerouteId": str(reroute_id),
                            "route_id": int(route_id_int),
                            "routeId": str(route_id_int),
                            "route_name": str(route_name),
                            "routeName": str(route_name),
                            "date": reroute_date.strftime("%Y-%m-%d"),
                            "suggested_points": list(suggested_points),
                            "suggestedPoints": list(suggested_points),
                            "factors": dict(factors or {}),
                            "generated_at": generated_at,
                            "generatedAt": generated_at,
                            "road_report": dict(rr),
                        },
                        "status": "processed",
                        "processed_at": firestore.SERVER_TIMESTAMP,
                        "rerouteId": str(reroute_id),
                    }
                    if isinstance(trace, dict):
                        update_payload["reroute"]["trace"] = trace

                    targets = [str(rr.get("__collection__") or report_collection)] if (rr.get("__collection__") or report_collection) else ["road_reports", "road_report"]
                    for col_name in targets:
                        if not col_name:
                            continue
                        try:
                            db.collection(col_name).document(str(report_id)).set(update_payload, merge=True)
                        except Exception:
                            continue
                except Exception:
                    reroute_written = False

            if handoff and firestore:
                try:
                    db = firestore.client()
                    ymd = reroute_date.strftime("%Y%m%d")
                    other_collector_id = "2" if full_collector_id == "1" else "1"
                    doc_full_id = f"{route_id_int}_{ymd}_{full_collector_id}"
                    doc_other_id = f"{route_id_int}_{ymd}_{other_collector_id}"

                    stops = []
                    for p in suggested_points:
                        nm = str(p.get("location_name") or p.get("locationName") or p.get("name") or "").strip()
                        if not nm:
                            continue
                        try:
                            score_val = float(p.get("score") or 0.0)
                        except Exception:
                            score_val = 0.0
                        try:
                            lat_val = float(p.get("latitude") or 0.0)
                            lng_val = float(p.get("longitude") or 0.0)
                        except Exception:
                            lat_val = 0.0
                            lng_val = 0.0
                        stops.append({
                            "name": nm,
                            "latitude": lat_val,
                            "longitude": lng_val,
                            "garbageLevel": int(round(score_val)),
                        })

                    split_idx = None
                    total = 0.0
                    threshold = 240.0
                    for i, st in enumerate(stops):
                        total += float(st.get("garbageLevel") or 0.0)
                        if total >= threshold:
                            split_idx = i + 1
                            break
                    if split_idx is None:
                        split_idx = max(1, int((len(stops) + 1) / 2))
                    split_idx = min(split_idx, len(stops))

                    full_stops = stops[:split_idx]
                    remaining_stops = stops[split_idx:]
                    if remaining_stops:
                        handoff_id = f"handoff_{reroute_id}"
                        db.collection("collector_schedules").document(doc_full_id).set({
                            "date": reroute_date.strftime("%Y-%m-%d"),
                            "dayName": reroute_date.strftime("%A"),
                            "dayIndex": int(reroute_date.weekday()),
                            "routeId": str(route_id_int),
                            "routeName": route_name,
                            "collectorId": str(full_collector_id),
                            "day_name": reroute_date.strftime("%A"),
                            "day_index": int(reroute_date.weekday()),
                            "route_id": int(route_id_int),
                            "route_name": route_name,
                            "collector_id": str(full_collector_id),
                            "collectorIdInt": int(full_collector_id),
                            "startTime": "06:00 AM",
                            "endTime": "10:00 PM",
                            "task": "Garbage Collection",
                            "status": "full",
                            "capacity_percent": 100,
                            "recommended_action": "go_to_dropoff_then_delegate",
                            "pickupPlan": {
                                "dominantLocation": "Dumpsite",
                                "locations": full_stops,
                            },
                            "handoff": {
                                "active": True,
                                "handoffId": handoff_id,
                                "handoffTo": str(other_collector_id),
                                "reason": "truck_full",
                                "rerouteId": reroute_id,
                                "reportId": str(report_id),
                                "remainingStopsCount": int(len(remaining_stops)),
                            },
                            "updatedAt": datetime.utcnow(),
                        }, merge=True)

                        db.collection("collector_schedules").document(doc_other_id).set({
                            "date": reroute_date.strftime("%Y-%m-%d"),
                            "dayName": reroute_date.strftime("%A"),
                            "dayIndex": int(reroute_date.weekday()),
                            "routeId": str(route_id_int),
                            "routeName": route_name,
                            "collectorId": str(other_collector_id),
                            "day_name": reroute_date.strftime("%A"),
                            "day_index": int(reroute_date.weekday()),
                            "route_id": int(route_id_int),
                            "route_name": route_name,
                            "collector_id": str(other_collector_id),
                            "collectorIdInt": int(other_collector_id),
                            "startTime": "06:00 AM",
                            "endTime": "10:00 PM",
                            "task": "Garbage Collection",
                            "status": "scheduled",
                            "pickupPlan": {
                                "dominantLocation": "Dumpsite",
                                "locations": remaining_stops,
                            },
                            "handoff": {
                                "active": True,
                                "handoffId": handoff_id,
                                "handoffFrom": str(full_collector_id),
                                "reason": "truck_full",
                                "rerouteId": reroute_id,
                                "reportId": str(report_id),
                                "assignedStopsCount": int(len(remaining_stops)),
                            },
                            "updatedAt": datetime.utcnow(),
                        }, merge=True)

                        handoff_applied = True
                        handoff_summary = {
                            "handoffId": handoff_id,
                            "fullCollectorId": str(full_collector_id),
                            "otherCollectorId": str(other_collector_id),
                            "fullStopsCount": int(len(full_stops)),
                            "remainingStopsCount": int(len(remaining_stops)),
                        }
                except Exception:
                    handoff_applied = False
                    handoff_summary = None
    except Exception:
        reroute_written = False

    for res in residents:
        try:
            Notification.objects.create(user=res.user, type='change', title=title, message=body)
            created_web += 1
        except Exception:
            pass
        if res.fcm_token and firebase_manager:
            try:
                if firebase_manager.send_push_notification(
                    token=res.fcm_token,
                    title=title,
                    body=body,
                    data={'type': 'reroute', 'route_id': str(route_id_int or ''), 'location': str(location_name)},
                ):
                    pushed += 1
            except Exception:
                pass

    for collector in collectors:
        try:
            Notification.objects.create(user=collector.user, type='change', title=title, message=body)
            created_web += 1
        except Exception:
            pass
        if firebase_manager and firestore:
            try:
                db = firestore.client()
                doc_ref = db.collection('collectors').document(collector.user.username)
                doc = doc_ref.get()
                if doc.exists:
                    d = doc.to_dict() or {}
                    token = d.get('fcmToken') or d.get('fcm_token')
                    if token:
                        if firebase_manager.send_push_notification(
                            token=token,
                            title=title,
                            body=body,
                            data={'type': 'reroute', 'route_id': str(route_id_int or ''), 'location': str(location_name)},
                        ):
                            pushed += 1
            except Exception:
                pass

    for admin in admin_users:
        try:
            Notification.objects.create(user=admin, type='change', title=title, message=body)
            created_web += 1
        except Exception:
            pass

    create_firestore_notification(
        title=title,
        body=body,
        target="residents",
        route_id=route_id_int,
        disruption_type="reroute",
        location_name=str(location_name),
    )
    create_firestore_notification(
        title=title,
        body=body,
        target="collectors",
        route_id=route_id_int,
        disruption_type="reroute",
        location_name=str(location_name),
    )
    create_firestore_notification(
        title=title,
        body=body,
        target="admin",
        route_id=route_id_int,
        disruption_type="reroute",
        location_name=str(location_name),
    )

    if report_id:
        try:
            mark_road_report_processed(str(report_id), str(report_collection) if report_collection else None)
        except Exception:
            pass

    return Response({
        'status': 'ok',
        'route_id': route_id_int,
        'route_name': route_name,
        'location_name': location_name,
        'web_notifications_created': created_web,
        'push_notifications_sent': pushed,
        'reroute_written': bool(reroute_written),
        'handoff_applied': bool(handoff_applied),
        'handoff': handoff_summary,
    })




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
    route = Route.objects.filter(name__iexact='Main Route').first() or Route.objects.first()
    route_points = []
    route_id = None
    if route:
        route_id = route.id
        pts = RoutePoint.objects.filter(route=route).order_by('order').select_related('location')
        for p in pts:
            route_points.append({
                'order': p.order,
                'location_name': p.location.name,
            })
    context = {
        'route_points_json': json.dumps(route_points),
        'route_id': route_id,
    }
    return render(request, 'dashboard.html', context)

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
    route = Route.objects.filter(name__iexact='Main Route').first() or Route.objects.first()
    route_points = []
    if route:
        pts = RoutePoint.objects.filter(route=route).order_by('order').select_related('location')
        for p in pts:
            route_points.append({
                'order': p.order,
                'location_name': p.location.name,
            })
    context = {
        'route_points_json': json.dumps(route_points),
    }
    return render(request, 'garbage_level.html', context)

@login_required
def road_map_view(request):
    if not getattr(request.user, "is_staff", False):
        return redirect('dashboard')
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    context = {
        'active_tab': 'road_map',
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
    }
    return render(request, 'road_map.html', context)

@login_required
def history_view(request):
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    context = {
        'active_tab': 'history',
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
    }
    return render(request, 'history.html', context)

@login_required
def schedules_view(request):
    """
    Renders the schedules page.
    Shows the weekly template (CollectionSchedule) and recent AI predictions.
    """
    schedules = CollectionSchedule.objects.all().order_by('day_of_week', 'start_time')
    target_names = {
        'sitio 6 basketball court',
        'gulayan',
        'sm hoa',
        'lucas compound',
        'justice',
        'dumpsite',
    }
    resident_route = Route.objects.filter(name__iexact='Main Route').first()
    if not resident_route:
        best = None
        best_score = -1
        for r in Route.objects.all():
            pts = r.points.select_related('location').all()
            score = 0
            for p in pts:
                nm = (p.location.name or '').strip().lower()
                if nm in target_names:
                    score += 1
            if score > best_score:
                best, best_score = r, score
        resident_route = best or Route.objects.first()
    route_points = []
    resident_route_id = None
    if resident_route:
        resident_route_id = resident_route.id
        pts = RoutePoint.objects.filter(route=resident_route).order_by('order').select_related('location')
        for p in pts:
            route_points.append({
                'point_id': p.id,
                'order': p.order,
                'location_name': p.location.name,
                'latitude': p.location.latitude,
                'longitude': p.location.longitude,
            })
    # Auto-refresh today's+upcoming schedules once per day using Firestore-backed history
    try:
        allow_auto = os.getenv('AUTO_REFRESH_AI', 'true').lower() in ('1', 'true', 'yes')
        if allow_auto:
            today = datetime.today().date()
            last_key = 'ai_sched_last_refresh'
            last_val = cache.get(last_key)
            if last_val != today.isoformat():
                days = int(os.getenv('AUTO_REFRESH_AI_DAYS', '7'))
                for route in Route.objects.all():
                    points = RoutePoint.objects.filter(route=route).order_by('order').select_related('location')
                    for i in range(days):
                        target_date = today + timedelta(days=i)
                        pred = ai_predictor.predict_route_schedule(route.id, target_date)
                        if pred:
                            start_tm = pred['predicted_start_time']
                            end_tm = pred['predicted_end_time']
                            start_str = start_tm.strftime("%I:%M %p")
                            end_str = end_tm.strftime("%I:%M %p")
                            confidence = float(pred.get('confidence_score', 0.0))
                            factors = pred.get('factors', {})
                            etas = []
                            cum = 0
                            for p in points:
                                eta_dt = start_tm + timedelta(minutes=int(cum))
                                eta_str = eta_dt.strftime("%I:%M %p")
                                etas.append({
                                    'point_id': p.id,
                                    'location_name': p.location.name,
                                    'order': p.order,
                                    'eta': eta_str,
                                })
                                cum += int(p.estimated_time_minutes or 5)
                            try:
                                sync_scheduling_assistance_to_firestore(
                                    route_id=route.id,
                                    route_name=route.name,
                                    assistance_date=target_date,
                                    predicted_start_time=start_str,
                                    predicted_end_time=end_str,
                                    confidence_score=confidence,
                                    factors=factors,
                                    etas=etas,
                                )
                            except Exception:
                                pass
                cache.set(last_key, today.isoformat(), 60 * 60 * 24)
    except Exception:
        pass
    try:
        allow_cs = os.getenv('AUTO_REFRESH_COLLECTOR_SCHEDULES', 'true').lower() in ('1', 'true', 'yes')
        if allow_cs and firestore:
            today = datetime.today().date()
            last_key = 'collector_sched_last_refresh'
            last_val = cache.get(last_key)
            if last_val != today.isoformat():
                future_days = int(os.getenv('AUTO_REFRESH_COLLECTOR_DAYS', '30'))
                for route in Route.objects.all():
                    points = RoutePoint.objects.filter(route=route).order_by('order').select_related('location')
                    base_stops = []
                    start_minutes = 6 * 60
                    for idx, p in enumerate(points):
                        if not getattr(p, 'location', None):
                            continue
                        nm = (p.location.name or '').strip()
                        if not nm:
                            continue
                        tmin = start_minutes + (idx * 50)
                        hh = int(tmin // 60) % 24
                        mm = int(tmin % 60)
                        base_stops.append({
                            'name': nm,
                            'latitude': float(p.location.latitude or 0.0),
                            'longitude': float(p.location.longitude or 0.0),
                            'garbageLevel': 0,
                            'plannedTime': datetime(2000, 1, 1, hh, mm).strftime("%I:%M %p"),
                        })
                    for i in range(max(0, future_days) + 1):
                        target_date = today + timedelta(days=i)
                        for collector_id in ('1', '2'):
                            try:
                                sync_collector_schedule_if_missing(
                                    route_id=route.id,
                                    route_name=route.name,
                                    schedule_date=target_date,
                                    collector_id=collector_id,
                                    pickup_locations=base_stops,
                                )
                            except Exception:
                                pass
                cache.set(last_key, today.isoformat(), 60 * 60 * 24)
    except Exception:
        pass
    today = datetime.today().date()
    end_date = today + timedelta(days=30)
    firestore_items = fetch_scheduling_assistance_items(start_date=today, end_date=end_date)
    predictions = []
    if firestore_items:
        for it in firestore_items:
            route_name = it.get('route_name') or it.get('routeName')
            predictions.append({
                'route_name': route_name,
                'route_id': it.get('route_id') or it.get('routeId'),
                'date': it.get('date'),
                'predicted_start': it.get('predicted_start') or it.get('predictedStart'),
                'predicted_end': it.get('predicted_end') or it.get('predictedEnd'),
                'confidence': it.get('confidence'),
                'factors': it.get('factors') or {},
                'updated_at': it.get('updated_at') or it.get('updatedAt'),
                'source': 'firestore.scheduling_assistance',
            })
        predictions.sort(key=lambda x: (str(x.get('date') or ''), str(x.get('predicted_start') or ''), str(x.get('route_name') or '')))
    else:
        qs = AIRoutePrediction.objects.filter(date__gte=today).select_related('route').order_by('date', 'predicted_start_time')
        for p in qs:
            predictions.append({
                'route_name': p.route.name if getattr(p, 'route', None) else None,
                'route_id': p.route_id,
                'date': p.date.isoformat() if p.date else None,
                'predicted_start': p.predicted_start_time.strftime('%H:%M') if p.predicted_start_time else None,
                'predicted_end': p.predicted_end_time.strftime('%H:%M') if p.predicted_end_time else None,
                'confidence': p.confidence_score,
                'factors': p.factors or {},
                'updated_at': None,
                'source': 'sql.ai_route_prediction',
            })
    
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    context = {
        'schedules': schedules,
        'predictions': predictions,
        'active_tab': 'resident_schedules',
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
        'resident_route_id': resident_route_id,
        'resident_route_points_json': json.dumps(route_points),
    }
    return render(request, 'schedules.html', context)


@login_required
def collector_schedules_view(request):
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    context = {
        'active_tab': 'collector_schedules',
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
    }
    return render(request, 'collector_schedules.html', context)

@login_required
def collector_route_suggestions_view(request):
    app_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
    firebase_config_json = json.dumps(settings.FIREBASE_CLIENT_CONFIG)
    google_maps_api_key = settings.FIREBASE_CLIENT_CONFIG.get('apiKey', '')
    context = {
        'active_tab': 'collector_route_suggestions',
        'firebase_config_json': firebase_config_json,
        'app_id': app_id,
        'google_maps_api_key': google_maps_api_key,
    }
    return render(request, 'collector_route_suggestions.html', context)

@login_required
def notification_view(request):
    if not getattr(request.user, "is_staff", False):
        return redirect('dashboard')
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
    if not getattr(request.user, "is_staff", False):
        return redirect('dashboard')
    unverified_residents = Resident.objects.filter(is_verified=False)
    context = {
        'unverified_residents': unverified_residents
    }
    return render(request, 'resident_verification.html', context)

def driver_verification_view(request):
    """Create driver Firebase Auth accounts and store metadata in Firestore."""

    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        username = request.POST.get('username', '').strip()
        address = request.POST.get('address', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        age = request.POST.get('age', '').strip()
        sex = request.POST.get('sex', '').strip()
        gmail_email = request.POST.get('gmail_email', '').strip()
        password = request.POST.get('password', '').strip()

        # Basic validation
        if not (full_name and username and address and contact_number and age and sex and gmail_email and password):
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
                        'username': username,
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
    pending_count = 0
    def _norm_status(v, doc: dict):
        s = (v or "").strip()
        low = s.lower()
        if not low:
            if doc.get("isVerified") is True or doc.get("is_verified") is True:
                return "Verified"
            return "Pending"
        if low in ("pending", "unverified", "not verified", "not_verified"):
            return "Pending"
        if low in ("verified",):
            return "Verified"
        if low in ("rejected",):
            return "Rejected"
        if doc.get("isVerified") is True or doc.get("is_verified") is True:
            return "Verified"
        if doc.get("isVerified") is False or doc.get("is_verified") is False:
            return "Pending"
        return s
    try:
        if firestore and getattr(firebase_admin, "_apps", None):
            db = firestore.client()
            try:
                for col_name in ("residents",):
                    try:
                        docs = db.collection(col_name).get()
                    except Exception:
                        docs = []
                    if not docs:
                        continue
                    for doc in docs:
                        data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                        if not isinstance(data, dict):
                            continue
                        st = _norm_status(data.get("verificationStatus") or data.get("status"), data)
                        if str(st).lower() == "pending":
                            pending_count += 1
                    break
            except Exception:
                pending_count = 0
            if pending_count == 0:
                docs = db.collection("notifications").where("kind", "==", "resident_verification").where("subtype", "==", "request").get()
                for doc in docs:
                    data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                    if not isinstance(data, dict):
                        continue
                    st = _norm_status(
                        data.get("verificationStatus")
                        or data.get("status")
                        or ((data.get("data") or {}).get("verificationStatus"))
                        or ((data.get("data") or {}).get("status")),
                        (data.get("resident") or (data.get("data") or {}).get("resident") or data),
                    )
                    if str(st).lower() == "pending":
                        pending_count += 1
        else:
            pending_count = Resident.objects.filter(is_verified=False).count()
    except Exception:
        pending_count = Resident.objects.filter(is_verified=False).count()

    admin_users = User.objects.filter(is_staff=True)
    created = 0
    for admin in admin_users:
        count = int(pending_count)
        if count == 0:
            continue
        title = 'Pending Resident Verifications'
        message = f'There are {count} residents awaiting verification.'
        Notification.objects.create(
            user=admin,
            type='verification',
            title=title,
            message=message,
        )
        try:
            create_firestore_notification(
                title=title,
                body=message,
                target="admin",
                route_id=None,
                disruption_type="resident_verification",
                doc_id=f"resident_verification_pending_{timezone.localdate().strftime('%Y%m%d')}",
                extra_data={"pending_count": int(count)},
            )
        except Exception:
            pass
        created += 1
    return Response({'status': 'ok', 'created': created, 'pending_count': int(pending_count)})


def _norm_resident_verification_status(raw, doc: dict):
    v = (raw or "").strip()
    low = v.lower()
    if not low:
        if doc.get("isVerified") is True or doc.get("is_verified") is True:
            return "Verified"
        return "Pending"
    if low in ("pending", "unverified", "not verified", "not_verified", "un-verfied", "unverify", "unverified "):
        return "Pending"
    if low in ("verified", "approved", "accept", "accepted"):
        return "Verified"
    if low in ("rejected", "reject", "denied", "declined"):
        return "Rejected"
    if doc.get("isVerified") is True or doc.get("is_verified") is True:
        return "Verified"
    if doc.get("isVerified") is False or doc.get("is_verified") is False:
        return "Pending"
    return v


def _verification_doc_values(*docs):
    values = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        nested = doc.get("data") if isinstance(doc.get("data"), dict) else {}
        resident = doc.get("resident") if isinstance(doc.get("resident"), dict) else nested.get("resident")
        if not isinstance(resident, dict):
            resident = {}
        for key in (
            "requestId", "verificationRequestId", "parentRequestId", "verificationId",
            "uid", "userId", "recipientUid", "email", "gmail_email",
        ):
            for source in (doc, nested, resident):
                value = source.get(key) if isinstance(source, dict) else None
                if value is not None and str(value).strip():
                    values.append(str(value).strip())
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _find_resident_verification_record(db, doc_id: str):
    doc_id = str(doc_id or "").strip()
    resident_ref = None
    resident_snap = None
    resident_data = {}
    resident_collection = None
    request_ref = None
    request_data = {}

    try:
        request_ref = db.collection("notifications").document(doc_id)
        request_snap = request_ref.get()
        if getattr(request_snap, "exists", False):
            request_data = request_snap.to_dict() or {}
    except Exception:
        request_ref = None
        request_data = {}

    lookup_values = [doc_id] + _verification_doc_values(request_data)

    for col_name in ("residents",):
        try:
            ref = db.collection(col_name).document(doc_id)
            snap = ref.get()
        except Exception:
            ref = None
            snap = None
        if snap is not None and getattr(snap, "exists", False):
            resident_ref = ref
            resident_snap = snap
            resident_data = snap.to_dict() or {}
            resident_collection = col_name
            break

        for field in ("verificationRequestId", "uid", "userId", "recipientUid", "email", "gmail_email"):
            if resident_ref is not None:
                break
            for value in lookup_values:
                try:
                    matches = db.collection(col_name).where(field, "==", value).limit(1).get()
                except Exception:
                    matches = []
                if matches:
                    resident_snap = matches[0]
                    resident_ref = db.collection(col_name).document(resident_snap.id)
                    resident_data = resident_snap.to_dict() or {}
                    resident_collection = col_name
                    break
        if resident_ref is not None:
            break

    return resident_ref, resident_snap, resident_data if isinstance(resident_data, dict) else {}, resident_collection, request_ref, request_data if isinstance(request_data, dict) else {}


def _update_verification_request_notifications(db, status_value: str, result_notification_id: str, decided_by: str, request_ids=None, recipient_uid=None, reason=None):
    request_ids = [str(v).strip() for v in (request_ids or []) if str(v or "").strip()]
    refs_by_path = {}
    update = {
        "verificationStatus": status_value,
        "status": status_value,
        "resultNotificationId": result_notification_id,
        "decidedAt": firestore.SERVER_TIMESTAMP,
        "decidedBy": decided_by,
    }
    if reason:
        update["rejectionReason"] = str(reason)
    elif status_value == "Verified":
        update["rejectionReason"] = firestore.DELETE_FIELD

    for request_id in request_ids:
        try:
            ref = db.collection("notifications").document(request_id)
            snap = ref.get()
            if getattr(snap, "exists", False):
                refs_by_path[ref.path] = ref
        except Exception:
            pass

    for field in ("requestId", "parentRequestId", "verificationId"):
        for request_id in request_ids:
            try:
                docs = db.collection("notifications").where(field, "==", request_id).get()
            except Exception:
                docs = []
            for snap in docs:
                try:
                    refs_by_path[snap.reference.path] = snap.reference
                except Exception:
                    pass

    if recipient_uid:
        for field in ("recipientUid", "data.recipientUid"):
            try:
                docs = db.collection("notifications").where("kind", "==", "resident_verification").where("subtype", "==", "request").where(field, "==", str(recipient_uid)).get()
            except Exception:
                docs = []
            for snap in docs:
                try:
                    data = snap.to_dict() or {}
                    current = _norm_resident_verification_status(
                        data.get("verificationStatus") or data.get("status") or ((data.get("data") or {}).get("verificationStatus")),
                        data,
                    )
                    if str(current).lower() == "pending":
                        refs_by_path[snap.reference.path] = snap.reference
                except Exception:
                    pass

    updated = 0
    for ref in refs_by_path.values():
        try:
            ref.set(update, merge=True)
            updated += 1
        except Exception:
            pass
    return updated


@api_view(['POST'])
def resident_verification_submit(request):
    if not firestore:
        return Response({'status': 'error', 'message': 'Firestore not available'}, status=500)

    token = ""
    try:
        auth_header = request.headers.get("Authorization") or ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    except Exception:
        token = ""

    if not token:
        return Response({'status': 'forbidden'}, status=401)

    if not getattr(firebase_admin, "_apps", None):
        return Response({'status': 'error', 'message': 'Firebase Admin SDK not initialized'}, status=500)

    try:
        decoded = auth.verify_id_token(token)
        uid = decoded.get("uid") or ""
    except Exception:
        return Response({'status': 'forbidden'}, status=401)

    if not uid:
        return Response({'status': 'error', 'message': 'Missing uid'}, status=400)

    resident = {}
    try:
        resident = dict(request.data or {})
    except Exception:
        resident = {}

    request_id = f"RVREQ_{uuid.uuid4().hex[:16]}"
    resident_doc_id = ""

    try:
        db = firestore.client()
        email = ""
        try:
            email = (resident.get("email") or decoded.get("email") or resident.get("gmail_email") or "").strip()
        except Exception:
            email = ""

        doc_key = email or uid
        resident_doc_id = str(doc_key)
        doc_ref = db.collection("residents").document(str(doc_key))
        existing = {}
        try:
            snap = doc_ref.get()
            if getattr(snap, "exists", False):
                existing = snap.to_dict() or {}
        except Exception:
            existing = {}

        existing_status = str(existing.get("verificationStatus") or existing.get("status") or "").strip().lower()
        already_verified = existing.get("isVerified") is True or existing.get("is_verified") is True or existing_status in ("verified", "approved", "accepted")

        if already_verified:
            return Response({'status': 'already_verified', 'requestId': existing.get("verificationRequestId") or ""})

        if existing_status in ("pending", "unverified", "not verified", "not_verified") and existing.get("verificationRequestId"):
            request_id = str(existing.get("verificationRequestId"))

        update = {
            "uid": uid,
            "verificationStatus": "Pending",
            "status": "Pending",
            "isVerified": False,
            "is_verified": False,
            "verificationRequestId": request_id,
            "verificationRequestedAt": firestore.SERVER_TIMESTAMP,
            "resultNotificationId": firestore.DELETE_FIELD,
            "verificationResultNotificationId": firestore.DELETE_FIELD,
            "rejectionReason": firestore.DELETE_FIELD,
        }
        if email:
            update["email"] = email
        if isinstance(resident, dict) and resident:
            for k, v in resident.items():
                if k in ("verificationStatus", "status", "isVerified", "is_verified"):
                    continue
                update[k] = v
        doc_ref.set(update, merge=True)
    except Exception:
        pass

    create_firestore_notification(
        title="Resident Verification",
        body="A resident verification request was submitted.",
        target="admin",
        disruption_type="resident_verification_request",
        doc_id=request_id,
        extra_data={
            "kind": "resident_verification",
            "subtype": "request",
            "requestId": request_id,
            "recipientUid": uid,
            "verificationStatus": "Pending",
            "status": "Pending",
            "source": "mobile",
            "resident": resident,
            "residentDocId": resident_doc_id,
            "resultNotificationId": None,
        },
    )

    return Response({'status': 'ok', 'requestId': request_id})


@api_view(['GET'])
def resident_verification_requests(request):
    if not (request.user and request.user.is_authenticated and request.user.is_staff):
        return Response({'status': 'forbidden'}, status=403)

    if not firestore or not getattr(firebase_admin, "_apps", None):
        return Response({'status': 'ok', 'count': 0, 'items': []})

    status_filter = (request.GET.get("status") or "Pending").strip()

    def _json_safe(value):
        import datetime as _dt
        from decimal import Decimal as _Decimal

        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, _Decimal):
            try:
                return float(value)
            except Exception:
                return str(value)
        if isinstance(value, (_dt.datetime, _dt.date)):
            try:
                return value.isoformat()
            except Exception:
                return str(value)
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                try:
                    key = str(k)
                except Exception:
                    continue
                out[key] = _json_safe(v)
            return out
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(v) for v in list(value)]

        lat = getattr(value, "latitude", None)
        lng = getattr(value, "longitude", None)
        if lat is not None and lng is not None:
            try:
                return {"lat": float(lat), "lng": float(lng)}
            except Exception:
                return {"lat": lat, "lng": lng}

        path = getattr(value, "path", None)
        doc_id = getattr(value, "id", None)
        if path is not None and doc_id is not None:
            try:
                return {"path": str(path), "id": str(doc_id)}
            except Exception:
                return str(value)

        try:
            return str(value)
        except Exception:
            return None

    try:
        db = firestore.client()
        resident_docs = []
        resident_collection = None
        for col_name in ("residents",):
            try:
                docs = []

                desired = (status_filter or "Pending").strip().lower()
                if desired != "all":
                    if desired == "pending":
                        want = ["Pending", "Unverified"]
                    elif desired == "verified":
                        want = ["Verified"]
                    elif desired == "rejected":
                        want = ["Rejected"]
                    else:
                        want = [status_filter]

                    results_by_doc_id = {}
                    for field in ("verificationStatus", "status", "verification_status"):
                        try:
                            qdocs = db.collection(col_name).where(field, "in", want).get()
                        except Exception:
                            qdocs = []
                        for d in qdocs:
                            try:
                                results_by_doc_id[d.id] = d
                            except Exception:
                                pass
                    docs = list(results_by_doc_id.values())

                if not docs:
                    docs = db.collection(col_name).get()
            except Exception:
                docs = []
            if docs:
                resident_docs = docs
                resident_collection = col_name
                break

        if resident_docs:
            items = []
            for snap in resident_docs:
                data = snap.to_dict() or {}
                if not isinstance(data, dict):
                    continue

                status_value = _norm_resident_verification_status(
                    data.get("verificationStatus") or data.get("verification_status") or data.get("status"),
                    data,
                )

                if status_filter.lower() != "all" and str(status_value).lower() != status_filter.lower():
                    continue

                ts = data.get("timestamp") or data.get("createdAt") or data.get("created_at")
                try:
                    created_at = ts.isoformat() if hasattr(ts, "isoformat") else None
                except Exception:
                    created_at = None

                recipient_uid = data.get("uid") or data.get("userId") or data.get("recipientUid") or snap.id

                items.append({
                    "id": snap.id,
                    "requestId": data.get("verificationRequestId") or data.get("requestId") or snap.id,
                    "resultNotificationId": data.get("resultNotificationId") or data.get("verificationResultNotificationId") or data.get("verification_result_notification_id"),
                    "recipientUid": str(recipient_uid or ""),
                    "status": status_value,
                    "verificationStatus": status_value,
                    "rejectionReason": data.get("rejectionReason") or data.get("rejectReason") or data.get("reason"),
                    "resident": _json_safe(data),
                    "createdAt": created_at,
                    "sourceCollection": resident_collection,
                })

            def _sort_key(x):
                v = x.get("createdAt") or ""
                return v

            items.sort(key=_sort_key, reverse=True)
            return Response({'status': 'ok', 'count': len(items), 'items': items})

        results_by_id = {}

        try:
            docs = db.collection("notifications").where("kind", "==", "resident_verification").where("subtype", "==", "request").get()
            for d in docs:
                results_by_id[d.id] = d
        except Exception:
            pass

        try:
            docs = db.collection("notifications").where("data.disruption_type", "==", "resident_verification_request").get()
            for d in docs:
                results_by_id[d.id] = d
        except Exception:
            pass

        if not results_by_id:
            try:
                docs = db.collection("notifications").where("data.kind", "==", "resident_verification").where("data.subtype", "==", "request").get()
                for d in docs:
                    results_by_id[d.id] = d
            except Exception:
                pass

        try:
            legacy_docs = db.collection("resident_verification").get()
        except Exception:
            legacy_docs = []

        for legacy in legacy_docs:
            try:
                legacy_id = str(getattr(legacy, "id", "") or "").strip()
                legacy_data = legacy.to_dict() or {}
                if not legacy_id or not isinstance(legacy_data, dict):
                    continue

                request_id = legacy_id
                if not request_id.startswith("RVREQ_"):
                    request_id = f"RVREQ_{legacy_id}"

                if request_id in results_by_id:
                    continue

                status_value = _norm_resident_verification_status(
                    legacy_data.get("verificationStatus") or legacy_data.get("status"),
                    legacy_data,
                )

                recipient_uid = (
                    legacy_data.get("uid")
                    or legacy_data.get("userId")
                    or legacy_data.get("resident_ID")
                    or legacy_data.get("residentId")
                    or ""
                )

                payload = {
                    "title": "Resident Verification",
                    "body": "A resident verification request was submitted.",
                    "target": "admin",
                    "isRead": False,
                    "read": False,
                    "timestamp": firestore.SERVER_TIMESTAMP if firestore else None,
                    "kind": "resident_verification",
                    "subtype": "request",
                    "requestId": request_id,
                    "recipientUid": str(recipient_uid or ""),
                    "verificationStatus": status_value,
                    "status": status_value,
                    "rejectionReason": legacy_data.get("rejectionReason"),
                    "source": "legacy_resident_verification",
                    "legacyDocId": legacy_id,
                    "resultNotificationId": None,
                    "data": {
                        "disruption_type": "resident_verification_request",
                        "resident": legacy_data,
                    },
                }
                db.collection("notifications").document(request_id).set(payload, merge=True)

                try:
                    results_by_id[request_id] = db.collection("notifications").document(request_id).get()
                except Exception:
                    pass
            except Exception:
                continue

        items = []
        for doc_id, snap in results_by_id.items():
            data = snap.to_dict() or {}
            if not isinstance(data, dict):
                continue

            status_value = _norm_resident_verification_status(
                data.get("verificationStatus")
                or data.get("status")
                or ((data.get("data") or {}).get("verificationStatus"))
                or ((data.get("data") or {}).get("status")),
                (data.get("resident") or (data.get("data") or {}).get("resident") or data),
            )

            if status_filter.lower() != "all" and str(status_value).lower() != status_filter.lower():
                continue

            resident = (data.get("data") or {}).get("resident") or data.get("resident") or {}
            if not isinstance(resident, dict):
                resident = {}

            ts = data.get("timestamp")
            try:
                created_at = ts.isoformat() if hasattr(ts, "isoformat") else None
            except Exception:
                created_at = None

            items.append({
                "id": doc_id,
                "requestId": data.get("requestId") or doc_id,
                "resultNotificationId": data.get("resultNotificationId") or (data.get("data") or {}).get("resultNotificationId"),
                "recipientUid": data.get("recipientUid") or (data.get("data") or {}).get("recipientUid") or resident.get("uid") or resident.get("userId"),
                "status": status_value,
                "verificationStatus": status_value,
                "rejectionReason": data.get("rejectionReason") or (data.get("data") or {}).get("rejectionReason"),
                "resident": _json_safe(resident),
                "createdAt": created_at,
            })

        def _sort_key(x):
            v = x.get("createdAt") or ""
            return v

        items.sort(key=_sort_key, reverse=True)
        return Response({'status': 'ok', 'count': len(items), 'items': items})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)


@api_view(['POST'])
def resident_verification_verify(request, doc_id: str):
    if not (request.user and request.user.is_authenticated and request.user.is_staff):
        return Response({'status': 'forbidden'}, status=403)

    if not firestore:
        return Response({'status': 'error', 'message': 'Firestore not available'}, status=500)

    try:
        db = firestore.client()
        resident_ref, resident_snap, resident_data, resident_collection, request_ref, request_data = _find_resident_verification_record(db, doc_id)
        decided_by = request.user.email or request.user.username or ""

        if resident_ref is not None and resident_snap is not None and resident_snap.exists:
            recipient_uid = resident_data.get("uid") or resident_data.get("userId") or str(doc_id)
            request_ids = _verification_doc_values({"requestId": str(doc_id)}, resident_data, request_data)
            primary_request_id = resident_data.get("verificationRequestId") or resident_data.get("requestId") or request_data.get("requestId") or str(doc_id)
            resident_ref.set(
                {
                    "verificationStatus": "Verified",
                    "status": "Verified",
                    "isVerified": True,
                    "is_verified": True,
                    "verifiedAt": firestore.SERVER_TIMESTAMP,
                    "rejectionReason": firestore.DELETE_FIELD,
                    "decidedAt": firestore.SERVER_TIMESTAMP,
                    "decidedBy": decided_by,
                },
                merge=True,
            )
            try:
                email = resident_data.get("email") or resident_data.get("gmail_email") or request_data.get("email") or ((request_data.get("data") or {}).get("resident") or {}).get("email")
                if email:
                    Resident.objects.filter(user__email__iexact=str(email)).update(is_verified=True)
            except Exception:
                pass

            title = "Resident Verification"
            body = "Your account has been verified."
            result_notification_id = f"resident_verification_result_{primary_request_id}"
            create_firestore_notification(
                title=title,
                body=body,
                target="resident",
                disruption_type="resident_verification_result",
                doc_id=result_notification_id,
                extra_data={
                    "kind": "resident_verification",
                    "subtype": "result",
                    "requestId": str(primary_request_id),
                    "parentRequestId": str(primary_request_id),
                    "recipientUid": str(recipient_uid or ""),
                    "verificationId": str(resident_snap.id),
                    "verificationStatus": "Verified",
                    "residentCollection": resident_collection,
                },
            )
            try:
                resident_ref.set({"resultNotificationId": result_notification_id, "verificationResultNotificationId": result_notification_id}, merge=True)
            except Exception:
                pass
            _update_verification_request_notifications(db, "Verified", result_notification_id, decided_by, request_ids=request_ids, recipient_uid=recipient_uid)

            try:
                token = resident_data.get("fcmToken") or resident_data.get("fcm_token")
                if not token and isinstance(request_data.get("data"), dict):
                    token = request_data["data"].get("fcmToken") or request_data["data"].get("fcm_token")
                if token and firebase_manager:
                    firebase_manager.send_push_notification(token, title, body, data={"requestId": str(primary_request_id), "verificationStatus": "Verified"})
            except Exception:
                pass

            return Response({'status': 'ok'})

        notif_ref = request_ref or db.collection("notifications").document(str(doc_id))
        snap = notif_ref.get()
        data = snap.to_dict() or {} if snap.exists else {}
        if not isinstance(data, dict):
            data = {}

        recipient_uid = data.get("recipientUid") or (data.get("data") or {}).get("recipientUid") or (data.get("data") or {}).get("uid") or data.get("uid")
        resident = (data.get("data") or {}).get("resident") or {}
        if isinstance(resident, dict) and not recipient_uid:
            recipient_uid = resident.get("uid") or resident.get("userId")

        if snap.exists:
            notif_ref.set(
                {
                    "verificationStatus": "Verified",
                    "status": "Verified",
                    "verifiedAt": firestore.SERVER_TIMESTAMP,
                    "rejectionReason": firestore.DELETE_FIELD,
                    "decidedAt": firestore.SERVER_TIMESTAMP,
                    "decidedBy": decided_by,
                },
                merge=True,
            )
        else:
            return Response({'status': 'not_found'}, status=404)

        title = "Resident Verification"
        body = "Your account has been verified."
        result_notification_id = f"resident_verification_result_{doc_id}"
        create_firestore_notification(
            title=title,
            body=body,
            target="resident",
            disruption_type="resident_verification_result",
            doc_id=result_notification_id,
            extra_data={
                "kind": "resident_verification",
                "subtype": "result",
                "requestId": data.get("requestId") or str(doc_id),
                "parentRequestId": str(doc_id),
                "recipientUid": recipient_uid or "",
                "verificationId": str(doc_id),
                "verificationStatus": "Verified",
                "recipient_id": recipient_uid or str(doc_id),
                "recipient": recipient_uid or "",
            },
        )
        try:
            notif_ref.set({"resultNotificationId": result_notification_id}, merge=True)
        except Exception:
            pass
        _update_verification_request_notifications(db, "Verified", result_notification_id, decided_by, request_ids=_verification_doc_values({"requestId": str(doc_id)}, data), recipient_uid=recipient_uid)

        try:
            token = (data.get("data") or {}).get("fcmToken") or (data.get("data") or {}).get("fcm_token") or resident.get("fcmToken") if isinstance(resident, dict) else None
            if token and firebase_manager:
                firebase_manager.send_push_notification(token, title, body, data={"requestId": data.get("requestId") or str(doc_id), "verificationStatus": "Verified"})
        except Exception:
            pass

        return Response({'status': 'ok'})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)


@api_view(['POST'])
def resident_verification_reject(request, doc_id: str):
    if not (request.user and request.user.is_authenticated and request.user.is_staff):
        return Response({'status': 'forbidden'}, status=403)

    if not firestore:
        return Response({'status': 'error', 'message': 'Firestore not available'}, status=500)

    reason = None
    try:
        reason = (request.data or {}).get("reason")
    except Exception:
        reason = None

    try:
        db = firestore.client()
        resident_ref, resident_snap, resident_data, resident_collection, request_ref, request_data = _find_resident_verification_record(db, doc_id)
        decided_by = request.user.email or request.user.username or ""

        if resident_ref is not None and resident_snap is not None and resident_snap.exists:
            recipient_uid = resident_data.get("uid") or resident_data.get("userId") or str(doc_id)
            request_ids = _verification_doc_values({"requestId": str(doc_id)}, resident_data, request_data)
            primary_request_id = resident_data.get("verificationRequestId") or resident_data.get("requestId") or request_data.get("requestId") or str(doc_id)

            update = {
                "verificationStatus": "Rejected",
                "status": "Rejected",
                "isVerified": False,
                "is_verified": False,
                "rejectedAt": firestore.SERVER_TIMESTAMP,
                "decidedAt": firestore.SERVER_TIMESTAMP,
                "decidedBy": decided_by,
            }
            if reason:
                update["rejectionReason"] = str(reason)
            resident_ref.set(update, merge=True)
            try:
                email = resident_data.get("email") or resident_data.get("gmail_email") or request_data.get("email") or ((request_data.get("data") or {}).get("resident") or {}).get("email")
                if email:
                    Resident.objects.filter(user__email__iexact=str(email)).update(is_verified=False)
            except Exception:
                pass

            title = "Resident Verification"
            body = "Your account verification was rejected."
            if reason:
                body = f"Your account verification was rejected: {reason}"

            result_notification_id = f"resident_verification_result_{primary_request_id}"
            create_firestore_notification(
                title=title,
                body=body,
                target="resident",
                disruption_type="resident_verification_result",
                doc_id=result_notification_id,
                extra_data={
                    "kind": "resident_verification",
                    "subtype": "result",
                    "requestId": str(primary_request_id),
                    "parentRequestId": str(primary_request_id),
                    "recipientUid": str(recipient_uid or ""),
                    "verificationId": str(resident_snap.id),
                    "verificationStatus": "Rejected",
                    "rejectionReason": str(reason) if reason else None,
                    "residentCollection": resident_collection,
                },
            )
            try:
                resident_ref.set({"resultNotificationId": result_notification_id, "verificationResultNotificationId": result_notification_id}, merge=True)
            except Exception:
                pass
            _update_verification_request_notifications(db, "Rejected", result_notification_id, decided_by, request_ids=request_ids, recipient_uid=recipient_uid, reason=reason)

            try:
                token = resident_data.get("fcmToken") or resident_data.get("fcm_token")
                if not token and isinstance(request_data.get("data"), dict):
                    token = request_data["data"].get("fcmToken") or request_data["data"].get("fcm_token")
                if token and firebase_manager:
                    firebase_manager.send_push_notification(token, title, body, data={"requestId": str(primary_request_id), "verificationStatus": "Rejected", "rejectionReason": str(reason) if reason else ""})
            except Exception:
                pass

            return Response({'status': 'ok'})

        notif_ref = request_ref or db.collection("notifications").document(str(doc_id))
        snap = notif_ref.get()
        data = snap.to_dict() or {} if snap.exists else {}
        if not isinstance(data, dict):
            data = {}

        recipient_uid = data.get("recipientUid") or (data.get("data") or {}).get("recipientUid") or (data.get("data") or {}).get("uid") or data.get("uid")
        resident = (data.get("data") or {}).get("resident") or {}
        if isinstance(resident, dict) and not recipient_uid:
            recipient_uid = resident.get("uid") or resident.get("userId")

        if snap.exists:
            update = {
                "verificationStatus": "Rejected",
                "status": "Rejected",
                "rejectedAt": firestore.SERVER_TIMESTAMP,
                "decidedAt": firestore.SERVER_TIMESTAMP,
                "decidedBy": decided_by,
            }
            if reason:
                update["rejectionReason"] = str(reason)
            notif_ref.set(update, merge=True)
        else:
            return Response({'status': 'not_found'}, status=404)

        title = "Resident Verification"
        body = "Your account verification was rejected."
        if reason:
            body = f"Your account verification was rejected: {reason}"

        result_notification_id = f"resident_verification_result_{doc_id}"
        create_firestore_notification(
            title=title,
            body=body,
            target="resident",
            disruption_type="resident_verification_result",
            doc_id=result_notification_id,
            extra_data={
                "kind": "resident_verification",
                "subtype": "result",
                "requestId": data.get("requestId") or str(doc_id),
                "parentRequestId": str(doc_id),
                "recipientUid": recipient_uid or "",
                "verificationId": str(doc_id),
                "verificationStatus": "Rejected",
                "recipient_id": recipient_uid or str(doc_id),
                "recipient": recipient_uid or "",
                "rejectionReason": str(reason) if reason else None,
            },
        )
        try:
            notif_ref.set({"resultNotificationId": result_notification_id}, merge=True)
        except Exception:
            pass
        _update_verification_request_notifications(db, "Rejected", result_notification_id, decided_by, request_ids=_verification_doc_values({"requestId": str(doc_id)}, data), recipient_uid=recipient_uid, reason=reason)

        try:
            token = (data.get("data") or {}).get("fcmToken") or (data.get("data") or {}).get("fcm_token") or resident.get("fcmToken") if isinstance(resident, dict) else None
            if token and firebase_manager:
                firebase_manager.send_push_notification(token, title, body, data={"requestId": data.get("requestId") or str(doc_id), "verificationStatus": "Rejected", "rejectionReason": str(reason) if reason else ""})
        except Exception:
            pass

        return Response({'status': 'ok'})
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)


@api_view(['POST'])
def generate_route_suggestion(request):
    if not (request.user and request.user.is_authenticated and request.user.is_staff):
        return Response({'status': 'forbidden'}, status=403)

    route_id = request.data.get('route_id') or request.query_params.get('route_id')
    date_str = request.data.get('date') or request.query_params.get('date')
    min_level = request.data.get('min_level') or request.query_params.get('min_level')
    start_policy = request.data.get('start_policy') or request.query_params.get('start_policy') or 'rotate'

    try:
        route_id_int = int(route_id)
    except Exception:
        return Response({'status': 'error', 'message': 'route_id is required'}, status=400)

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.localdate()
    except Exception:
        target_date = timezone.localdate()

    predictor = GarbageRoutePredictor()
    result = predictor.optimize_route_by_garbage_level(
        route_id_int,
        explain=True,
        mirror=True,
        optimization_date=target_date,
        min_level=min_level,
        start_policy=start_policy,
    )
    return Response({'status': 'ok', 'doc_id': f"{route_id_int}_{target_date.strftime('%Y%m%d')}", 'result': result})

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
