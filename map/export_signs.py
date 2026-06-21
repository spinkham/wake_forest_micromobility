#!/usr/bin/env python3
"""Export sidewalk-exception sign work orders -- Option C (boundary + major
cross-streets), placed PER ROAD SIDE.

Policy (grounded in MUTCD Part 9 + municipal practice):
  * BEGIN/END (M4-14P/M4-6P) markers at every corridor end -- riders know where
    the exception starts/stops in either direction.
  * At each MAJOR cross-street (collector/arterial) along a corridor, a
    reassurance marker (D11-1, "may be repeated at side streets so bicycles
    entering know they are on a route") + a yield/crossing treatment (R9-6
    "Bikes Yield to Peds"). Minor residential tees are skipped (Option C).
  * Side of road matters: a sidewalk ridden both ways is a two-way facility with
    the wrong-way intersection hazard (Wachtel & Lewiston; FHWA sidepath
    guidance). Where a sidewalk exists on BOTH sides, BOTH are signed so each
    direction rides WITH traffic; where only one side has a sidewalk, that single
    facility is unavoidably two-way and still gets the treatment. Posts are
    therefore placed per side (probed from mapped OSM footways), not per road.

Exact major-crossing nodes come from the pinned street graph; sides come from
mapped footways. Writes sign_posts{,_knee,_parity}.csv/.geojson and
sign_stretches{,_knee,_parity}.csv/.geojson in one pass.
"""
import csv
import numpy as np
import geopandas as gpd
import networkx as nx
from shapely.geometry import Point
from shapely.strtree import STRtree

exec(open("build_islands.py").read())   # -> edges, minimal, knee_idx, parity_idx, G, ROAD, _xy, PROJ, _foot

OFFSET, TOL, DEDUP, BRIDGE = 11.0, 7.0, 16.0, 40.0   # m: post offset, footway match, dedup, same-street gap bridge
# opposite sides sit 2*OFFSET=22 m apart > DEDUP, so they survive dedup; same-side
# multi-approach dupes (<16 m) merge to one post per intersection per side.
MAJOR = {"primary", "secondary", "tertiary", "primary_link", "secondary_link",
         "tertiary_link", "trunk", "trunk_link"}

# drivable graph -> detect a MAJOR cross-street meeting a corridor node
Gd = nx.Graph()
for _, r in edges[edges["hw"].isin(ROAD)].iterrows():
    Gd.add_edge(int(r["u"]), int(r["v"]), hw=r["hw"])

# projected edge lookup (geom, name) and footway spatial index for side probing
_ep = edges.to_crs(PROJ)
E = {}
for _, r in _ep.iterrows():
    E[frozenset((int(r["u"]), int(r["v"])))] = (r.geometry, str(r.get("name")))
foot_proj = list(_foot.to_crs(PROJ).geometry.values)
ftree = STRtree(foot_proj)


def npos(n):
    p = _xy.loc[n].geometry
    return np.array([p.x, p.y])


def has_foot(pt):
    for i in ftree.query(pt.buffer(TOL)):
        if foot_proj[i].distance(pt) <= TOL:
            return True
    return False


def tangent(geom, nxy):
    """unit direction pointing from the corridor-node end into the corridor."""
    c0, c1 = np.array(geom.coords[0]), np.array(geom.coords[-1])
    if np.hypot(*(c0 - nxy)) <= np.hypot(*(c1 - nxy)):
        base, far = Point(geom.coords[0]), geom.interpolate(min(15.0, geom.length))
    else:
        base, far = Point(geom.coords[-1]), geom.interpolate(max(0.0, geom.length - 15.0))
    v = np.array([far.x - base.x, far.y - base.y])
    return v / (np.hypot(*v) or 1.0)


def side_posts(node, geom):
    """posts offset to each side of the road that actually has a sidewalk."""
    nxy = npos(node)
    t = tangent(geom, nxy)
    perp = np.array([-t[1], t[0]])          # left normal
    out = []
    for s in (1, -1):                       # +left, -right
        pt = Point(*(nxy + perp * OFFSET * s))
        if has_foot(pt):
            out.append(pt)
    return out or [Point(*nxy)]             # fallback: centerline if footway not matched


