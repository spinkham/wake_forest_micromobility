"""NCDOT posted-speed join for OSM road edges.

Assign a NCDOT posted speed to a road edge only where the edge runs ALONG a
NCDOT segment -- nearest within `max_dist`, AND the edge's bearing aligns with
the NCDOT segment's local tangent (within `max_angle`). This stops a short
cross-street stub at an intersection from inheriting a *crossing* arterial's
speed (e.g. a 44 ft residential stub touching a 35 mph primary), which would
otherwise create a phantom >25 mph gate and falsely strand the street.
"""
import math
import pandas as pd
import geopandas as gpd
from shapely.geometry import MultiLineString


def _ls(geom):
    return max(geom.geoms, key=lambda s: s.length) if isinstance(geom, MultiLineString) else geom


def _bearing(a, b):
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0


def _edge_bearing(geom):
    c = list(_ls(geom).coords)
    return _bearing(c[0], c[-1])


def _nc_tangent(line, pt):
    g = _ls(line)
    d = g.project(pt)
    a = g.interpolate(max(0.0, d - 12.0))
    b = g.interpolate(min(g.length, d + 12.0))
    return _bearing((a.x, a.y), (b.x, b.y))


def assign_posted(edges, ncdot, road_mask, proj, max_dist=20.0, max_angle=35.0):
    """Return a Series (indexed like `edges`) of NCDOT posted speeds (NaN where
    no along-segment match)."""
    ep = edges.to_crs(proj)
    nc = ncdot.to_crs(proj).reset_index(drop=True)
    mid = ep.loc[road_mask, ["geometry"]].copy()
    mid["geometry"] = mid.geometry.representative_point()
    j = gpd.sjoin_nearest(mid, nc[["SpeedLimit", "geometry"]], how="left",
                          max_distance=max_dist, distance_col="d")
    j = j.sort_values(["d", "SpeedLimit"], ascending=[True, False],
                      kind="stable").groupby(level=0).first()
    out = {}
    for idx, row in j.iterrows():
        ri, sp = row.get("index_right"), row.get("SpeedLimit")
        if ri is None or pd.isna(ri) or pd.isna(sp):
            continue
        diff = abs(_edge_bearing(ep.geometry.loc[idx]) -
                   _nc_tangent(nc.geometry.iloc[int(ri)], mid.geometry.loc[idx]))
        if min(diff, 180.0 - diff) <= max_angle:
            out[idx] = int(sp)
    return pd.Series(out, dtype="float64").reindex(edges.index)
