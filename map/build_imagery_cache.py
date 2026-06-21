#!/usr/bin/env python3
"""Fetch NC OneMap 6-inch orthoimagery for the town and tile it to XYZ WebP.

Builds a local slippy-map tile cache (z{MIN}..z{MAX}, 256 px) of the NC OneMap
"Latest" 6-inch natural-color (3-band RGB) orthoimagery, clipped to the Wake
Forest corporate limits, for use as a self-hosted/offline basemap under the
reachability map.

How it works
------------
1. Pull native-resolution blocks in Web Mercator (EPSG:3857) from the
   ImageServer `exportImage` endpoint. Blocks are aligned to the z{MAX} tile
   grid, so the base zoom slices out with no resampling.
2. Slice each block into opaque z{MAX} web tiles, keeping only tiles that
   intersect the town. Tiles are NOT clipped/masked: a tile is either fully
   cached or absent, so the cache-first map layer falls back per-tile to live
   NC OneMap for anything not cached.
3. Build the z{MAX-1}..z{MIN} overview pyramid by averaging down, emitting only
   fully-covered (all-4-children) tiles so the pyramid stays opaque (no extra
   server hits).

Polite by construction: one request per block, exponential-backoff retries,
a configurable delay, and it RESUMES -- a block whose tiles already exist is
skipped without re-fetching. Run --dry-run first to see counts and an estimate.

native NC OneMap GSD = 0.5 ft (6-inch) -> z20 is the native level; z21 only
oversamples. Default caps at z20.

Deps: requests, Pillow (with WebP), numpy, shapely, pyproj, geopandas.
Run from inside map/ (reads corporate_limits.geojson by default).

  python build_imagery_cache.py --dry-run         # plan: tile counts + size
  python build_imagery_cache.py                    # build z12-20 WebP cache
  python build_imagery_cache.py --zoom-max 19 --format jpg   # smaller / shallower

Imagery: NC OneMap / NC Orthoimagery Program (free public data; attribute
"NC OneMap"). Non-commercial civic use. Be gentle with the server.
"""
import argparse
import io
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw
import requests
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import unary_union, transform as shp_transform
from shapely.prepared import prep

# JPEG (lossy) display mosaic vs the 4-band DEFLATE (lossless) "analysis" mosaic.
JPEG_SERVICE = ("https://services.nconemap.gov/secure/rest/services/Imagery/"
                "Orthoimagery_Latest/ImageServer/exportImage")
LOSSLESS_SERVICE = ("https://services.nconemap.gov/secure/rest/services/Imagery/"
                    "Orthoimagery_Latest_Analysis/ImageServer/exportImage")
R = 20037508.342789244          # web-mercator half-extent (m)
TILE = 256
SERVER_MAX_PX = 4100            # ImageServer maxImageHeight cap
# Generic UA -- deliberately no personal contact info (public repo).
UA = "wake-forest-micromobility civic map; non-commercial imagery cache"


def res_at(z):                  # 3857 metres per pixel at zoom z
    return (2 * R) / (TILE * 2 ** z)


def tile_span(z):               # 3857 metres covered by one 256px tile
    return res_at(z) * TILE


