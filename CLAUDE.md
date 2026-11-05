# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

GridSight is a grid operations and resilience analytics tool for the PJM footprint. Core question: where is the grid most likely to be stressed, and which regions would need infrastructure upgrades first if demand grows? Early-stage; there are no tests, linter config, or packaging yet.

## Setup and commands

Python virtualenv in `.venv/`; dependencies are pinned in `requirements.txt`.

    source .venv/bin/activate
    pip install -r requirements.txt

The EIA scripts need `EIA_API_KEY` in a `.env` file at the repo root (loaded via `python-dotenv`).

**All scripts use paths relative to the repo root — run them from there.**

    python src/ingest/eia_zones.py         # list PJM sub-BA zones returned by EIA (exploration only, writes nothing)
    python src/ingest/eia_pull.py          # PJM-wide hourly demand -> data/raw/pjm_demand_sample.json
    python src/ingest/eia_zone_demand.py   # per-zone hourly demand -> data/raw/... and data/processed/zone_demand.csv
    python src/models/load_duckdb.py       # zone_demand.csv -> data/gridsight.duckdb (table zone_demand) + sanity summaries
    streamlit run src/app/app.py           # map app

## Architecture / data flow

1. **Ingest (`src/ingest/`)** — standalone top-level scripts (no functions, no shared module) that call the EIA v2 API (`api.eia.gov/v2/electricity/rto/...`). `region-data` gives PJM-wide demand; `region-sub-ba-data` with `facets[parent][]=PJM` gives per-zone demand (zone code is the `subba` field). Date ranges are hard-coded (`start`/`end` params) and requests use `length: 5000` with no pagination, so longer ranges will silently truncate. Raw API responses are saved untouched to `data/raw/`; cleaned tables go to `data/processed/`.
2. **Processed schema** — `data/processed/zone_demand.csv`: `period` (EIA hour string, `YYYY-MM-DDTHH`, UTC), `zone` (EIA sub-BA code), `demand_mw`.
3. **Models (`src/models/`)** — `load_duckdb.py` rebuilds the `zone_demand` table in `data/gridsight.duckdb`, parsing `period` into a `hour_utc` timestamp.
4. **App (`src/app/app.py`)** — Streamlit + Plotly `choropleth_map`. Reads the `zone_demand` table from `data/gridsight.duckdb` (opened read-only), joins it to `reference/pjm_zones.csv` on `eia_code` = `zone`, and draws zones from `reference/pjm_zones.geojson` (feature `id` = EIA code). Zones are coloured by demand as a % of each zone's own peak in the data. Date and hour controls are in US Eastern time (converted to UTC for queries); known EIA gap hours and the nonexistent spring-forward hour show a message instead of a map.

`reference/pjm_zones.csv` is hand-maintained and committed: one row per PJM zone (`eia_code`, `name`, `lat`, `lon` — approximate centroids). New zones from EIA must be added here to appear on the map.

`reference/pjm_zones.geojson` holds approximate zone outlines extracted from PJM's zone map PDF (`pjm.com/-/media/DotCom/about-pjm/pjm-zones.pdf`, map dated 2023-05-11), lined up to real coordinates using state-border tripoints (about 1–2 km typical error, up to ~5 km). OVEC is in the PDF but has no EIA data, so it's not included. Rebuild it with `python src/ingest/pjm_zone_shapes.py path/to/pjm-zones.pdf`; that script needs `pymupdf`, `shapely`, `pyproj` and `numpy`, which are deliberately not in `requirements.txt` (see the script header). Its state-shape numbers and zone colours are tied to that PDF version.

`data/` is gitignored, so on a fresh clone the ingest script must be run before the loader or the app will work.
