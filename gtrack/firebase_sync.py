# gtrack/firebase_sync.py
from typing import Optional, Sequence, Dict, Any
from datetime import date as date_type
from django.conf import settings

try:
    import firebase_admin
    from firebase_admin import firestore
except Exception:
    firebase_admin = None
    firestore = None


def _get_firestore_client():
    if not firebase_admin or not firestore:
        return None
    try:
        # Ensure Firebase Admin SDK is initialized via settings.py
        if not firebase_admin._apps:
            return None
        return firestore.client()
    except Exception:
        return None


def sync_prediction_to_firestore(
    route_id: int,
    route_name: str,
    prediction_date: date_type,
    predicted_start_time: str,
    predicted_end_time: str,
    confidence_score: float,
    factors: Optional[dict] = None,
) -> bool:
    """
    Mirror AI prediction into Firestore so clients can consume it.
    Writes under:
      artifacts/{projectId}/public/data/predictions/{route_id}_{YYYYMMDD}
    Also maintains index:
      artifacts/{projectId}/public/index/predictions_by_day/{YYYY-MM-DD}/{route_id}
    Time values expected as HH:MM (string) for portability.
    """
    db = _get_firestore_client()
    if db is None:
        return False

    try:
        project_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')
        ymd = prediction_date.strftime('%Y%m%d')
        iso_date = prediction_date.strftime('%Y-%m-%d')
        doc_id = f"{route_id}_{ymd}"

        payload = {
            'routeId': route_id,
            'routeName': route_name,
            'date': iso_date,
            'predictedStart': predicted_start_time,
            'predictedEnd': predicted_end_time,
            'confidence': float(confidence_score),
            'factors': factors or {},
            'updatedAt': firestore.SERVER_TIMESTAMP if firestore else None,
        }

        # Primary data doc
        doc_ref = db.document(
            'artifacts', project_id, 'public', 'data', 'predictions', doc_id
        )
        doc_ref.set(payload, merge=True)

        # Index by day for quick lookup
        index_ref = db.document(
            'artifacts', project_id, 'public', 'index', 'predictions_by_day', iso_date, str(route_id)
        )
        index_ref.set(payload, merge=True)

        return True
    except Exception:
        return False


def sync_predictions_batch_to_firestore(
    items: Sequence[Dict[str, Any]],
    chunk_size: int = 200,
) -> int:
    """
    Batch-write multiple prediction payloads to Firestore efficiently.

    Each item must include keys:
      - route_id: int
      - route_name: str
      - prediction_date: date
      - predicted_start_time: str (HH:MM)
      - predicted_end_time: str (HH:MM)
      - confidence_score: float
      - factors: dict (optional)

    Uses batched writes (two ops per prediction: data + index).
    Firestore limits batches to 500 ops; with two ops per item, chunk_size<=250.
    Returns the number of items successfully scheduled for commit.
    """
    db = _get_firestore_client()
    if db is None:
        return 0

    project_id = settings.FIREBASE_CLIENT_CONFIG.get('projectId', 'g-trackapp')

    # Ensure chunk_size stays within Firestore limits (2 ops per item)
    chunk_size = max(1, min(chunk_size, 250))

    committed_items = 0
    try:
        # Process in chunks
        batch = None
        for i, item in enumerate(items):
            if i % chunk_size == 0:
                # Commit previous batch if exists
                if batch is not None:
                    try:
                        batch.commit()
                    except Exception:
                        pass
                batch = db.batch()

            route_id = item['route_id']
            route_name = item['route_name']
            prediction_date = item['prediction_date']
            predicted_start_time = item['predicted_start_time']
            predicted_end_time = item['predicted_end_time']
            confidence_score = float(item.get('confidence_score', 0.0))
            factors = item.get('factors') or {}

            ymd = prediction_date.strftime('%Y%m%d')
            iso_date = prediction_date.strftime('%Y-%m-%d')
            doc_id = f"{route_id}_{ymd}"

            payload = {
                'routeId': route_id,
                'routeName': route_name,
                'date': iso_date,
                'predictedStart': predicted_start_time,
                'predictedEnd': predicted_end_time,
                'confidence': confidence_score,
                'factors': factors,
                'updatedAt': firestore.SERVER_TIMESTAMP if firestore else None,
            }

            # Primary data doc
            doc_ref = db.document(
                'artifacts', project_id, 'public', 'data', 'predictions', doc_id
            )
            batch.set(doc_ref, payload, merge=True)

            # Index by day for quick lookup
            index_ref = db.document(
                'artifacts', project_id, 'public', 'index', 'predictions_by_day', iso_date, str(route_id)
            )
            batch.set(index_ref, payload, merge=True)

            committed_items += 1

        # Final commit
        if batch is not None:
            try:
                batch.commit()
            except Exception:
                pass

    except Exception:
        # Return what we scheduled in batch even if commit fails
        pass

    return committed_items


