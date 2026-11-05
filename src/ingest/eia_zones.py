import os
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("EIA_API_KEY")

URL = "https://api.eia.gov/v2/electricity/rto/region-sub-ba-data/data/"

params = {
    "api_key": API_KEY,
    "frequency": "hourly",
    "data[0]": "value",
    "facets[parent][]": "PJM",
    "start": "2026-10-01T00",
    "end": "2026-10-01T03",
    "length": 5000,
}

response = requests.get(URL, params=params, timeout=30)
response.raise_for_status()
rows = response.json()["response"]["data"]

print(f"Got {len(rows)} rows")
zones = sorted({(r["subba"], r.get("subba-name")) for r in rows})
print(f"Found {len(zones)} zones:")
for code, name in zones:
    print(code, "-", name)