def merc_to_tile(z, X, Y):
    t = tile_span(z)
    return int((X + R) // t), int((R - Y) // t)


def block_bounds(z, bx, by, B):
    t = tile_span(z)
    minx = -R + bx * t
    maxy = R - by * t
    return (minx, R - (by + B) * t, -R + (bx + B) * t, maxy)   # minx,miny,maxx,maxy


# ---- town polygon -> per-block alpha mask ----------------------------------
def town_rings(limits_path):
    """Return (prepared_geom, raw_geom, [(exterior, [holes]), ...]) in EPSG:3857.

    Reads the GeoJSON (EPSG:4326) and reprojects with pyproj -- no geopandas/GDAL,
    so the build runs anywhere with just pip wheels (Pillow, numpy, requests,
    shapely, pyproj)."""
    gj = json.load(open(limits_path))
    geoms = [shape(f["geometry"]) for f in gj["features"]] if "features" in gj else [shape(gj["geometry"])]
    tf = Transformer.from_crs(4326, 3857, always_xy=True).transform
    g = shp_transform(tf, unary_union(geoms))
    polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    rings = [(list(p.exterior.coords), [list(h.coords) for h in p.interiors]) for p in polys]
    return prep(g), g, rings


def block_alpha(rings, minx, maxy, px, res):
    """255 inside the town polygon, 0 outside, for a px*px block."""
    m = Image.new("L", (px, px), 0)
    d = ImageDraw.Draw(m)
    to_px = lambda cs: [((X - minx) / res, (maxy - Y) / res) for X, Y in cs]
    for ext, holes in rings:
        d.polygon(to_px(ext), fill=255)
        for h in holes:
            d.polygon(to_px(h), fill=0)
    return np.asarray(m)


# ---- fetch + save -----------------------------------------------------------
def fetch_block(sess, url, bounds, px, sleep, extra=None, retries=4):
    minx, miny, maxx, maxy = bounds
    params = {"bbox": f"{minx},{miny},{maxx},{maxy}", "bboxSR": 3857, "imageSR": 3857,
              "size": f"{px},{px}", "format": "png", "f": "image"}
    if extra:
        params.update(extra)
    err = "?"
    for attempt in range(retries):
        try:
            r = sess.get(url, params=params, timeout=120)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                return np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
            err = f"HTTP {r.status_code} {r.headers.get('content-type')}"
        except requests.RequestException as e:
            err = type(e).__name__
        wait = sleep * (2 ** attempt) + 0.5
        print(f"    retry {attempt + 1}/{retries} ({err}); wait {wait:.1f}s", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"block fetch failed at {bounds}: {err}")


def save_tile(rgb, path, fmt, quality, method):
    # Always opaque (no polygon clip / alpha mask): a tile is either fully cached
    # or absent -> 404 -> the cache-first map layer falls back to live NC OneMap.
    im = Image.fromarray(rgb, "RGB")
    if fmt in ("jpg", "jpeg"):
        im.save(path, "JPEG", quality=quality)
    else:
        im.save(path, "WEBP", quality=quality, method=method)


def tile_path(out, z, x, y, fmt):
    return os.path.join(out, str(z), str(x), f"{y}.{fmt}")


# ---- overview pyramid -------------------------------------------------------
def build_overviews(out, zmin, zmax, fmt, quality, method):
    total = 0
    for z in range(zmax - 1, zmin - 1, -1):
        cz = os.path.join(out, str(z + 1))
        if not os.path.isdir(cz):
            continue
        parents = {}
        for xs in os.listdir(cz):
            for ys in os.listdir(os.path.join(cz, xs)):
                if ys.endswith("." + fmt):
                    cx, cy = int(xs), int(ys.split(".")[0])
                    parents.setdefault((cx // 2, cy // 2), []).append((cx, cy))
        made = 0
        for (px_, py_), kids in parents.items():
            # Only build a fully-covered (all 4 children) overview tile so it is
            # opaque; partial edges are left absent -> 404 -> live NC OneMap
            # fallback. Keeps the pyramid clean (no transparent fringe).
            need = {(2 * px_, 2 * py_), (2 * px_ + 1, 2 * py_),
                    (2 * px_, 2 * py_ + 1), (2 * px_ + 1, 2 * py_ + 1)}
            if set(kids) != need:
                continue
            canvas = Image.new("RGB", (512, 512))
            for cx, cy in kids:
                canvas.paste(Image.open(tile_path(out, z + 1, cx, cy, fmt)).convert("RGB"),
                             ((cx & 1) * 256, (cy & 1) * 256))
            small = np.ascontiguousarray(np.asarray(canvas.resize((256, 256), Image.LANCZOS)))
            os.makedirs(os.path.join(out, str(z), str(px_)), exist_ok=True)
            save_tile(small, tile_path(out, z, px_, py_, fmt), fmt, quality, method)
            total += 1
            made += 1
        print(f"  z{z}: {made} overview tiles")
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limits", default="corporate_limits.geojson")
    ap.add_argument("--out", default="tiles")
    ap.add_argument("--zoom-min", type=int, default=12)
    ap.add_argument("--zoom-max", type=int, default=20, help="20 = native 6-inch; 21 oversamples")
    ap.add_argument("--block-tiles", type=int, default=16, help="block = N*256 px per request (<=4100)")
    ap.add_argument("--format", choices=["webp", "jpg"], default="webp")
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--method", type=int, default=6, help="WebP effort 0-6")
    ap.add_argument("--sleep", type=float, default=0.4, help="seconds between block requests")
    ap.add_argument("--max-blocks", type=int, default=0, help="stop after N blocks (testing)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lossless", action="store_true",
                    help="source from the lossless DEFLATE _Analysis mosaic (RGB via bandIds + "
                         "cubic resampling) instead of the JPEG service; pair with --quality 90")
    a = ap.parse_args()
    B, z = a.block_tiles, a.zoom_max
    if B * 256 > SERVER_MAX_PX:
        sys.exit(f"--block-tiles {B} -> {B*256}px exceeds server cap {SERVER_MAX_PX}")
    if a.lossless:
        service = LOSSLESS_SERVICE
        extra = {"bandIds": "0,1,2", "interpolation": "RSP_CubicConvolution"}
        print("source: Orthoimagery_Latest_Analysis (lossless DEFLATE) -> RGB + cubic resampling")
    else:
        service, extra = JPEG_SERVICE, None

    prepared, geom, rings = town_rings(a.limits)
    minx, miny, maxx, maxy = geom.bounds
    x0, y0 = merc_to_tile(z, minx, maxy)
    x1, y1 = merc_to_tile(z, maxx, miny)
    res = res_at(z)
    blocks = [(bx, by) for bx in range(x0, x1 + 1, B) for by in range(y0, y1 + 1, B)
              if prepared.intersects(box(*block_bounds(z, bx, by, B)))]
    print(f"map area: {(maxx-minx)/1000:.1f} x {(maxy-miny)/1000:.1f} km (3857) | "
          f"z{z} grid {x1-x0+1} x {y1-y0+1} tiles | {len(blocks)} blocks intersect town")

    # base-tile plan (rasterize each block's mask; cheap, no network)
    base = 0
    block_need = {}
    for bx, by in blocks:
        bb = block_bounds(z, bx, by, B)
        al = block_alpha(rings, bb[0], bb[3], B * 256, res)
        need = [(i, j, bx + i, by + j) for j in range(B) for i in range(B)
                if al[j*256:(j+1)*256, i*256:(i+1)*256].max() > 0]
        block_need[(bx, by)] = (bb, al, need)
        base += len(need)
    ov = sum(int(base / 4 ** k) for k in range(1, z - a.zoom_min + 1))
    kb = 14 if a.format == "webp" else 17
    print(f"base z{z} tiles in town: {base:,}  | overview tiles ~{ov:,}  | "
          f"total ~{base+ov:,} ~= {(base+ov)*kb/1e6:.1f} GB @ ~{kb}KB/tile")
    if a.dry_run:
        print("dry run: nothing fetched.")
        return

    sess = requests.Session(); sess.headers["User-Agent"] = UA
    fetched = written = skipped = 0
    t0 = time.time()
    for n, (bx, by) in enumerate(blocks, 1):
        if a.max_blocks and n > a.max_blocks:
            break
        bb, al, need = block_need[(bx, by)]
        todo = [t for t in need if not os.path.exists(tile_path(a.out, z, t[2], t[3], a.format))]
        if not todo:
            skipped += 1
            continue
        rgb = fetch_block(sess, service, bb, B * 256, a.sleep, extra); fetched += 1
        for i, j, tx, ty in todo:
            sub_rgb = np.ascontiguousarray(rgb[j*256:(j+1)*256, i*256:(i+1)*256, :])
            os.makedirs(os.path.join(a.out, str(z), str(tx)), exist_ok=True)
            save_tile(sub_rgb, tile_path(a.out, z, tx, ty, a.format), a.format, a.quality, a.method)
            written += 1
        if n % 10 == 0 or n == len(blocks):
            print(f"  block {n}/{len(blocks)} | fetched {fetched} skipped {skipped} | "
                  f"{written:,} base tiles | {time.time()-t0:.0f}s")
        time.sleep(a.sleep)

    print(f"base done: {written:,} z{z} tiles ({fetched} blocks fetched, {skipped} already cached)")
    print("building overviews...")
    nov = build_overviews(a.out, a.zoom_min, z, a.format, a.quality, a.method)
    print(f"done: {written:,} base + {nov:,} overview tiles in {a.out}/  ({time.time()-t0:.0f}s)")
    print(f"\nfolium/Leaflet usage (max_native_zoom = free client-side overzoom past z{z}):")
    print(f'  TileLayer(tiles="{a.out}/{{z}}/{{x}}/{{y}}.{a.format}", attr="NC OneMap",')
    print(f'            name="NC 6-inch imagery",')
    print(f'            min_zoom={a.zoom_min}, max_native_zoom={z}, max_zoom={z + 2})')


if __name__ == "__main__":
    main()
