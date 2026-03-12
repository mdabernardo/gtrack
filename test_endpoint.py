import urllib.request
import json

try:
    with urllib.request.urlopen('http://127.0.0.1:8000/api/check-road-reports/') as response:
        print(f"Status Code: {response.getcode()}")
        data = json.loads(response.read().decode())
        print(f"Response: {data}")
except Exception as e:
    print(f"Error: {e}")
