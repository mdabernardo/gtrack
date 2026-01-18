import os
import firebase_admin
from firebase_admin import credentials

# Get the path to the service account key from the environment variable
firebase_credentials_path = os.environ.get('FIREBASE_CREDENTIALS_PATH')

if firebase_credentials_path:
    # Initialize Firebase Admin SDK
    cred = credentials.Certificate(firebase_credentials_path)
    firebase_admin.initialize_app(cred)
else:
    raise Exception("The FIREBASE_CREDENTIALS_PATH environment variable is not set.")