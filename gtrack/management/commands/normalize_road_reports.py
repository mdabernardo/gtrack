from django.core.management.base import BaseCommand

from gtrack.models import Route, RoutePoint
from gtrack.firebase_sync import _get_firestore_client

try:
    from firebase_admin import firestore
except Exception:
    firestore = None


class Command(BaseCommand):
    help = "Normalize Firestore road report docs to use real Main Route location names instead of Unknown/missing location."

    def handle(self, *args, **opts):
        db = _get_firestore_client()
        if db is None:
            self.stdout.write(self.style.WARNING("Firestore client not available."))
            return

        route = Route.objects.filter(name__iexact="Main Route").first() or Route.objects.first()
        target_names = []
        if route:
            pts = RoutePoint.objects.filter(route=route).order_by("order").select_related("location")
            for p in pts:
                nm = (p.location.name or "").strip()
                if nm:
                    target_names.append(nm)
        if not target_names:
            target_names = [
                "Sitio 6 basketball court",
                "Gulayan",
                "SM Hoa",
                "Lucas Compound",
                "Justice",
                "Dumpsite",
            ]

        issue_templates = [
            "Road blocked by parked vehicle.",
            "Construction work causing delay.",
            "Flooded street reported.",
            "Accident reported near the area.",
            "Heavy traffic congestion.",
            "Fallen debris blocking part of the road.",
        ]

        def needs_fix(loc_val: str) -> bool:
            s = (loc_val or "").strip().lower()
            return (not s) or (s in {"unknown", "unknown location", "n/a", "na"})

        updated = 0
        scanned = 0
        name_idx = 0
        batch = db.batch()
        batch_ops = 0

        for col_name in ("road_reports", "road_report"):
            try:
                docs = db.collection(col_name).get()
            except Exception:
                continue
            for doc in docs:
                scanned += 1
                data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                if not isinstance(data, dict):
                    continue
                loc = data.get("location")
                desc = data.get("description")

                patch = {}
                if needs_fix(str(loc or "")):
                    patch["location"] = target_names[name_idx % len(target_names)]
                    name_idx += 1
                if not desc or str(desc).strip().lower().startswith("seeded road report"):
                    patch["description"] = issue_templates[(name_idx - 1) % len(issue_templates)]
                if "timestamp" not in data and firestore:
                    patch["timestamp"] = firestore.SERVER_TIMESTAMP
                if "status" not in data:
                    patch["status"] = "new"

                if not patch:
                    continue

                try:
                    batch.update(db.collection(col_name).document(doc.id), patch)
                    batch_ops += 1
                    updated += 1
                    if batch_ops >= 400:
                        batch.commit()
                        batch = db.batch()
                        batch_ops = 0
                except Exception:
                    continue

        if batch_ops:
            try:
                batch.commit()
            except Exception:
                pass

        self.stdout.write(self.style.SUCCESS(f"Scanned {scanned} docs. Updated {updated} docs."))
