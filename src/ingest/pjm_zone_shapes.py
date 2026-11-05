# Build approximate PJM zone outlines (reference/pjm_zones.geojson) from
# PJM's zone map PDF.
#
# Usage (run from the repo root):
#     curl -o /tmp/pjm-zones.pdf https://www.pjm.com/-/media/DotCom/about-pjm/pjm-zones.pdf
#     python src/ingest/pjm_zone_shapes.py /tmp/pjm-zones.pdf
#
# Extra libraries, not in requirements.txt because the app doesn't need them
# (tested with these versions):
#     pip install pymupdf==2.1 shapely==2.2.0 pyproj numpy
#
# The PDF is a vector map with no coordinates in it. Steps:
#   1. Read every filled shape from the PDF (page coordinates).
#   2. Find where state borders meet (tripoints) on the page, and pair them
#      with their known latitude/longitude.
#   3. Fit a transform from page coordinates to a conic map projection.
#   4. Pick out each zone by its legend colour, keep only the part that is
#      visible on the map, convert it to lon/lat, and save as GeoJSON.
#
# Written for the map dated 2023-05-11 (the date printed on the PDF). The state
# shape numbers and zone colours below are specific to that version; if PJM
# changes the PDF, check them again.
#
# Typical error is about 1-2 km, up to ~5 km (printed when the script runs).

import json
import sys

import numpy as np
import pymupdf
from pyproj import Transformer
from shapely import transform as shp_transform
from shapely.geometry import Point, Polygon, box, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

OUT_PATH = "reference/pjm_zones.geojson"

# Map area on the page, and the legend box that sits on top of it.
MAP_FRAME = box(23, 24, 590, 634)
LEGEND_BOX = box(122, 399, 582, 626)

# Legend swatch colour -> (EIA sub-BA code, name as printed in the legend).
# OVEC has an empty swatch and no EIA data, so it is left out.
ZONE_COLORS = {
    (0.302, 0.698, 0.392): ("AP", "Allegheny Power Systems"),
    (0.482, 0.69, 0.875): ("AEP", "American Electric Power"),
    (0.965, 0.757, 0.251): ("ATSI", "American Transmission Systems, Inc."),
    (0.725, 0.557, 0.702): ("AE", "Atlantic City Electric Co."),
    (0.792, 0.882, 0.675): ("BC", "Baltimore Gas and Electric Co."),
    (0.667, 0.435, 0.624): ("CE", "Commonwealth Edison Company"),
    (0.851, 0.8, 0.89): ("DAY", "Dayton Power & Light Co."),
    (0.376, 0.584, 0.816): ("DPL", "Delmarva Power & Light Co."),
    (0.533, 0.176, 0.443): ("DOM", "Dominion"),
    (0.949, 0.576, 0.176): ("DEOK", "Duke Energy Ohio and Kentucky Corp."),
    (0.443, 0.098, 0.051): ("DUQ", "Duquesne Light Co."),
    (0.859, 0.024, 0.043): ("EKPC", "East Kentucky Power Cooperative"),
    (0.91, 0.376, 0.11): ("JC", "Jersey Central Power & Light"),
    (0.643, 0.09, 0.051): ("ME", "Met-Ed"),
    (0.098, 0.392, 0.69): ("PL", "PPL Electric Utilities"),
    (0.6, 0.31, 0.533): ("PE", "PECO Energy Co."),
    (0.969, 0.941, 0.341): ("PN", "Pennsylvania Electric Co."),
    (0.792, 0.675, 0.792): ("PEP", "Potomac Electric Power Co."),
    (0.565, 0.784, 0.525): ("PS", "PSEG"),
    (0.259, 0.49, 0.749): ("RECO", "Rockland Electric Co."),
}

# Which PDF drawing (by its order in the file) is which state.
STATE_SHAPES = {
    "IL": 59, "IN": 58, "OH": 33, "KY": 56, "WV": 23, "VA": 24, "PA": 30,
    "MD": 55, "NJ": 46, "NY": 44, "MI": 51, "TN": 27, "NC": 34, "GA": 60,
    "AL": 65, "MS": 49,
}

