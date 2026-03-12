import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gtrack.settings")
django.setup()

from django.db.models import Q

from gtrack.models import Route
from gtrack.ai_predictor import GarbageRoutePredictor
from gtrack.firebase import firebase_manager  # noqa: F401
from gtrack.firebase_sync import (
    fetch_road_reports,
    mark_road_report_processed,
    create_firestore_notification,
    _get_firestore_client,
)


def find_or_create_catmon_reports():
    reports = fetch_road_reports(only_new=True) or []
    catmon_reports = []
    for r in reports:
        loc = str(r.get("location") or "").lower()
        if "catmon" in loc or "malabon" in loc:
            catmon_reports.append(r)
    if catmon_reports:
        return catmon_reports

    db = _get_firestore_client()
    if db is None:
        return []

    data = {
        "location": "Catmon, Malabon",
        "description": "Sample obstruction near Catmon, Malabon for reroute testing.",
        "status": "new",
    }
    doc_ref = db.collection("road_reports").document()
    doc_ref.set(data)
    data["id"] = doc_ref.id
    data["__collection__"] = "road_reports"
    return [data]


def seed_catmon_reroute_and_notifications():
    reports = find_or_create_catmon_reports()
    if not reports:
        print("No Catmon/Malabon road_reports available and unable to create one.")
        return

    processed = 0
    for report in reports:
        loc_name = report.get("location", "Unknown Location")
        desc = report.get("description", "Road issue reported")
        report_id = report.get("id")
        report_collection = report.get("__collection__")

        title = f"Road Alert: {loc_name}"
        body = f"Issue reported at {loc_name}: {desc}. Rerouting in progress."

        create_firestore_notification(
            title=title,
            body=body,
            target="collectors",
            route_id=None,
            disruption_type="road_report",
            location_name=loc_name,
        )

        if report_id:
            mark_road_report_processed(report_id, report_collection)
        processed += 1

    print(f"Processed {processed} Catmon/Malabon road report(s).")

    ai_predictor = GarbageRoutePredictor()

    routes = Route.objects.filter(
        Q(points__location__name__icontains="Catmon")
        | Q(points__location__name__icontains="Malabon")
    ).distinct()

    if not routes.exists():
        routes = Route.objects.all()

    rerouted_names = []
    for route in routes:
        try:
            result = ai_predictor.optimize_route_by_garbage_level(route.id)
            rerouted_names.append(result.get("route_name") or route.name)

            title = f"Route Rerouted: {route.name}"
            body = f"New path generated due to road report near Catmon/Malabon. Check map."
            create_firestore_notification(
                title=title,
                body=body,
                target="collectors",
                route_id=route.id,
                disruption_type="reroute",
                location_name="Catmon, Malabon",
            )
        except Exception as e:
            print(f"Error optimizing route {route.id}: {e}")

    print("Rerouted routes:", rerouted_names)


if __name__ == "__main__":
    seed_catmon_reroute_and_notifications()

