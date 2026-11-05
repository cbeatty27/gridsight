# Backfill hourly PJM zone demand from EIA into DuckDB.
#
# Usage (run from the repo root):
#     python src/ingest/backfill_zone_demand.py 2026-09-01 2026-09-30
#
# Both dates are inclusive: this pulls every hour from 00:00 on the start
# date through 23:00 on the end date (UTC).
#
# Steps:
#   1. Read the start and end dates from the command line.
#   2. Ask EIA for the data one page (5,000 rows) at a time until we have it all.
#   3. Save the raw responses to data/raw/ untouched.
#   4. Check we got exactly as many rows as EIA says exist.
#   5. Clean the rows into (hour_utc, zone, demand_mw).
#   6. Replace this date range in the zone_demand table (no duplicates on re-run).
#   7. Print sanity checks.

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import duckdb
import pandas as pd
import requests
from dotenv import load_dotenv

URL = "https://api.eia.gov/v2/electricity/rto/region-sub-ba-data/data/"
PAGE_SIZE = 5000  # EIA's maximum rows per request
DB_PATH = "data/gridsight.duckdb"


# ---------------------------------------------------------------------------
# 1. Read the start and end dates from the command line
# ---------------------------------------------------------------------------

def parse_date(text):
    # argparse calls this on each date. If the text isn't YYYY-MM-DD,
    # strptime raises ValueError and argparse prints a friendly error.
    return datetime.strptime(text, "%Y-%m-%d")

parser = argparse.ArgumentParser(description="Backfill PJM zone demand from EIA.")
parser.add_argument("start", type=parse_date, help="first day, YYYY-MM-DD")
parser.add_argument("end", type=parse_date, help="last day, YYYY-MM-DD (inclusive)")
args = parser.parse_args()

if args.end < args.start:
    sys.exit("End date is before start date.")

# First hour = midnight on the start date; last hour = 23:00 on the end date.
start_hour = args.start
end_hour = args.end + timedelta(hours=23)

