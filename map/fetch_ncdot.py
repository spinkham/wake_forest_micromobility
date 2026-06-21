#!/usr/bin/env python3
"""Fetch NCDOT posted speed limits for the Wake Forest area -> ncdot_speed.geojson.

NCDOT_SpeedLimitQtr MapServer, layers 0 (State Maintained), 1 (Primary Routes),
2 (Interstate). Authoritative posted speeds (mph) on state-maintained roads.
Polite: one bbox query per layer (paginated), single-threaded, cached to disk.
"""
import json
import requests

BASE = "https://gis11.services.ncdot.gov/arcgis/rest/services/NCDOT_SpeedLimitQtr/MapServer"
BBOX = "-78.60,35.87,-78.43,36.04"  # a bit larger than town limits + buffer
HEADERS = {"User-Agent": "Wake Forest micromobility civic map; a non-commercial civic project"}
LAYERS = {0: "state", 1: "primary", 2: "interstate"}

feats = []
for lyr, label in LAYERS.items():
    offset = 0
    got = 0
    while True:
        params = {
            "where": "1=1",
            "geometry": BBOX, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "SpeedLimit,RouteClass,RouteNumber", "outSR": "4326",
            "resultOffset": offset, "resultRecordCount": 1000, "f": "geojson",
        }
        r = requests.get(f"{BASE}/{lyr}/query", params=params, headers=HEADERS, timeout=60)
        r.raise_for_status()
        g = r.json()
        fs = g.get("features", [])
        for f in fs:
            f.setdefault("properties", {})["nclayer"] = label
        feats += fs
        got += len(fs)
        if len(fs) < 1000:
            break
        offset += 1000
    print(f"layer {lyr} ({label}): {got} segments")

json.dump({"type": "FeatureCollection", "features": feats}, open("ncdot_speed.geojson", "w"))
print("total NCDOT segments:", len(feats))
# quick speed distribution
from collections import Counter
c = Counter(f["properties"].get("SpeedLimit") for f in feats)
print("speed distribution (mph: count):", dict(sorted((k, v) for k, v in c.items() if k is not None)))
