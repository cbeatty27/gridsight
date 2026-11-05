import json
import os
from datetime import datetime

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

DB_PATH = "data/gridsight.duckdb"
ZONES_GEOJSON = "reference/pjm_zones.geojson"
EASTERN = "America/New_York"

st.set_page_config(page_title="GridSight", layout="wide")
st.title("GridSight: PJM zones")

if not os.path.exists(DB_PATH):
    st.error(f"{DB_PATH} not found. Run the backfill script first.")
    st.stop()


def query(sql, params=None):
    # Open read-only for each query, so the app never locks or changes the
    # database. Results are cached below, so this runs rarely.
    with duckdb.connect(DB_PATH, read_only=True) as con:
        return con.execute(sql, params or []).df()


def to_eastern(hour_utc):
    # The database stores naive UTC timestamps.
    return pd.Timestamp(hour_utc).tz_localize("UTC").tz_convert(EASTERN)


@st.cache_data
def load_summary():
    # Everything the controls need: which Eastern dates have data, each
    # zone's highest demand, and the hour of highest total demand.
    hours = query("SELECT DISTINCT hour_utc FROM zone_demand")["hour_utc"]
    dates = sorted(
        hours.dt.tz_localize("UTC").dt.tz_convert(EASTERN).dt.date.unique()
    )
    zone_peaks = query("""
        SELECT zone, MAX(demand_mw) AS peak_mw
        FROM zone_demand
        GROUP BY zone
    """)
    peak = query("""
        SELECT hour_utc, SUM(demand_mw) AS total_mw
        FROM zone_demand
        GROUP BY hour_utc
        ORDER BY total_mw DESC
        LIMIT 1
    """)
    latest = hours.max()
    return dates, zone_peaks, to_eastern(peak["hour_utc"][0]), to_eastern(latest)


@st.cache_data
def load_hour(hour_utc):
    return query(
        "SELECT zone, demand_mw FROM zone_demand WHERE hour_utc = ?",
        [hour_utc],
    )


@st.cache_data
def load_zone_shapes():
    # Approximate zone outlines traced from PJM's zone map.
    with open(ZONES_GEOJSON) as f:
        return json.load(f)


zones = pd.read_csv("reference/pjm_zones.csv")
shapes = load_zone_shapes()
dates, zone_peaks, peak_hour, latest_hour = load_summary()

if not dates:
    st.info("The zone_demand table is empty. Run the backfill script first.")
    st.stop()

# Start on the most recent hour with data.
if "date" not in st.session_state:
    st.session_state["date"] = latest_hour.date()
    st.session_state["hour"] = latest_hour.hour


def jump_to_peak():
    # Runs before the page redraws, so it can set the widgets' values.
    st.session_state["date"] = peak_hour.date()
    st.session_state["hour"] = peak_hour.hour


def hour_label(h):
    return datetime(2000, 1, 1, h).strftime("%-I %p")


col_date, col_hour, col_button = st.columns([1, 2, 1])
with col_date:
    chosen_date = st.date_input(
        "Date (Eastern)", key="date", min_value=dates[0], max_value=dates[-1]
    )
with col_hour:
    chosen_hour = st.select_slider(
        "Hour (Eastern)", options=list(range(24)), key="hour", format_func=hour_label
    )
with col_button:
    st.write("")  # line the button up with the inputs
    st.button("Jump to the hour of highest total demand", on_click=jump_to_peak)

# Turn the chosen Eastern date and hour into the UTC hour stored in the
# database. On the spring daylight-saving day 2 AM doesn't exist; on the fall
# day 1 AM happens twice, and we show the first one (EDT).
local = pd.Timestamp(datetime(chosen_date.year, chosen_date.month, chosen_date.day, chosen_hour))
eastern = local.tz_localize(EASTERN, ambiguous=True, nonexistent="NaT")
if pd.isna(eastern):
    st.info("This hour doesn't exist on this date (clocks jumped forward). Pick another hour.")
    st.stop()

st.subheader(f"{eastern:%A, %B} {eastern.day}, {eastern:%Y} · {hour_label(chosen_hour)} {eastern.tzname()}")

# The date picker can't grey out individual dates, so check here too.
if chosen_date not in set(dates):
    st.info("No data for this date. Pick another date.")
    st.stop()

hour_utc = eastern.tz_convert("UTC").tz_localize(None).to_pydatetime()
snapshot = load_hour(hour_utc)
if snapshot.empty:
    st.info("No data for this hour (EIA has a known gap here). Try another hour.")
    st.stop()

data = zones.merge(snapshot, left_on="eia_code", right_on="zone").merge(zone_peaks, on="zone")
# Colour by how close each zone is to its own highest demand, so small and
# large zones are comparable and colours move as demand rises and falls.
data["pct_of_peak"] = (100 * data["demand_mw"] / data["peak_mw"]).round(1)

st.metric(f"Total demand across the {len(data)} zones (MW)", f"{data['demand_mw'].sum():,.0f}")

fig = px.choropleth_map(
    data,
    geojson=shapes,
    locations="zone",
    featureidkey="id",
    color="pct_of_peak",
    color_continuous_scale="YlOrRd",
    range_color=[30, 100],
    opacity=0.75,
    hover_name="name",
    hover_data={"zone": False, "demand_mw": ":,.0f", "pct_of_peak": True},
    labels={"demand_mw": "Demand (MW)", "pct_of_peak": "% of zone's peak"},
    zoom=5,
    center={"lat": 39.5, "lon": -80.0},
    height=650,
)
fig.update_layout(
    map_style="open-street-map",
    margin={"l": 0, "r": 0, "t": 0, "b": 0},
    coloraxis_colorbar={"title": "% of zone's<br>peak demand"},
)
fig.update_traces(marker_line_width=0.5, marker_line_color="#444")

st.plotly_chart(fig)
st.caption(
    "Colour shows each zone's demand as a percentage of its highest hour in the data. "
    "Zone outlines are approximate, traced from PJM's zone map."
)
