#!/usr/bin/env python3
"""Pre-compress the large deployed assets (the two map HTML pages and the
route graph JSON) for the live server, so Apache serves a static .br sidecar
instead of compressing 10 MB of JSON on every single request.

Brotli beats gzip on all three files by a wide margin (measured on the actual
deployed content): ~8% of original vs gzip's ~12-13%. Brotli has near-universal
modern-browser support (Accept-Encoding: br), so it's the only pre-compressed
sidecar written -- a .gz sidecar was tried too, but /etc/mime.types (root-owned,
can't edit without root) already registers .gz as a real MIME type
(application/gzip), which conflicts with using it purely as an encoding
suffix and makes Apache emit the wrong Content-Type. Non-Brotli clients fall
back to on-the-fly gzip via mod_deflate (already enabled server-side; see
wf-htaccess) instead of a pre-computed sidecar. Written at MAX quality
(brotli quality=11) -- too slow to do per-request, but trivial as a one-off
deploy step (a few seconds per file).

Serving relies on Apache's classic mod_mime double-extension convention
(foo.json.br -> type application/json, encoding br) via the RewriteRule +
AddEncoding directives in wf-htaccess (deployed as .htaccess). That needs
only mod_rewrite + mod_mime, both already enabled on the server -- no root,
no new modules, no server restart. See wf-htaccess for the serving rules.

Run from map/ (or anywhere -- paths are relative to the repo root) after
build_map.py / build_router.py / build_route_graph.py, before deploying.
"""
import os
import time

import brotli

FILES = [
    "../wake-forest-micromobility-map.html",
    "../wake-forest-router.html",
    "../wf-route-graph.json",
]


def compress_one(path):
    if not os.path.exists(path):
        print(f"  skip (not found): {path}")
        return
    data = open(path, "rb").read()
    orig = len(data)

    t0 = time.time()
    br = brotli.compress(data, quality=11)
    t_br = time.time() - t0
    with open(path + ".br", "wb") as fh:
        fh.write(br)

    print(f"  {os.path.basename(path)}: {orig:,} bytes -> "
          f"br {len(br):,} ({100*len(br)/orig:.1f}%, {t_br:.1f}s)")


if __name__ == "__main__":
    print("pre-compressing deployed assets...")
    for f in FILES:
        compress_one(f)
