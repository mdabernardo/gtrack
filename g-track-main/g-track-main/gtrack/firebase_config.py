# gtrack/firebase_config.py
from django.conf import settings
import firebase_admin
from firebase_admin import credentials

# Check if Firebase has already been initialized
if not firebase_admin._apps:
    try:
        # Use the path from settings.py for the service account key
        cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        # This will print the error if the credentials file is not found
        print(f"Failed to initialize Firebase Admin SDK: {e}")

# The configuration for the client-side SDK is now here
FIREBASE_CLIENT_CONFIG = settings.FIREBASE_CLIENT_CONFIG