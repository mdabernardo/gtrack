# D:\Angelo\gtrack\gtrack\firebase_setup.py

import firebase_admin
from firebase_admin import credentials
from django.conf import settings

# Path to your Firebase service account key
FIREBASE_CREDENTIALS_PATH = settings.FIREBASE_CREDENTIALS_PATH

# Check if the app is already initialized to prevent re-initialization on hot-reloads
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
    firebase_admin.initialize_app(cred)
    print("Firebase Admin SDK initialized successfully.")