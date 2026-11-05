import os
import json
import pandas as pd
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

# keep the raw response untouched
with open("data/raw/pjm_zone_demand_sample.json", "w") as f:
    json.dump(payload, f)

# make a clean table: one row per zone per hour
df = pd.DataFrame(rows)
df["demand_mw"] = pd.to_numeric(df["value"])   # text -> number
df = df.rename(columns={"subba": "zone"})
df = df[["period", "zone", "demand_mw"]]
df.to_csv("data/processed/zone_demand.csv", index=False)

print(df.head())
print("Highest demand by zone:")
print(df.groupby("zone")["demand_mw"].max().sort_values(ascending=False).head(5))