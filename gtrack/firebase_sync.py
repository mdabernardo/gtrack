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
    min_level: Optional[str] = None,
    start_policy: Optional[str] = None,
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
            'min_level': min_level,
            'start_policy': start_policy,
            'updated_at': firestore.SERVER_TIMESTAMP if firestore else None,
        }

        doc_ref = db.collection('route_suggestion').document(doc_id)
        doc_ref.set(payload, merge=True)

        return True
    except Exception:
        return False


def sync_reroute_to_firestore(
    reroute_id: str,
    route_id: int,
    route_name: str,
    reroute_date: date_type,
    suggested_points: Sequence[Dict[str, Any]],
    factors: Optional[dict] = None,
    generated_at: Optional[str] = None,
    road_report: Optional[Dict[str, Any]] = None,
    trace: Optional[Dict[str, Any]] = None,
) -> bool:
    db = _get_firestore_client()
    if db is None:
        return False

    try:
        iso_date = reroute_date.strftime("%Y-%m-%d")
        doc_id = str(reroute_id or "").strip()
        doc_id = doc_id.replace("/", "_") if doc_id else ""
        if not doc_id:
            return False

        payload: Dict[str, Any] = {
            "id": doc_id,
            "route_id": route_id,
            "routeId": str(route_id),
            "route_name": route_name,
            "routeName": route_name,
            "date": iso_date,
            "suggested_points": list(suggested_points),
            "suggestedPoints": list(suggested_points),
            "factors": factors or {},
            "generated_at": generated_at,
            "generatedAt": generated_at,
            "status": "approved",
            "updated_at": firestore.SERVER_TIMESTAMP if firestore else None,
            "updatedAt": firestore.SERVER_TIMESTAMP if firestore else None,
        }

        if road_report:
            payload["road_report"] = dict(road_report)
        if trace:
            payload["trace"] = dict(trace)

        db.collection("reroutr").document(doc_id).set(payload, merge=True)
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
    etas: Optional[Sequence[Dict[str, Any]]] = None,
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
            'etas': list(etas) if etas else [],
            'updated_at': firestore.SERVER_TIMESTAMP if firestore else None,
        }

        doc_ref = db.collection('scheduling_assistance').document(doc_id)
        doc_ref.set(payload, merge=True)

        return True
    except Exception:
        return False


def sync_collector_schedule_if_missing(
    route_id: int,
    route_name: str,
    schedule_date: date_type,
    collector_id: str,
    pickup_locations: Optional[Sequence[Dict[str, Any]]] = None,
    status: str = "scheduled",
    task: str = "Garbage Collection",
    start_time: str = "06:00 AM",
    end_time: str = "10:00 PM",
    area: str = "",
) -> bool:
    db = _get_firestore_client()
    if db is None:
        return False

    try:
        collector_id = str(collector_id).strip()
        if collector_id not in ("1", "2"):
            return False

        ymd = schedule_date.strftime("%Y%m%d")
        iso = schedule_date.strftime("%Y-%m-%d")
        day_name = schedule_date.strftime("%A")
        day_index = int(schedule_date.weekday())
        doc_id = f"{int(route_id)}_{ymd}_{collector_id}"

        doc_ref = db.collection("collector_schedules").document(doc_id)
        try:
            snap = doc_ref.get()
            if getattr(snap, "exists", False):
                return True
        except Exception:
            pass

        payload: Dict[str, Any] = {
            "date": iso,
            "dayName": day_name,
            "dayIndex": day_index,
            "day_name": day_name,
            "day_index": day_index,
            "routeId": str(int(route_id)),
            "routeName": str(route_name or "").strip() or str(int(route_id)),
            "route_id": int(route_id),
            "route_name": str(route_name or "").strip() or str(int(route_id)),
            "collectorId": collector_id,
            "collector_id": collector_id,
            "collectorIdInt": int(collector_id),
            "startTime": str(start_time),
            "endTime": str(end_time),
            "status": str(status or "scheduled"),
            "task": str(task or "Garbage Collection"),
            "pickupPlan": {
                "area": str(area or ""),
                "locations": [
                    (lambda x: (x.pop("plannedTime", None), x.pop("time_am", None), x.pop("time_pm", None), x.pop("times", None), x)[-1])(dict(loc))
                    for loc in (list(pickup_locations) if pickup_locations else [])
                ],
                "dominantLocation": "Dumpsite",
            },
            "updatedAt": firestore.SERVER_TIMESTAMP if firestore else None,
            "generated": True,
        }

        doc_ref.set(payload, merge=True)
        return True
    except Exception:
        return False


