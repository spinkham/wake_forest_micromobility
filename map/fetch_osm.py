#!/usr/bin/env python3
"""One-time, cached OSM fetch for the Wake Forest micromobility map.

Pulls every `highway=*` way within (a slightly buffered) Wake Forest town
limit in a single Overpass query via osmnx, then writes osm_highways.geojson.
osmnx caches the Overpass response under osm_cache/ so re-runs don't re-hit
the API. Polite by construction: one request, single-threaded.
"""
import geopandas as gpd
import osmnx as ox
from shapely import make_valid
from shapely.ops import unary_union

for k, v in [("use_cache", True), ("cache_folder", "osm_cache"),
             ("requests_timeout", 180), ("overpass_rate_limit", True)]:
    try:
        setattr(ox.settings, k, v)
    except Exception as e:
        print("settings warn", k, e)

tl = gpd.read_file("town_limits.geojson").to_crs(4326)
poly = unary_union([make_valid(g) for g in tl.geometry.values])
poly_b = poly.buffer(0.002)  # ~200 m, to catch roads on the boundary

print("fetching OSM highways within town limits (one Overpass query)...")
feats = ox.features_from_polygon(poly_b, {"highway": True})
print("raw features:", len(feats))

lines = feats[feats.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
cols = ["highway", "name", "maxspeed", "footway", "cycleway",
        "cycleway:left", "cycleway:right", "cycleway:both", "sidewalk",
        "bicycle", "foot", "access", "oneway", "surface", "service"]
keep = [c for c in cols if c in lines.columns]
out = lines[keep + ["geometry"]].reset_index(drop=True)
out.to_file("osm_highways.geojson", driver="GeoJSON")

print("saved osm_highways.geojson lines:", len(out))
print(out["highway"].value_counts().head(25).to_string())
if "maxspeed" in out:
    print("maxspeed tagged on", int(out["maxspeed"].notna().sum()), "lines")
