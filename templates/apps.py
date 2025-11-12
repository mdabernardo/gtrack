# gtrack/apps.py

from django.apps import AppConfig

class GtrackConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'gtrack'

    def ready(self):
        # This function runs when the Django app is ready
        try:
            # Import your firebase_config module here
            import gtrack.firebase_config
            print("Firebase initialized successfully.")
        except Exception as e:
            print(f"Error initializing Firebase: {e}")