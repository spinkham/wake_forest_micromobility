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
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

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

CWEBP = None            # path to the cwebp binary; set when --cwebp is used
CWEBP_FLAGS = None      # list of cwebp flags; None -> encode WebP with Pillow
TMPDIR = "/dev/shm" if os.path.isdir("/dev/shm") else None  # RAM tmp for cwebp input


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
def fetch_block(sess, url, bounds, px, sleep, extra=None, retries=6):
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
    if fmt == "webp" and CWEBP_FLAGS:        # encode via the cwebp CLI (e.g. for -sharp_yuv)
        fd, tmp = tempfile.mkstemp(suffix=".png", dir=TMPDIR)
        os.close(fd)
        try:
            Image.fromarray(rgb, "RGB").save(tmp, "PNG")
            subprocess.run([CWEBP, "-quiet", *CWEBP_FLAGS, tmp, "-o", path], check=True)
        finally:
            os.unlink(tmp)
        return
    im = Image.fromarray(rgb, "RGB")
    if fmt in ("jpg", "jpeg"):
        im.save(path, "JPEG", quality=quality)
    else:
        im.save(path, "WEBP", quality=quality, method=method)


def tile_path(out, z, x, y, fmt):
    return os.path.join(out, str(z), str(x), f"{y}.{fmt}")


