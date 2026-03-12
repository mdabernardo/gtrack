import os
import django
import firebase_admin
from firebase_admin import credentials, firestore
from django.conf import settings
from datetime import date
import sys

# Redirect stdout/stderr to file
sys.stdout = open("verification_result.txt", "w")
sys.stderr = sys.stdout

print("Script started.")

try:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gtrack.settings")
    django.setup()

    # Init Firebase if needed
    if not firebase_admin._apps:
        cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', None)
        # Fallback to local file if not in settings
        if not cred_path:
            default_path = os.path.join(settings.BASE_DIR, 'gtrack-50116-firebase-adminsdk-fbsvc-c86675034a.json')
            if os.path.exists(default_path):
                cred_path = default_path

        if cred_path:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        else:
            firebase_admin.initialize_app()

    from gtrack.firebase_sync import sync_optimization_to_firestore

    # Dummy data
    route_id = 999
    route_name = "Test Route Reroutr"
    today = date.today()
    points = [{'location_name': 'Test Loc', 'order': 1, 'score': 10}]

    print("Syncing to Firestore...")
    success = sync_optimization_to_firestore(
        route_id=route_id,
        route_name=route_name,
        optimization_date=today,
        suggested_points=points,
        factors={'test': True},
        generated_at="now"
    )

    print(f"Sync success: {success}")

    if success:
        db = firestore.client()
        ymd = today.strftime('%Y%m%d')
        doc_id = f"{route_id}_{ymd}"
        print(f"Checking document: {doc_id}")
        doc = db.collection('reroutr').document(doc_id).get()
        if doc.exists:
            print("Verified! Document found in 'reroutr'.")
            data = doc.to_dict()
            print(f"Data keys: {list(data.keys())}")
            if 'data_string' in data:
                print("Confirmed 'data_string' field exists.")
        else:
            print("Verification failed: Document not found in 'reroutr'")

except Exception as e:
    print(f"CRASH: {e}")
    import traceback
    traceback.print_exc()

print("Script finished.")