# Points where three states meet: (latitude, longitude).
TRIPOINTS = {
    ("PA", "OH", "WV"): (40.6386, -80.5194),
    ("PA", "MD", "WV"): (39.7213, -79.4772),
    ("OH", "IN", "MI"): (41.6960, -84.8061),
    ("OH", "IN", "KY"): (39.1043, -84.8203),
    ("IL", "IN", "KY"): (37.7940, -88.0280),
    ("OH", "KY", "WV"): (38.4176, -82.5934),
    ("KY", "VA", "WV"): (37.5402, -81.9677),
    ("KY", "TN", "VA"): (36.6008, -83.6753),
    ("VA", "NC", "TN"): (36.5881, -81.6775),
    ("PA", "NJ", "NY"): (41.3573, -74.6946),
    ("TN", "GA", "NC"): (34.9873, -84.3215),
    ("TN", "AL", "GA"): (34.9853, -85.6052),
    ("TN", "AL", "MS"): (34.9960, -88.2000),
}

# Conic projection centred on PJM. Fits the PDF better than plain lat/lon
# or Web Mercator.
PROJECTION = "+proj=lcc +lat_1=37 +lat_2=41 +lat_0=39 +lon_0=-80 +datum=WGS84"


# ---------------------------------------------------------------------------
# 1. Read every filled shape from the PDF
# ---------------------------------------------------------------------------

def bezier_points(p0, p1, p2, p3, steps=6):
    # Approximate a curve with a few straight segments.
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        points.append((
            u**3 * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t**3 * p3.x,
            u**3 * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t**3 * p3.y,
        ))
    return points


def drawing_rings(drawing):
    # Split a PDF path into closed rings (a new ring starts whenever the pen jumps).
    rings, current, last = [], [], None
    for item in drawing["items"]:
        kind = item[0]
        if kind == "re":
            r = item[1]
            rings.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)])
            last = None
            continue
        if kind == "qu":
            q = item[1]
            rings.append([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)])
            last = None
            continue
        start = item[1]
        if last is None or abs(start.x - last.x) > 1e-3 or abs(start.y - last.y) > 1e-3:
            if len(current) >= 3:
                rings.append(current)
            current = [(start.x, start.y)]
        if kind == "l":
            current.append((item[2].x, item[2].y))
            last = item[2]
        elif kind == "c":
            current += bezier_points(item[1], item[2], item[3], item[4])
            last = item[4]
    if len(current) >= 3:
        rings.append(current)
    return rings


def drawing_geometry(drawing):
    geom = None
    for ring in drawing_rings(drawing):
        g = make_valid(Polygon(ring))
        if g.is_empty:
            continue
        if geom is None:
            geom = g
        elif drawing.get("even_odd"):
            geom = geom.symmetric_difference(g)
        else:
            geom = geom.union(g)
    return geom


if len(sys.argv) != 2:
    sys.exit("Usage: python src/ingest/pjm_zone_shapes.py path/to/pjm-zones.pdf")

page = pymupdf.open(sys.argv[1])[0]
shapes = []  # (drawing number, fill colour, geometry), in drawing order
for number, drawing in enumerate(page.get_drawings()):
    if drawing.get("fill") is None:
        continue
    geom = drawing_geometry(drawing)
    if geom is not None and not geom.is_empty:
        shapes.append((number, tuple(round(x, 3) for x in drawing["fill"]), geom))
print(f"Read {len(shapes)} filled shapes from the PDF")


# ---------------------------------------------------------------------------
# 2. Find the state tripoints on the page
# ---------------------------------------------------------------------------

by_number = {number: geom for number, _, geom in shapes}


