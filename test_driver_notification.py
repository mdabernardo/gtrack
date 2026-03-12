
import os
import sys
import traceback

try:
    import django
    from unittest.mock import MagicMock, patch

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gtrack.settings")
    django.setup()

    from django.contrib.auth.models import User
    from gtrack.models import GarbageCollector, Notification, Resident
    from gtrack.views import check_road_reports
    from rest_framework.test import APIRequestFactory

    # Mock firebase_manager and firestore
    with patch('gtrack.views.firebase_manager') as mock_fb_manager, \
         patch('gtrack.views.firestore') as mock_firestore, \
         patch('gtrack.views.fetch_road_reports') as mock_fetch_reports, \
         patch('gtrack.views.mark_road_report_processed') as mock_mark_processed, \
         patch('gtrack.views.ai_predictor') as mock_predictor:

        # Setup mocks
        mock_fetch_reports.return_value = [{
            'id': 'report1',
            'location': 'Test Location',
            'description': 'Test Issue'
        }]
        
        mock_predictor.optimize_route_by_garbage_level.return_value = {'route_name': 'Test Route'}
        
        # Mock Firestore client
        mock_db = MagicMock()
        mock_firestore.client.return_value = mock_db
        
        # Mock Document Snapshot
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {'fcmToken': 'test_driver_token'}
        
        # Mock Collection/Document Reference
        mock_col = MagicMock()
        mock_doc_ref = MagicMock()
        mock_db.collection.return_value = mock_col
        mock_col.document.return_value = mock_doc_ref
        mock_doc_ref.get.return_value = mock_doc

        # Create test data
        # Create Driver
        driver_user, _ = User.objects.get_or_create(username='driver1')
        if not hasattr(driver_user, 'garbagecollector'):
            GarbageCollector.objects.create(user=driver_user, phone_number='1234567890')
        
        # Create Resident (to ensure we don't break existing logic)
        res_user, _ = User.objects.get_or_create(username='resident1')
        if not hasattr(res_user, 'resident'):
            Resident.objects.create(user=res_user, notification_enabled=True, fcm_token='res_token')

        # Clear previous notifications
        Notification.objects.all().delete()

        # Call the function
        with open('debug_notification_result.txt', 'w') as f:
            f.write("Calling check_road_reports...\n")
            factory = APIRequestFactory()
            request = factory.post('/api/check_reports/')
            response = check_road_reports(request)
            
            f.write(f"Response: {response.data}\n")

            # Verify Notifications
            driver_notif = Notification.objects.filter(user=driver_user).first()
            res_notif = Notification.objects.filter(user=res_user).first()
            
            if driver_notif:
                f.write(f"SUCCESS: Driver notification created: {driver_notif.title}\n")
            else:
                f.write("FAILURE: No driver notification created\n")

            if res_notif:
                f.write(f"SUCCESS: Resident notification created: {res_notif.title}\n")
            else:
                f.write("FAILURE: No resident notification created\n")

            # Verify Firestore token fetch for driver
            # We expect db.collection('collectors').document('driver1') to be called
            try:
                mock_col.document.assert_called_with('driver1')
                f.write("SUCCESS: Firestore 'collectors' collection queried for driver token\n")
            except AssertionError as e:
                f.write(f"FAILURE: Firestore collection query mismatch: {e}\n")

            # Verify Push Notification sent
            # We expect 2 calls: one for resident, one for driver
            f.write(f"Push notifications sent: {mock_fb_manager.send_push_notification.call_count}\n")
            
            # Check if send_push_notification was called with driver token
            calls = mock_fb_manager.send_push_notification.call_args_list
            driver_called = any(call.kwargs.get('token') == 'test_driver_token' for call in calls)
            
            if driver_called:
                f.write("SUCCESS: Push notification sent to driver token\n")
            else:
                f.write("FAILURE: Push notification NOT sent to driver token\n")

except Exception:
    with open('debug_notification_result.txt', 'w') as f:
        f.write("CRITICAL ERROR:\n")
        traceback.print_exc(file=f)

