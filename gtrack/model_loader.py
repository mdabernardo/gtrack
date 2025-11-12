import os
from typing import Optional
from django.conf import settings

# Use google-cloud-storage via Firebase Admin dependency
try:
    from google.cloud import storage
except Exception:
    storage = None


def _parse_gcs_uri(uri: str):
    # Format: gs://bucket/path/to/blob
    if not uri.startswith('gs://'):
        return None, None
    no_scheme = uri[5:]
    parts = no_scheme.split('/', 1)
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def ensure_model_available() -> Optional[str]:
    """
    Ensure a local model file is available. If configured for GCS, download it.
    Returns the local filesystem path to the model, or None if unavailable.
    """
    local_path = getattr(settings, 'MODEL_LOCAL_PATH', os.path.join(settings.BASE_DIR, 'models', 'arrival_tf.h5'))
    storage_mode = getattr(settings, 'MODEL_STORAGE', 'local')
    model_uri = getattr(settings, 'MODEL_URI', '')

    # If already present locally, use it
    if os.path.exists(local_path):
        return local_path

    # If configured for GCS, attempt to download
    if storage_mode.lower() == 'gcs' and model_uri and storage is not None:
        bucket_name, blob_path = _parse_gcs_uri(model_uri)
        if not bucket_name or not blob_path:
            return None
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            client = storage.Client()  # uses application default credentials
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            blob.download_to_filename(local_path)
            if os.path.exists(local_path):
                return local_path
        except Exception:
            return None

    # Fallback: no model available
    return None