def sync_optimization_to_firestore(
    route_id: int,
    route_name: str,
    optimization_date: date_type,
    suggested_points: Sequence[Dict[str, Any]],
    factors: Optional[dict] = None,
    generated_at: Optional[str] = None,
) -> bool:
    """
    Mirror route optimization result into Firestore so clients can consume it.
    Writes under:
      artifacts/{projectId}/public/data/optimizations/{route_id}_{YYYYMMDD}
    Also maintains index:
      artifacts/{projectId}/public/index/optimizations_by_day/{YYYY-MM-DD}/{route_id}

    suggested_points: list of dicts with fields like
      { point_id, location_id, location_name, latitude, longitude, original_order, score, order }
    """
    db = _get_firestore_client()
    if db is None:
        return False

    try:
        ymd = optimization_date.strftime('%Y%m%d')
        iso_date = optimization_date.strftime('%Y-%m-%d')
        doc_id = f"{route_id}_{ymd}"

        payload = {
            'route_id': route_id,
            'route_name': route_name,
            'date': iso_date,
            'suggested_points': list(suggested_points),
            'factors': factors or {},
            'generated_at': generated_at,
            'updated_at': firestore.SERVER_TIMESTAMP if firestore else None,
        }

        doc_ref = db.collection('route_suggestion').document(doc_id)
        doc_ref.set(payload, merge=True)

        return True
    except Exception:
        return False


def sync_scheduling_assistance_to_firestore(
    route_id: int,
    route_name: str,
    assistance_date: date_type,
    predicted_start_time: str,
    predicted_end_time: str,
    confidence_score: float,
    factors: Optional[dict] = None,
) -> bool:
    """
    Mirror scheduling assistance into Firestore as a separate artifact.
    Writes under:
      artifacts/{projectId}/public/data/scheduling_assistance/{route_id}_{YYYYMMDD}
    Also maintains index:
      artifacts/{projectId}/public/index/scheduling_by_day/{YYYY-MM-DD}/{route_id}
    """
    db = _get_firestore_client()
    if db is None:
        return False

    try:
        ymd = assistance_date.strftime('%Y%m%d')
        iso_date = assistance_date.strftime('%Y-%m-%d')
        doc_id = f"{route_id}_{ymd}"

        payload = {
            'route_id': route_id,
            'route_name': route_name,
            'date': iso_date,
            'predicted_start': predicted_start_time,
            'predicted_end': predicted_end_time,
            'confidence': float(confidence_score),
            'factors': factors or {},
            'updated_at': firestore.SERVER_TIMESTAMP if firestore else None,
        }

        doc_ref = db.collection('scheduling_assistance').document(doc_id)
        doc_ref.set(payload, merge=True)

        return True
    except Exception:
        return False


def fetch_garbagelevel_items() -> list:
    """Fetch garbage level items from Firestore collection 'garbagelevel'.
    Returns list of dicts: {id, location, garbageLevel, latitude, longitude, ...}
    """
    db = _get_firestore_client()
    if db is None:
        return []
    try:
        col = db.collection('garbagelevel')
        # For Admin SDK: .get() returns a list of DocumentSnapshot
        docs = col.get()
        items = []
        for d in docs:
            data = d.to_dict() if hasattr(d, 'to_dict') else d._data  # fallback
            if not isinstance(data, dict):
                continue
            data['id'] = d.id
            items.append(data)
        return items
    except Exception:
        return []
