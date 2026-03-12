import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gtrack.settings")
django.setup()

from rest_framework.test import APIRequestFactory
from gtrack.views import check_road_reports


def main():
    factory = APIRequestFactory()
    request = factory.get("/api/check-road-reports/")
    response = check_road_reports(request)
    print(response.data)


if __name__ == "__main__":
    main()

