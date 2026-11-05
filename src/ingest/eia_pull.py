import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("EIA_API_KEY")

URL = "https://api.eia.gov/v2/electricity/rto/region-data/data/"

params = {
    "api_key": API_KEY,
    "frequency": "hourly",
    "data[0]": "value",
    "facets[respondent][]": "PJM",
    "facets[type][]": "D",
    "start": "2026-10-01T00",
    "end": "2026-10-08T00",
    "sort[0][column]": "period",
    "sort[0][direction]": "asc",
    "length": 5000,
}

response = requests.get(URL, params=params, timeout=30)
response.raise_for_status()
payload = response.json()

rows = payload["response"]["data"]
print(f"Got {len(rows)} rows")
print(rows[0])

with open("data/raw/pjm_demand_sample.json", "w") as f:
    json.dump(payload, f)