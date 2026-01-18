import firebase_admin
from firebase_admin import credentials, firestore, auth, messaging
import os
from .firebase_config import FIREBASE_CONFIG, FIREBASE_SERVICE_ACCOUNT_KEY_PATH

class FirebaseManager:
    """
    Manages Firebase integration for the G-Track application.
    Handles authentication, Firestore database operations, and real-time updates.
    """
    
    def __init__(self):
        self.app = None
        self.db = None
        self.initialize_firebase()
    
    def initialize_firebase(self):
        """Initialize Firebase Admin SDK with service account credentials."""
        try:
            # Check if Firebase app is already initialized
            if not firebase_admin._apps:
                # Initialize with service account if file exists
                if os.path.exists(FIREBASE_SERVICE_ACCOUNT_KEY_PATH):
                    cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_KEY_PATH)
                    self.app = firebase_admin.initialize_app(cred)
                else:
                    # Initialize with default credentials (for development)
                    self.app = firebase_admin.initialize_app()
                
                # Get Firestore database instance
                self.db = firestore.client()
                print("Firebase initialized successfully")
            else:
                # Get existing app instance
                self.app = firebase_admin.get_app()
                self.db = firestore.client()
                print("Using existing Firebase app")
        except Exception as e:
            print(f"Error initializing Firebase: {e}")
    
    def get_user_by_email(self, email):
        """Get a user by email address."""
        try:
            return auth.get_user_by_email(email)
        except auth.UserNotFoundError:
            return None
        except Exception as e:
            print(f"Error getting user by email: {e}")
            return None
    
    def create_user(self, email, password, display_name=None):
        """Create a new Firebase user."""
        try:
            user_properties = {
                'email': email,
                'password': password,
                'email_verified': False,
            }
            if display_name:
                user_properties['display_name'] = display_name
            
            return auth.create_user(**user_properties)
        except Exception as e:
            print(f"Error creating user: {e}")
            return None
    
    def update_user(self, uid, properties):
        """Update a Firebase user's properties."""
        try:
            return auth.update_user(uid, **properties)
        except Exception as e:
            print(f"Error updating user: {e}")
            return None
    
    def delete_user(self, uid):
        """Delete a Firebase user."""
        try:
            auth.delete_user(uid)
            return True
        except Exception as e:
            print(f"Error deleting user: {e}")
            return False
    
    def save_route_data(self, route_id, data):
        """Save route data to Firestore."""
        try:
            route_ref = self.db.collection('routes').document(str(route_id))
            route_ref.set(data, merge=True)
            return True
        except Exception as e:
            print(f"Error saving route data: {e}")
            return False
    
    def get_route_data(self, route_id):
        """Get route data from Firestore."""
        try:
            route_ref = self.db.collection('routes').document(str(route_id))
            doc = route_ref.get()
            if doc.exists:
                return doc.to_dict()
            return None
        except Exception as e:
            print(f"Error getting route data: {e}")
            return None
    
    def save_prediction(self, prediction_id, data):
        """Save AI prediction data to Firestore."""
        try:
            prediction_ref = self.db.collection('predictions').document(str(prediction_id))
            prediction_ref.set(data, merge=True)
            return True
        except Exception as e:
            print(f"Error saving prediction data: {e}")
            return False
    
    def get_predictions_for_date(self, date_str):
        """Get all predictions for a specific date."""
        try:
            predictions = []
            query = self.db.collection('predictions').where('date', '==', date_str)
            docs = query.stream()
            for doc in docs:
                predictions.append(doc.to_dict())
            return predictions
        except Exception as e:
            print(f"Error getting predictions: {e}")
            return []
    
    def save_notification(self, user_id, notification_data):
        """Save a notification to Firestore."""
        try:
            notification_ref = self.db.collection('users').document(str(user_id)).collection('notifications').document()
            notification_data['created_at'] = firestore.SERVER_TIMESTAMP
            notification_data['is_read'] = False
            notification_ref.set(notification_data)
            return notification_ref.id
        except Exception as e:
            print(f"Error saving notification: {e}")
            return None

    def send_push_notification(self, token, title, body, data=None):
        """Send a push notification via Firebase Cloud Messaging."""
        if not token:
            return False
        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=token,
                data=data or {}
            )
            response = messaging.send(message)
            return True
        except Exception as e:
            print(f"Error sending push notification: {e}")
            return False
    
    def get_user_notifications(self, user_id, limit=20):
        """Get notifications for a specific user."""
        try:
            notifications = []
            query = self.db.collection('users').document(str(user_id)).collection('notifications') \
                .order_by('created_at', direction=firestore.Query.DESCENDING).limit(limit)
            docs = query.stream()
            for doc in docs:
                notification = doc.to_dict()
                notification['id'] = doc.id
                notifications.append(notification)
            return notifications
        except Exception as e:
            print(f"Error getting notifications: {e}")
            return []
    
    def mark_notification_as_read(self, user_id, notification_id):
        """Mark a notification as read."""
        try:
            notification_ref = self.db.collection('users').document(str(user_id)) \
                .collection('notifications').document(notification_id)
            notification_ref.update({'is_read': True})
            return True
        except Exception as e:
            print(f"Error marking notification as read: {e}")
            return False

# Create a singleton instance
firebase_manager = FirebaseManager()