# How many hours that is (the +1 counts both the first and last hour).
expected_hours = int((end_hour - start_hour).total_seconds() // 3600) + 1

start_day = args.start.strftime("%Y-%m-%d")
end_day = args.end.strftime("%Y-%m-%d")
print(f"Backfilling {start_day} to {end_day} ({expected_hours} hours, UTC)")


# ---------------------------------------------------------------------------
# Load the API key. We only ever send it to EIA; we never print it.
# ---------------------------------------------------------------------------

load_dotenv()
API_KEY = os.getenv("EIA_API_KEY")
if not API_KEY:
    sys.exit("EIA_API_KEY is not set. Add it to the .env file in the repo root.")

def hide_key(text):
    # Safety net: blank out the key if it shows up in any message we print.
    return text.replace(API_KEY, "***")


# ---------------------------------------------------------------------------
# 2. Fetch every page from EIA
# ---------------------------------------------------------------------------

def get_page(offset):
    # Ask EIA for one page of rows, starting at row number `offset`.
    params = {
        "api_key": API_KEY,
        "frequency": "hourly",  # "hourly" periods are in UTC
        "data[0]": "value",
        "facets[parent][]": "PJM",
        "start": start_hour.strftime("%Y-%m-%dT%H"),
        "end": end_hour.strftime("%Y-%m-%dT%H"),
        # A fixed sort order matters for paging: without it, EIA could
        # return rows in a different order on each request, and paging
        # could skip some rows or repeat others.
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "sort[1][column]": "subba",
        "sort[1][direction]": "asc",
        "offset": offset,
        "length": PAGE_SIZE,
    }

    # Page number as shown in the "page N" progress lines.
    page_num = len(pages) + 1

    # EIA sometimes times out or returns a 5xx error on one request. Retry the
    # same page, waiting longer each time. Waits before attempts 2-5.
    waits = [2, 4, 8, 16]
    attempt = 1
    while True:
        # requests puts the key in the URL, and its error messages include
        # that URL. So we print only the error type, never str(error).
        try:
            response = requests.get(URL, params=params, timeout=60)
            if response.status_code < 500:
                break  # success, or a 4xx error that retrying won't fix
            reason = f"HTTP {response.status_code}"
        except (requests.Timeout, requests.ConnectionError) as error:
            reason = type(error).__name__
        except requests.RequestException as error:
            sys.exit(f"Could not reach EIA ({type(error).__name__}). Nothing was loaded.")

        if attempt > len(waits):
            sys.exit(
                f"Page {page_num} still failing after {attempt} attempts "
                f"(last error: {reason}). Nothing was loaded."
            )
        wait = waits[attempt - 1]
        attempt += 1
        print(f"  page {page_num}: {reason}, retrying in {wait}s (attempt {attempt} of {len(waits) + 1})")
        time.sleep(wait)

    if response.status_code != 200:
        sys.exit(
            f"EIA returned HTTP {response.status_code}: "
            f"{hide_key(response.text[:500])}\nNothing was loaded."
        )

    payload = response.json()
    if "error" in payload:  # EIA sometimes reports errors inside a 200 response
        sys.exit(f"EIA error: {hide_key(str(payload['error']))}\nNothing was loaded.")
    return payload

pages = []     # every raw response, kept untouched for data/raw/
all_rows = []  # every data row, from all pages combined
offset = 0

while True:
    payload = get_page(offset)
    pages.append(payload)

    # EIA tells us how many rows match in total (it sends it as text).
    total = int(payload["response"]["total"])
    rows = payload["response"]["data"]
    print(f"  page {len(pages)}: got {len(rows)} rows (offset {offset}, total {total})")

    if not rows:           # empty page: nothing left to fetch
        break
    all_rows.extend(rows)
    offset += len(rows)    # next page starts right after the rows we have
    if offset >= total:    # we have everything
        break


# ---------------------------------------------------------------------------
# 3. Save the raw responses untouched, named by date range
# ---------------------------------------------------------------------------

os.makedirs("data/raw", exist_ok=True)
raw_path = f"data/raw/zone_demand_{start_day}_{end_day}.json"
with open(raw_path, "w") as f:
    json.dump({"start": start_day, "end": end_day, "total": total, "pages": pages}, f)
print(f"Saved raw responses to {raw_path}")


# ---------------------------------------------------------------------------
# 4. Check we got exactly as many rows as EIA reports
# ---------------------------------------------------------------------------

if total == 0:
    sys.exit("EIA has no data for this range. Nothing was loaded.")

if len(all_rows) != total:
    sys.exit(
        f"Row count mismatch: fetched {len(all_rows)} but EIA reports {total}. "
        "Nothing was loaded."
    )
print(f"Row count check passed: {len(all_rows)} of {total}")


# ---------------------------------------------------------------------------
# 5. Clean the rows: one row per zone per hour
# ---------------------------------------------------------------------------

df = pd.DataFrame(all_rows)
df = df.rename(columns={"subba": "zone"})
# Text -> number. A blank value becomes NaN, which DuckDB stores as NULL.
df["demand_mw"] = pd.to_numeric(df["value"], errors="coerce")
# "2026-09-01T05" -> a real timestamp, same as load_duckdb.py does
df["hour_utc"] = pd.to_datetime(df["period"], format="%Y-%m-%dT%H")
df = df[["hour_utc", "zone", "demand_mw"]]

# The same zone should never appear twice for the same hour.
dupes = df.duplicated(subset=["hour_utc", "zone"]).sum()
if dupes:
    sys.exit(f"EIA returned {dupes} duplicate zone-hour rows. Nothing was loaded.")


# ---------------------------------------------------------------------------
# 6. Load into DuckDB, replacing this date range
# ---------------------------------------------------------------------------
# To avoid duplicates on a re-run, we delete whatever is already stored for
# these hours, then insert the fresh rows. Both happen in one transaction:
# if anything fails partway, DuckDB undoes the whole thing, so the table is
# never left half-updated.

con = duckdb.connect(DB_PATH)

# Same columns and types that load_duckdb.py created.
con.execute("""
    CREATE TABLE IF NOT EXISTS zone_demand (
        hour_utc  TIMESTAMP,
        zone      VARCHAR,
        demand_mw DOUBLE
    )
""")

# Let SQL refer to the pandas table by the name "new_rows".
con.register("new_rows", df)

con.begin()
try:
    already_there = con.execute(
        "SELECT COUNT(*) FROM zone_demand WHERE hour_utc BETWEEN ? AND ?",
        [start_hour, end_hour],
    ).fetchone()[0]
    con.execute(
        "DELETE FROM zone_demand WHERE hour_utc BETWEEN ? AND ?",
        [start_hour, end_hour],
    )
    con.execute("""
        INSERT INTO zone_demand
        SELECT hour_utc, zone, demand_mw FROM new_rows
    """)
    con.commit()
except Exception:
    con.rollback()
    raise

print(f"Replaced {already_there} existing rows with {len(df)} new rows")


# ---------------------------------------------------------------------------
# 7. Sanity checks (only for the range we just loaded)
# ---------------------------------------------------------------------------

range_params = [start_hour, end_hour]

print("\nRows per zone:")
print(con.execute("""
    SELECT zone,
           COUNT(*)         AS hours,
           COUNT(demand_mw) AS hours_with_value,
           MAX(demand_mw)   AS peak_mw
    FROM zone_demand
    WHERE hour_utc BETWEEN ? AND ?
    GROUP BY zone
    ORDER BY zone
""", range_params).df().to_string(index=False))

distinct_hours = con.execute("""
    SELECT COUNT(DISTINCT hour_utc)
    FROM zone_demand
    WHERE hour_utc BETWEEN ? AND ?
""", range_params).fetchone()[0]
print(f"\nDistinct hours: {distinct_hours} of {expected_hours} expected")

# Build the full list of hours we expect, then keep only the ones with no data.
missing = con.execute("""
    SELECT h.hour_utc
    FROM generate_series(?::TIMESTAMP, ?::TIMESTAMP, INTERVAL 1 HOUR) AS h(hour_utc)
    LEFT JOIN (
        SELECT DISTINCT hour_utc FROM zone_demand WHERE hour_utc BETWEEN ? AND ?
    ) AS d ON h.hour_utc = d.hour_utc
    WHERE d.hour_utc IS NULL
    ORDER BY h.hour_utc
""", range_params + range_params).df()

if missing.empty:
    print("Missing hours: none")
else:
    print(f"Missing hours ({len(missing)}):")
    print(missing.to_string(index=False))

# Zones that have some data but are missing individual hours.
short_zones = con.execute("""
    SELECT zone, COUNT(*) AS hours
    FROM zone_demand
    WHERE hour_utc BETWEEN ? AND ?
    GROUP BY zone
    HAVING COUNT(*) < ?
    ORDER BY zone
""", range_params + [expected_hours]).df()

if short_zones.empty:
    print("Zones missing hours: none")
else:
    print(f"Zones with fewer than {expected_hours} hours:")
    print(short_zones.to_string(index=False))

blank_values = con.execute("""
    SELECT COUNT(*)
    FROM zone_demand
    WHERE hour_utc BETWEEN ? AND ? AND demand_mw IS NULL
""", range_params).fetchone()[0]
print(f"Rows with a blank demand value: {blank_values}")

con.close()
