import os
import firebase_admin
from firebase_admin import credentials, messaging
from django.conf import settings

class FirebaseManager:
    def __init__(self):
        self.initialized = False
        try:
            self.ensure_initialized()
        except Exception as e:
            print(f"Firebase init error: {e}")

    def ensure_initialized(self):
        if firebase_admin._apps:
            self.initialized = True
            return

        cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', os.environ.get('FIREBASE_CREDENTIALS_PATH'))
        
        if not cred_path:
            # Try to find it in default location if not specified
            default_path = os.path.join(settings.BASE_DIR, 'gtrack-50116-firebase-adminsdk-fbsvc-c86675034a.json')
            if os.path.exists(default_path):
                cred_path = default_path

        if cred_path and os.path.exists(cred_path):
            try:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
                self.initialized = True
                print(f"Firebase Admin SDK initialized with {cred_path}")
            except Exception as e:
                print(f"Failed to initialize Firebase app: {e}")
        else:
            print(f"FIREBASE_CREDENTIALS_PATH not found or invalid: {cred_path}")

    def send_push_notification(self, token, title, body, data=None):
        """Send a push notification via Firebase Cloud Messaging."""
        if not self.initialized:
            self.ensure_initialized()
        
        if not self.initialized:
            print("Cannot send notification: Firebase not initialized.")
            return False

        if not token:
            return False

        try:
            # Ensure data values are strings
            safe_data = {k: str(v) for k, v in (data or {}).items()}
            
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=safe_data,
                token=token,
            )
            response = messaging.send(message)
            # print(f"Successfully sent message: {response}")
            return True
        except Exception as e:
            print(f"Error sending push notification: {e}")
            return False
    
    def send_message(self, token, title, body, data=None):
        """Alias for send_push_notification."""
        return self.send_push_notification(token, title, body, data)

# Create a singleton instance
firebase_manager = FirebaseManager()