def find_tripoint(states):
    # The vertex closest to all three state shapes at once.
    geoms = [by_number[STATE_SHAPES[s]] for s in states]
    best = None
    for g in geoms:
        for poly in getattr(g, "geoms", [g]):
            if poly.geom_type != "Polygon":
                continue
            for x, y in poly.exterior.coords:
                gap = max(h.distance(Point(x, y)) for h in geoms)
                if best is None or gap < best[0]:
                    best = (gap, x, y)
    return best[1], best[2]


names = list(TRIPOINTS)
page_xy = np.array([find_tripoint(k) for k in names])
lat = np.array([TRIPOINTS[k][0] for k in names])
lon = np.array([TRIPOINTS[k][1] for k in names])


# ---------------------------------------------------------------------------
# 3. Fit page coordinates -> projected metres (a straight-line fit)
# ---------------------------------------------------------------------------

proj_x, proj_y = Transformer.from_crs("EPSG:4326", PROJECTION, always_xy=True).transform(lon, lat)
A = np.c_[page_xy, np.ones(len(names))]
fit_x = np.linalg.lstsq(A, proj_x, rcond=None)[0]
fit_y = np.linalg.lstsq(A, proj_y, rcond=None)[0]

error_km = np.hypot(A @ fit_x - proj_x, A @ fit_y - proj_y) / 1000
print(f"Fit error at the tripoints: typical {np.sqrt(np.mean(error_km**2)):.1f} km, "
      f"worst {error_km.max():.1f} km ({'-'.join(names[error_km.argmax()])})")
if error_km.max() > 5:
    sys.exit("Fit is worse than expected. Has the PDF changed? Nothing was written.")

to_lonlat = Transformer.from_crs(PROJECTION, "EPSG:4326", always_xy=True)


def page_to_lonlat(coords):
    x, y = coords[:, 0], coords[:, 1]
    lon_, lat_ = to_lonlat.transform(fit_x[0] * x + fit_x[1] * y + fit_x[2],
                                     fit_y[0] * x + fit_y[1] * y + fit_y[2])
    return np.c_[lon_, lat_]


# ---------------------------------------------------------------------------
# 4. Build each zone's visible outline and save
# ---------------------------------------------------------------------------

map_area = MAP_FRAME.difference(LEGEND_BOX)
zone_shapes = [
    (fill, geom) for _, fill, geom in shapes
    if fill in ZONE_COLORS and geom.area > 1.0
    and geom.intersects(map_area) and not geom.within(LEGEND_BOX)
]

# Shapes drawn later cover earlier ones, so remove later zones from each.
visible = {}
for i, (fill, geom) in enumerate(zone_shapes):
    code = ZONE_COLORS[fill][0]
    later = unary_union([g for f, g in zone_shapes[i + 1:] if ZONE_COLORS[f][0] != code])
    g = make_valid(geom.intersection(map_area))
    if not later.is_empty:
        g = g.difference(later)
    visible[code] = g if code not in visible else visible[code].union(g)

missing = {c for c, _ in ZONE_COLORS.values()} - set(visible)
if missing:
    sys.exit(f"No shape found for {sorted(missing)}. Nothing was written.")

zone_names = dict(ZONE_COLORS.values())
features = []
for code, g in sorted(visible.items()):
    g = g.buffer(0.15).buffer(-0.15)  # close hairline gaps between path pieces
    g = unary_union([p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and p.area > 0.3])
    # Convert to lon/lat, simplify (~300 m), then round to 4 decimals (~10 m).
    g = shp_transform(g, page_to_lonlat)
    g = g.simplify(0.003, preserve_topology=True)
    g = shp_transform(g, lambda c: np.round(c, 4))
    features.append({
        "type": "Feature",
        "id": code,
        "properties": {"eia_code": code, "name": zone_names[code]},
        "geometry": mapping(g),
    })

with open(OUT_PATH, "w") as f:
    json.dump({"type": "FeatureCollection", "features": features}, f)
print(f"Wrote {len(features)} zones to {OUT_PATH}")