def fetch_garbagelevel_items(for_date: Optional[date_type] = None) -> list:
    """Fetch garbage level items from Firestore collection 'garbagelevel'.
    Returns list of dicts: {id, location, garbageLevel, latitude, longitude, ...}
    """
    db = _get_firestore_client()
    if db is None:
        return []
    try:
        col = db.collection('garbagelevel')
        docs = None
        if for_date is not None:
            iso = for_date.strftime('%Y-%m-%d')
            try:
                docs = col.where('date', '==', iso).get()
            except Exception:
                docs = None
        if docs is None:
            docs = col.get()
        items = []
        for d in docs:
            data = d.to_dict() if hasattr(d, 'to_dict') else d._data  # fallback
            if not isinstance(data, dict):
                continue
            data['id'] = d.id
            if for_date is not None:
                iso = for_date.strftime('%Y-%m-%d')
                dt = str(data.get('date') or '').strip()
                if dt and dt != iso:
                    continue
            items.append(data)
        return items
    except Exception:
        return []


def fetch_scheduling_assistance_items(
    start_date: Optional[date_type] = None,
    end_date: Optional[date_type] = None,
    route_id: Optional[int] = None,
    limit: int = 2000,
) -> list:
    db = _get_firestore_client()
    if db is None:
        return []
    try:
        col = db.collection('scheduling_assistance')
        docs = col.get()
        items = []
        start_iso = start_date.strftime('%Y-%m-%d') if start_date else None
        end_iso = end_date.strftime('%Y-%m-%d') if end_date else None
        for d in docs:
            data = d.to_dict() if hasattr(d, 'to_dict') else getattr(d, '_data', {}) or {}
            if not isinstance(data, dict):
                continue
            data['id'] = d.id
            dt = str(data.get('date') or '').strip()
            if start_iso and (not dt or dt < start_iso):
                continue
            if end_iso and (not dt or dt > end_iso):
                continue
            rid = data.get('route_id')
            try:
                rid = int(rid) if rid is not None else None
            except Exception:
                rid = None
            if route_id is not None and rid != int(route_id):
                continue
            items.append(data)
            if limit and len(items) >= int(limit):
                break
        return items
    except Exception:
        return []


def fetch_road_reports(only_new: bool = False) -> list:
    """
    Fetch road reports from Firestore.
    Prefers collection 'road_reports' (plural), falls back to 'road_report' (singular).
    
    Args:
        only_new (bool): If True, filters for reports with status='new' (or missing status).
        
    Returns:
        list of dicts: {id, location, description, timestamp, status, __collection__, ...}
    """
    db = _get_firestore_client()
    if db is None:
        return []

    def _collect(col_name: str) -> list:
        try:
            col = db.collection(col_name)
            docs = col.get()
        except Exception:
            return []
        items = []
        for d in docs:
            data = d.to_dict() if hasattr(d, "to_dict") else getattr(d, "_data", {}) or {}
            if not isinstance(data, dict):
                continue
            status = data.get('status', 'new')
            if only_new and status == 'processed':
                continue
            data['id'] = d.id
            data['__collection__'] = col_name
            items.append(data)
        return items

    # Prefer the pluralized collection where real app data is stored
    items = _collect('road_reports')
    if not items:
        # Backwards compatibility with older 'road_report' collection
        items = _collect('road_report')
    return items


def mark_road_report_processed(report_id: str, collection: Optional[str] = None):
    """Mark a road report as processed in Firestore."""
    db = _get_firestore_client()
    if db is None:
        return False
    try:
        targets = [collection] if collection else ['road_reports', 'road_report']
        updated = False
        for col_name in targets:
            if not col_name:
                continue
            try:
                db.collection(col_name).document(report_id).update({
                    'status': 'processed',
                    'processed_at': firestore.SERVER_TIMESTAMP
                })
                updated = True
                # Do not break; try to keep both collections in sync if they both exist
            except Exception:
                continue
        return updated
    except Exception:
        return False


def create_firestore_notification(
    title: str,
    body: str,
    target: str,
    route_id: Optional[int] = None,
    disruption_type: str = "road_report",
    location_name: Optional[str] = None,
    doc_id: Optional[str] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Create a notification document in Firestore 'notifications' collection so
    mobile clients can display it (mirrors existing mobile-driven schema).
    """
    db = _get_firestore_client()
    if db is None:
        return False
    try:
        payload: Dict[str, Any] = {
            "title": title,
            "body": body,
            "target": target,
            "route_id": route_id,
            "isRead": False,
            "read": False,
            "timestamp": firestore.SERVER_TIMESTAMP if firestore else None,
            "data": {
                "disruption_type": disruption_type,
            },
        }
        if location_name:
            payload["data"]["location_name"] = location_name
        if extra_data:
            try:
                data = dict(extra_data)
                for k in ("kind", "subtype", "requestId", "parentRequestId", "resultNotificationId", "recipientUid", "verificationStatus", "status", "source"):
                    if k in data:
                        payload[k] = data.get(k)
                payload["data"].update({k: v for k, v in data.items()})
            except Exception:
                pass
        if doc_id:
            db.collection("notifications").document(str(doc_id)).set(payload, merge=True)
        else:
            db.collection("notifications").add(payload)
        return True
    except Exception:
        return False
