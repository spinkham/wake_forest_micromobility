#!/usr/bin/env python3
"""Generate the imagery-cache outline: a generous, tile-aligned region.

Why not just use corporate_limits.geojson (what build_imagery_cache.py defaults
to)? Two reasons, both observed on the deployed cache:

1. The legal boundary is ragged and full of unincorporated enclaves, and the
   base-tile rule only builds a tile that TOUCHES the polygon. So a house 33 m
   outside the line gets no tile and the map falls back to a live NC OneMap
   exportImage at maximum zoom -- exactly what happens today in the Holding
   Village neighbourhood (tile 20/295616/411916, 33 m outside the limits; 25% of
   the z20 tiles within 1 km of it are missing). Whether a tile exists should not
   depend on municipal annexation. Fix: take the CONVEX HULL of the limits, so
   the boundary is far from anywhere anyone actually rides.

2. build_overviews() composites a parent only from all 4 children, so any parent
   straddling the edge of the cached region is incomplete. Snapping the region
   out to a tile grid at zoom ALIGN makes every parent from z(max-1) down to
   zALIGN fully covered, so those levels composite offline with no network at
   all. Below zALIGN the fringe is a handful of tiles, which build_overviews
   fetches directly.

   ALIGN is a cost dial, and it is steep -- the snapped rectangle grows to the
   grid of the level you pick:
     z16 ->  224 km2, ~153k z20 tiles, ~3.4 GB   (z19-z16 offline, ~70 fetched)
     z14 ->  335 km2, ~230k z20 tiles, ~5.0 GB
     z12 ->  383 km2, ~263k z20 tiles, ~5.8 GB   (everything offline)
   z16 is the chosen balance: it buys offline overviews for the levels with
   thousands of tiles, and leaves only the ~70-tile tail to fetch.

Writes tile_outline.geojson (EPSG:4326) for
`build_imagery_cache.py --limits tile_outline.geojson`. Run from map/.
"""
import argparse
import json

from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

R = 20037508.342789244


def span(z):
    return 2 * R / 2 ** z


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limits", default="corporate_limits.geojson")
    ap.add_argument("--out", default="tile_outline.geojson")
    ap.add_argument("--align-zoom", type=int, default=16,
                    help="snap the region out to this zoom's tile grid (lower = "
                         "more levels composite offline, but much more area)")
    a = ap.parse_args()

    gj = json.load(open(a.limits))
    geoms = [shape(f["geometry"]) for f in gj["features"]] if "features" in gj else [shape(gj["geometry"])]
    to3857 = Transformer.from_crs(4326, 3857, always_xy=True).transform
    to4326 = Transformer.from_crs(3857, 4326, always_xy=True).transform

    limits = shp_transform(to3857, unary_union(geoms))
    hull = limits.convex_hull

    t = span(a.align_zoom)
    b = hull.bounds
    x0 = int((b[0] + R) // t)
    x1 = int((b[2] + R) // t)
    y0 = int((R - b[3]) // t)
    y1 = int((R - b[1]) // t)
    rect = box(-R + x0 * t, R - (y1 + 1) * t, -R + (x1 + 1) * t, R - y0 * t)

    out = shp_transform(to4326, rect)
    with open(a.out, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {
                "note": f"convex hull of {a.limits}, snapped out to the z{a.align_zoom} tile grid",
                "align_zoom": a.align_zoom},
             "geometry": mapping(out)}]}, fh)

    print(f"limits      {limits.area/1e6:8.1f} km2")
    print(f"convex hull {hull.area/1e6:8.1f} km2")
    print(f"z{a.align_zoom}-aligned  {rect.area/1e6:8.1f} km2  "
          f"({x1-x0+1} x {y1-y0+1} z{a.align_zoom} tiles)")
    print(f"saved {a.out}")


if __name__ == "__main__":
    main()
