import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="GridSight", layout="wide")
st.title("GridSight: PJM zones")

zones = pd.read_csv("reference/pjm_zones.csv")
demand = pd.read_csv("data/processed/zone_demand.csv")

hours = sorted(demand["period"].unique())
chosen = st.select_slider("Hour (UTC)", options=hours, value=hours[0])

snapshot = demand[demand["period"] == chosen]
data = zones.merge(snapshot, left_on="eia_code", right_on="zone")

st.metric("Total demand across the 20 zones (MW)", f"{data['demand_mw'].sum():,.0f}")

fig = px.scatter_map(
    data,
    lat="lat",
    lon="lon",
    size="demand_mw",
    color="demand_mw",
    color_continuous_scale="YlOrRd",
    range_color=[0, demand["demand_mw"].max()],
    size_max=45,
    hover_name="name",
    hover_data={"demand_mw": True, "lat": False, "lon": False},
    zoom=5,
    center={"lat": 39.5, "lon": -80.0},
    height=650,
)
fig.update_layout(map_style="open-street-map", margin={"l": 0, "r": 0, "t": 0, "b": 0})

st.plotly_chart(fig)