def export(level, L, suffix):
    sub = edges.loc[L]
    CG = nx.Graph()
    for _, r in sub.iterrows():
        CG.add_edge(int(r["u"]), int(r["v"]))
    Lset = {frozenset((int(r["u"]), int(r["v"]))) for _, r in sub.iterrows()}

    # bridge near corridor nodes on the SAME street (digitizing/intersection gaps)
    # so a continuous sidewalk isn't split into fragments with spurious end posts.
    node_names = {}
    for fs in Lset:
        u, v = tuple(fs)
        nm = E.get(fs, (None, None))[1]
        if nm not in (None, "nan", "None", ""):
            node_names.setdefault(u, set()).add(nm)
            node_names.setdefault(v, set()).add(nm)
    cn0 = list(CG.nodes)
    arr = np.array([npos(n) for n in cn0])
    for i in range(len(cn0)):
        d = np.hypot(arr[:, 0] - arr[i, 0], arr[:, 1] - arr[i, 1])
        for j in np.where((d > 0) & (d <= BRIDGE))[0]:
            if node_names.get(cn0[i], set()) & node_names.get(cn0[j], set()):
                CG.add_edge(cn0[i], cn0[j], bridge=True)   # not a sign edge; merges fragments

    posts = []                              # (Point_proj, sign_type, street)
    str_rows, str_geoms = [], []
    n_ends = n_major = 0
    for sid, comp in enumerate(nx.connected_components(CG)):
        sg = CG.subgraph(comp)
        term = [n for n in sg.nodes if sg.degree(n) == 1] or list(sg.nodes)[:1]
        tset = set(term)
        major = []
        for n in sg.nodes:
            if n in tset:
                continue
            for nb in Gd.neighbors(n):
                if frozenset((n, nb)) in Lset:
                    continue
                if Gd[n][nb].get("hw") in MAJOR:
                    major.append(n)
                    break
        n_ends += len(term)
        n_major += len(major)

        cedges = [frozenset((u, v)) for u, v in sg.edges()]
        names = [E[e][1] for e in cedges if e in E and E[e][1] not in ("nan", "None", "")]
        name = max(set(names), key=names.count) if names else "unnamed"
        length = sum(E[e][0].length for e in cedges if e in E) / 1609.34
        two = 0
        nodes_signed = [(n, "begin/end") for n in term] + [(n, "major-crossing") for n in major]
        for n, typ in nodes_signed:
            geom = None
            for nb in sg.neighbors(n):
                geom = E.get(frozenset((n, nb)), (None,))[0]
                if geom is not None:
                    break
            if geom is None:
                continue
            sp = side_posts(n, geom)
            if len(sp) == 2:
                two += 1
            for pt in sp:
                posts.append((pt, typ, name))
        sides = 2 if nodes_signed and two >= 0.5 * len(nodes_signed) else 1
        str_rows.append({"stretch_id": sid, "street": name, "length_mi": round(length, 3),
                         "sidewalk_sides": sides, "n_sign_nodes": len(nodes_signed)})
        str_geoms.append(_ep.loc[[i for i in sub.index
                                  if frozenset((int(edges.at[i, "u"]), int(edges.at[i, "v"]))) in
                                  {frozenset((u, v)) for u, v in sg.edges()}], "geometry"].unary_union)

    # dedup posts within DEDUP m (merges multi-approach dupes at a node-side;
    # keeps opposite sides ~2*OFFSET apart and separate intersections distinct)
    pts = np.array([[p.x, p.y] for p, _, _ in posts])
    ll = gpd.GeoSeries([p for p, _, _ in posts], crs=PROJ).to_crs(4326)
    used = np.zeros(len(pts), bool)
    post_rows, post_geoms = [], []
    for i in range(len(pts)):
        if used[i]:
            continue
        grp = np.where(np.hypot(pts[:, 0] - pts[i, 0], pts[:, 1] - pts[i, 1]) <= DEDUP)[0]
        used[grp] = True
        types = {posts[j][1] for j in grp}
        typ = "begin/end" if "begin/end" in types else "major-crossing"
        streets = sorted({posts[j][2] for j in grp if posts[j][2] != "unnamed"})
        p = ll.iloc[i]
        post_rows.append({"post_id": len(post_rows), "lat": round(p.y, 6), "lon": round(p.x, 6),
                          "sign_type": typ, "streets": " / ".join(streets) or "unnamed"})
        post_geoms.append(Point(p.x, p.y))

    for fn, rows in [(f"sign_stretches{suffix}.csv", str_rows), (f"sign_posts{suffix}.csv", post_rows)]:
        with open(fn, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    gpd.GeoDataFrame(str_rows, geometry=str_geoms, crs=PROJ).to_crs(4326).to_file(
        f"sign_stretches{suffix}.geojson", driver="GeoJSON")
    gpd.GeoDataFrame(post_rows, geometry=post_geoms, crs=4326).to_file(
        f"sign_posts{suffix}.geojson", driver="GeoJSON")
    nbe = sum(1 for r in post_rows if r["sign_type"] == "begin/end")
    nmx = len(post_rows) - nbe
    two_sided = sum(1 for r in str_rows if r["sidewalk_sides"] == 2)
    print(f"[{level:7s}] {sub['len_mi'].sum():.1f} mi | {len(str_rows)} corridors "
          f"({two_sided} two-sided) | {len(post_rows)} posts = {nbe} begin/end + {nmx} major-crossing "
          f"| ~{2*len(post_rows)} faces  (ends:{n_ends} major-x:{n_major} centerline-nodes)")


print("\n=== Option C sign export (per-side: begin/end + major cross-streets) ===")
for level, L, suffix in [("minimal", list(minimal.index), ""),
                         ("knee", list(minimal.index) + knee_idx, "_knee"),
                         ("parity", list(minimal.index) + knee_idx + parity_idx, "_parity")]:
    export(level, L, suffix)