# ---- overview pyramid -------------------------------------------------------
def build_overviews(out, zmin, zmax, fmt, quality, method, pool=None,
                    sess=None, service=None, extra=None, sleep=0.4):
    """Downsample each level from the one below.

    A parent with all 4 children is composited locally (free, no network). A
    parent with only SOME children straddles the edge of the cached region --
    the base z20 set is the town polygon's ragged tile-cover, so every level has
    such a fringe.

    Those partial parents used to be skipped outright, to avoid a tile with
    black holes where children were missing. That is right about the holes but
    wrong as a rule, because it COMPOUNDS: a dropped tile makes its own parent
    incomplete, so the perimeter erodes inward one tile per level while the
    level itself shrinks 4x. A z15 tile needs all 1024 of its z20 descendants,
    i.e. the deep interior only -- which is why z15 ended up with 2 tiles, z14
    with none, and z12-14 were then skipped entirely by the isdir() check.
    That left the town-wide default view (~z13-14) with an EMPTY cache, so every
    tile there paid a 404 plus a live exportImage round-trip.

    Fix: fetch a partial parent's own bbox from the source instead of dropping
    it -- one request per fringe tile, and the erosion stops at that level
    rather than cascading. Fetches run serially (politeness); composites stay
    parallel. Existing tiles are left alone, so this is resumable and a re-run
    only fills gaps.
    """
    total = fetched = 0
    for z in range(zmax - 1, zmin - 1, -1):
        cz = os.path.join(out, str(z + 1))
        if not os.path.isdir(cz):
            print(f"  z{z}: skipped (no z{z+1} to downsample from)")
            continue
        parents = {}
        for xs in os.listdir(cz):
            for ys in os.listdir(os.path.join(cz, xs)):
                if ys.endswith("." + fmt):
                    cx, cy = int(xs), int(ys.split(".")[0])
                    parents.setdefault((cx // 2, cy // 2), []).append((cx, cy))

        todo = [(p, k) for p, k in parents.items()
                if not os.path.exists(tile_path(out, z, p[0], p[1], fmt))]
        full, partial = [], []
        for (px_, py_), kids in todo:
            need = {(2 * px_, 2 * py_), (2 * px_ + 1, 2 * py_),
                    (2 * px_, 2 * py_ + 1), (2 * px_ + 1, 2 * py_ + 1)}
            (full if set(kids) == need else partial).append(((px_, py_), kids))

        def _composite(item, z=z):
            (px_, py_), kids = item
            canvas = Image.new("RGB", (512, 512))
            for cx, cy in kids:
                canvas.paste(Image.open(tile_path(out, z + 1, cx, cy, fmt)).convert("RGB"),
                             ((cx & 1) * 256, (cy & 1) * 256))
            small = np.ascontiguousarray(np.asarray(canvas.resize((256, 256), Image.LANCZOS)))
            os.makedirs(os.path.join(out, str(z), str(px_)), exist_ok=True)
            save_tile(small, tile_path(out, z, px_, py_, fmt), fmt, quality, method)
            return 1

        made = sum(pool.map(_composite, full)) if pool else sum(_composite(it) for it in full)

        nf = 0
        if partial and sess is not None:
            for (px_, py_), _kids in partial:
                rgb = fetch_block(sess, service, block_bounds(z, px_, py_, 1), 256, sleep, extra)
                os.makedirs(os.path.join(out, str(z), str(px_)), exist_ok=True)
                save_tile(rgb, tile_path(out, z, px_, py_, fmt), fmt, quality, method)
                nf += 1
                time.sleep(sleep)
        elif partial:
            print(f"  z{z}: WARNING {len(partial)} fringe tiles need a source fetch "
                  f"but no session was passed -- pyramid will erode")

        total += made + nf
        fetched += nf
        have = sum(len(os.listdir(os.path.join(out, str(z), d)))
                   for d in os.listdir(os.path.join(out, str(z)))) if os.path.isdir(os.path.join(out, str(z))) else 0
        print(f"  z{z}: +{made} composited, +{nf} fetched (fringe) | {have} tiles total at this level")
    if fetched:
        print(f"overview fringe tiles fetched from source: {fetched}")
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
    ap.add_argument("--method", type=int, default=6, help="WebP effort 0-6 (Pillow path)")
    ap.add_argument("--cwebp", default="", help="encode WebP via the cwebp CLI with these flags "
                    "(e.g. '-q 90 -m 6 -sharp_yuv -pass 10'); parallelized. Default uses Pillow.")
    ap.add_argument("--workers", type=int, default=0, help="parallel encode workers for --cwebp (0 = cpu count)")
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

    global CWEBP, CWEBP_FLAGS
    pool = None
    if a.cwebp:
        CWEBP = shutil.which("cwebp") or os.path.expanduser("~/bin/cwebp")
        if not os.path.exists(CWEBP):
            sys.exit("cwebp not found on PATH or in ~/bin; install it or drop --cwebp")
        CWEBP_FLAGS = shlex.split(a.cwebp)
        nw = a.workers or (os.cpu_count() or 4)
        pool = ThreadPoolExecutor(max_workers=nw)
        print(f"encoder: cwebp {' '.join(CWEBP_FLAGS)}  ({nw} parallel workers)")

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
    failed = []
    t0 = time.time()
    for n, (bx, by) in enumerate(blocks, 1):
        if a.max_blocks and n > a.max_blocks:
            break
        bb, al, need = block_need[(bx, by)]
        todo = [t for t in need if not os.path.exists(tile_path(a.out, z, t[2], t[3], a.format))]
        if not todo:
            skipped += 1
            continue
        # One block must never kill the run. NC OneMap returns HTTP 504s, and
        # sometimes a 200 whose body is a JSON error rather than an image, under
        # load -- and a 598-block job takes hours, so raising on the first block
        # that exhausts its retries throws away everything still to do. (It did:
        # a run died at block 310/598 after ~2.5 h.) Log it, count it, carry on.
        # Nothing is lost -- already-fetched tiles are skipped on the next run,
        # so re-running the same command retries only the blocks that failed.
        try:
            rgb = fetch_block(sess, service, bb, B * 256, a.sleep, extra)
            fetched += 1
        except RuntimeError as e:
            failed.append((n, bb))
            print(f"  block {n}/{len(blocks)} FAILED (will be retried on a re-run): {e}",
                  file=sys.stderr)
            time.sleep(a.sleep * 4)   # back off harder; the server is struggling
            continue

        def _enc(t, rgb=rgb):
            i, j, tx, ty = t
            sub_rgb = np.ascontiguousarray(rgb[j*256:(j+1)*256, i*256:(i+1)*256, :])
            os.makedirs(os.path.join(a.out, str(z), str(tx)), exist_ok=True)
            save_tile(sub_rgb, tile_path(a.out, z, tx, ty, a.format), a.format, a.quality, a.method)
        if pool:
            list(pool.map(_enc, todo))
        else:
            for t in todo:
                _enc(t)
        written += len(todo)
        if n % 10 == 0 or n == len(blocks):
            print(f"  block {n}/{len(blocks)} | fetched {fetched} skipped {skipped} | "
                  f"{written:,} base tiles | {time.time()-t0:.0f}s")
        time.sleep(a.sleep)

    print(f"base done: {written:,} z{z} tiles ({fetched} blocks fetched, {skipped} already cached)")
    if failed:
        # Loud on purpose: a silently-missing block is a hole in the cache that
        # only shows up later as a live-imagery fallback for the end user.
        print(f"  *** {len(failed)} block(s) FAILED after retries and are NOT cached: "
              f"{[n for n, _ in failed][:20]}{' ...' if len(failed) > 20 else ''}")
        print(f"  *** re-run the same command to retry just those "
              f"(cached tiles are skipped).")
    print("building overviews...")
    nov = build_overviews(a.out, a.zoom_min, z, a.format, a.quality, a.method, pool=pool,
                          sess=sess, service=service, extra=extra, sleep=a.sleep)
    print(f"done: {written:,} base + {nov:,} overview tiles in {a.out}/  ({time.time()-t0:.0f}s)")
    print(f"\nfolium/Leaflet usage (max_native_zoom = free client-side overzoom past z{z}):")
    print(f'  TileLayer(tiles="{a.out}/{{z}}/{{x}}/{{y}}.{a.format}", attr="NC OneMap",')
    print(f'            name="NC 6-inch imagery",')
    print(f'            min_zoom={a.zoom_min}, max_native_zoom={z}, max_zoom={z + 2})')


if __name__ == "__main__":
    main()
