#!/usr/bin/env python3
"""Phase 2: micromobility reachability / "islands" under Chapter 30.

Computes the connected components of the PERMITTED riding network (greenways/
paths, cycleways, roads <=25 mph, on-road bike lanes) within the corporate
limits, with realistic detours (graph extends 2 km past the limits) and small
intersection-gap snapping (10 m). Writes reachability.geojson (in-town rideable
edges tagged role = main / island) for build_map.py to layer onto the main map.

The graph is PINNED to disk (wf_graph.pkl) on first run, because osmnx rebuilds
graphs slightly non-deterministically -- pinning makes per-street connectivity
reproducible.

Bike lanes are legally ambiguous (§30-90(b)(2) permits "bicycle lanes" but bars
>25 mph roads). We report both readings; the overlay uses the rider-favorable
one (a bike lane unlocks its road at any speed), which differs from the
conservative reading by only a few small pods.
"""
import os
import re
import pickle
import geopandas as gpd
import pandas as pd
import networkx as nx
import osmnx as ox
from shapely.ops import unary_union

ox.settings.use_cache = True
ox.settings.cache_folder = "osm_cache"
# osmnx's default useful_tags_way omits the tags that actually answer "may I
# ride here?" -- bicycle/foot/footway/cycleway/surface were all absent from the
# graph, which is why is_greenway_footway has to infer rideability by spatially
# cross-referencing the Town's GIS instead of just reading OSM's own tag. These
# only affect a graph REBUILD (a pinned wf_graph.pkl is loaded as-is); the
# cached Overpass response already carries every tag, so re-pinning needs no
# new network request and is topologically identical (verified: same 26064
# nodes / 68725 edges / identical lengths).
ox.settings.useful_tags_way = sorted(set(ox.settings.useful_tags_way) | {
    "bicycle", "foot", "footway", "cycleway", "cycleway:left", "cycleway:right",
    "cycleway:both", "surface", "segregated", "sidewalk", "smoothness"})
PROJ = 32617
SPEED = {"motorway": 55, "motorway_link": 45, "trunk": 45, "trunk_link": 35,
         "primary": 45, "primary_link": 35, "secondary": 35, "secondary_link": 35,
         "tertiary": 35, "tertiary_link": 30, "residential": 25, "living_street": 20,
         "unclassified": 30, "road": 30}
ROAD = set(SPEED)
NONRIDE = {"footway", "steps", "pedestrian", "service", "construction", "proposed", "track"}
FREEWAY = {"motorway", "motorway_link", "trunk", "trunk_link"}
SNAP_M = 10
DETOUR_M = 2000
GRAPH_PKL = "wf_graph.pkl"


def head(hw):
    return hw[0] if isinstance(hw, list) else hw


def mph(v):
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0]
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else None


# ---- load -------------------------------------------------------------------
juris = gpd.read_file("corporate_limits.geojson").to_crs(4326)
jg = unary_union(juris.geometry.values)
ncdot = gpd.read_file("ncdot_speed.geojson").to_crs(PROJ)

if os.path.exists(GRAPH_PKL):
    print("loading pinned graph", GRAPH_PKL)
    with open(GRAPH_PKL, "rb") as fh:
        G = pickle.load(fh)
else:
    poly = gpd.GeoSeries([unary_union(juris.to_crs(PROJ).geometry.values).buffer(DETOUR_M)],
                         crs=PROJ).to_crs(4326).iloc[0]
    print(f"building OSM graph (+{DETOUR_M} m detour buffer) and pinning to {GRAPH_PKL}...")
    G = ox.graph_from_polygon(poly, network_type="all", simplify=True,
                              retain_all=True, truncate_by_edge=True)
    with open(GRAPH_PKL, "wb") as fh:
        pickle.dump(G, fh)

edges = ox.graph_to_gdfs(G, nodes=False).reset_index()
edges["hw"] = edges["highway"].map(head)
edges["intown"] = edges.geometry.representative_point().within(jg)
edges["len_mi"] = edges.to_crs(PROJ).length / 1609.34

# ---- NCDOT posted speed onto road edges (along-segment match; see nc_speed) --
import nc_speed
roadmask = edges["hw"].isin(ROAD)
edges["posted"] = nc_speed.assign_posted(edges, ncdot, roadmask, PROJ)


def edge_speed(r):
    if pd.notna(r["posted"]):
        return int(r["posted"])
    s = mph(r.get("maxspeed"))
    return s if s is not None else SPEED.get(r["hw"], 30)


edges["speed"] = edges.apply(edge_speed, axis=1)

# ---- existing on-road bike lanes (Town CTP) ---------------------------------
bl_ex = gpd.read_file("town_bike_lanes.geojson").to_crs(PROJ)
bl_ex = bl_ex[bl_ex["Status"] == "Existing"]
bl_buf = unary_union(bl_ex.geometry.buffer(18).values)
edges["bikelane"] = edges.to_crs(PROJ).geometry.representative_point().within(bl_buf) & roadmask


# Some of the town's own paved, shared-use greenway trails are tagged
# highway=footway in OSM instead of cycleway/path (e.g. Kiwanis Greenway,
# Dunn Creek Greenway -- confirmed via the Town's own GIS as 8-10 ft paved,
# Completed facilities, not narrow hiking paths). A plain footway is
# NONRIDE (right for an actual sidewalk), but that wrongly gates a real,
# named, town-built greenway too. Cross-reference footways against the
# Town's authoritative greenways.geojson + mup.geojson: a footway that
# substantially coincides with a mapped greenway/MUP is treated as
# permitted, same as a cycleway.
_gw_official = gpd.read_file("greenways.geojson")
_mup_official = gpd.read_file("mup.geojson")
_gw_buf = unary_union(pd.concat([_gw_official, _mup_official])[["geometry"]]
                       .to_crs(PROJ).geometry.buffer(15).values)
_foot_cand = edges["hw"].isin(("footway", "pedestrian"))
_ep_pre = edges.to_crs(PROJ)
edges["is_greenway_footway"] = False
edges.loc[_foot_cand, "is_greenway_footway"] = _ep_pre.loc[edges[_foot_cand].index, "geometry"].apply(
    lambda g: g.intersection(_gw_buf).length >= 0.5 * g.length).values
print(f"footway segments reclassified as permitted greenway/path (matched "
      f"Town GIS): {edges['is_greenway_footway'].sum()} edges, "
      f"{edges.loc[edges['is_greenway_footway'], 'len_mi'].sum():.1f} mi")


def traversable(r, mode):
    hw = r["hw"]
    if r["is_greenway_footway"]:
        return True
    if hw in NONRIDE:
        return False
    if hw in ("cycleway", "path"):
        return True
    if r["bikelane"] and (mode == "all" or r["speed"] <= 25):
        return True
    if not r["intown"]:
        # Chapter 30 doesn't restrict speed outside the corporate limits, but
        # keep this in line with the router: never a freeway, and cap at 45 mph
        # (a road over 45 with no bike lane/sidewalk isn't rideable-in-practice,
        # and there's no shoulder data to say otherwise). Without this cap the
        # analysis would "ride" out-of-town freeways and report freeway-severed
        # subdivisions as connected when no bike-legal crossing exists.
        return (r["hw"] not in FREEWAY) and (r["speed"] <= 45)
    return r["speed"] <= 25


edges["trav_all"] = edges.apply(lambda r: traversable(r, "all"), axis=1)
edges["trav_le25"] = edges.apply(lambda r: traversable(r, "le25"), axis=1)

# "Sidewalks on >25 mph roads where there's no bike lane" accommodation: such a
# road becomes usable IF a sidewalk runs along it (proxied by a parallel mapped
# footway covering >=50% of the segment). Those road edges become connectors.
_foot = edges[edges["hw"].isin(["footway", "steps", "pedestrian"]) & edges["intown"]]
_swbuf = unary_union(_foot.to_crs(PROJ).geometry.buffer(18).values) if len(_foot) else None
_ep = edges.to_crs(PROJ)
_cand = edges["intown"] & edges["hw"].isin(ROAD) & (edges["speed"] > 25) & (~edges["bikelane"])
edges["sw_road"] = False
if _swbuf is not None:
    edges.loc[_cand, "sw_road"] = _ep.loc[_cand, "geometry"].apply(
        lambda g: g.intersection(_swbuf).length >= 0.5 * g.length).values
edges["trav_all_sw"] = edges["trav_all"] | edges["sw_road"]
edges["trav_le25_sw"] = edges["trav_le25"] | edges["sw_road"]
print(f"sidewalk-connector roads (>25, no bike lane, has sidewalk): "
      f"{edges.loc[edges['sw_road'], 'len_mi'].sum():.1f} mi")

# ---- snap tiny intersection-node gaps ---------------------------------------
_alln = set(edges.loc[edges["trav_all"], "u"]) | set(edges.loc[edges["trav_all"], "v"])
_np = ox.graph_to_gdfs(G, edges=False).to_crs(PROJ)
_np = _np[_np.index.isin(_alln)].reset_index()[["osmid", "geometry"]]
_buf = _np.copy(); _buf["geometry"] = _buf.geometry.buffer(SNAP_M)
_pr = gpd.sjoin(_buf, _np, predicate="intersects")
SNAP_PAIRS = [(a, b) for a, b in zip(_pr["osmid_left"], _pr["osmid_right"]) if a < b]
print(f"node-snap (<= {SNAP_M} m): {len(SNAP_PAIRS)} candidate connector(s)")

# ---- marked-crosswalk connectors (shared with build_route_graph.py) ----------
# OSM tags pedestrian crossings as highway=footway, footway=crossing, excluded
# like any footway -- so a marked crosswalk over a barrier road (a freeway/trunk
# you can neither ride along nor share an intersection node with) leaves the two
# sides disconnected even though a bike may legally cross there. Bridge each
# crossing by connecting the nearest routable node on each side: the same idea
# as the SNAP_PAIRS intersection-gap snap, but for real marked crossings and a
# wider gap (a road is wider than the 10 m snap tolerance). Used by BOTH the
# reachability analysis (here) and the router (build_route_graph.py) so their
# connectivity agrees.
CROSS_T = 18.0     # m: max distance from a crossing endpoint to a routable node
CROSS_SPAN = 45.0  # m: max node-to-node span of one crosswalk connector
_nodes_p = ox.graph_to_gdfs(G, edges=False).to_crs(PROJ)
_node_x, _node_y = _nodes_p.geometry.x, _nodes_p.geometry.y
try:
    _xw = gpd.read_file("osm_highways.geojson")
    _xw = _xw[_xw["footway"] == "crossing"].to_crs(PROJ) if "footway" in _xw.columns else _xw.iloc[0:0]
except Exception as _e:
    print("crosswalk source (osm_highways.geojson) skipped:", _e)
    _xw = _nodes_p.iloc[0:0]


def crosswalk_connectors(used_ids):
    """[(u, v, span_m), ...]: connectors that bridge marked crosswalks between
    routable nodes in used_ids. Shared by the reachability analysis and the
    router so both agree on where a bike can cross a barrier road."""
    import numpy as np
    ids = [i for i in used_ids if i in _nodes_p.index]
    if not len(_xw) or not ids:
        return []
    xs = _node_x.loc[ids].to_numpy(); ys = _node_y.loc[ids].to_numpy()

    def nearest(px, py):
        d = np.hypot(xs - px, ys - py)
        j = int(d.argmin())
        return (ids[j], float(d[j])) if d[j] <= CROSS_T else (None, None)

    out = {}
    for geom in _xw.geometry:
        if geom is None or geom.is_empty:
            continue
        g = max(geom.geoms, key=lambda p: p.length) if geom.geom_type == "MultiLineString" else geom
        (x0, y0), (x1, y1) = g.coords[0], g.coords[-1]
        n0, _d0 = nearest(x0, y0)
        n1, _d1 = nearest(x1, y1)
        if n0 is None or n1 is None or n0 == n1:
            continue
        span = float(np.hypot(_node_x[n0] - _node_x[n1], _node_y[n0] - _node_y[n1]))
        if span > CROSS_SPAN:
            continue
        key = (n0, n1) if n0 < n1 else (n1, n0)
        if key not in out or span < out[key]:
            out[key] = span
    return [(u, v, s) for (u, v), s in out.items()]


# crossing pairs over the broadest routability node set (trav_all_sw); reach()
# adds each only where both endpoints are in that reading's subgraph.
_cross_nodeset = set(edges.loc[edges["trav_all_sw"], "u"]) | set(edges.loc[edges["trav_all_sw"], "v"])
CROSS_PAIRS = crosswalk_connectors(_cross_nodeset)
print(f"marked-crosswalk connectors bridging barrier roads: {len(CROSS_PAIRS)}")

# ---- coverage + ambiguous bike lanes ----------------------------------------
inrd = edges[edges["intown"] & roadmask]
cov = inrd.groupby(inrd.apply(
    lambda r: "ncdot" if pd.notna(r["posted"]) else ("osm" if mph(r.get("maxspeed")) is not None else "inferred"),
    axis=1))["len_mi"].sum()
print("in-town road miles by speed source:",
      {k: round(cov.get(k, 0), 1) for k in ("ncdot", "osm", "inferred")})
bl_fast = edges[edges["bikelane"] & edges["intown"] & (edges["speed"] > 25)]
print(f"existing bike lanes on >25 mph roads (the ambiguous segments): {bl_fast['len_mi'].sum():.1f} mi")


def reach(col):
    P = nx.Graph()
    for _, r in edges[edges[col]].iterrows():
        P.add_edge(r["u"], r["v"], length=float(r.get("length") or 0))
    for a, b in SNAP_PAIRS:
        if a in P and b in P:
            P.add_edge(a, b, length=0.0)
    for a, b, s in CROSS_PAIRS:
        if a in P and b in P:
            P.add_edge(a, b, length=s)
    comps = list(nx.connected_components(P))
    main = max(comps, key=lambda c: sum(d["length"] for _, _, d in P.subgraph(c).edges(data=True)))
    n2c = {n: i for i, c in enumerate(comps) for n in c}
    mainid = n2c[next(iter(main))]
    ride = edges[edges[col] & edges["intown"] & ~edges["hw"].isin(FREEWAY)].copy()
    ride["comp"] = ride["u"].map(n2c)
    ride["role"] = ride["comp"].map(lambda c: "main" if c == mainid else "island")
    return ride


def summarize(ride, label):
    tot = ride["len_mi"].sum()
    isl = ride[ride["role"] == "island"]
    pods = isl.groupby("comp").ngroups
    print(f"  {label:34s} rideable {tot:6.1f} mi | connected {100*(1-isl['len_mi'].sum()/tot):4.1f}% | "
          f"stranded {isl['len_mi'].sum():6.1f} mi / {pods} pods")
    return tot, isl["len_mi"].sum(), pods


print("\n=== GROUNDED reachability (NCDOT speeds, snapped, pinned graph) ===")
ride_all = reach("trav_all")
ride_le = reach("trav_le25")
ride_all_sw = reach("trav_all_sw")
ride_le_sw = reach("trav_le25_sw")
a = summarize(ride_all, "bike lanes count everywhere")
b = summarize(ride_le, "bike lanes only on <=25 roads")
summarize(ride_all_sw, "+ sidewalks on >25 roads (no bike lane)")
print(f"  -> bike-lane/>25 ambiguity moves {b[1]-a[1]:+.1f} mi / {b[2]-a[2]:+d} pods")

# how much of the otherwise-unreachable network does the sidewalk rule connect?
base_isl = ride_all[ride_all["role"] == "island"]
sw_role = ride_all_sw["role"].reindex(base_isl.index)
recon_mi = base_isl.loc[(sw_role == "main").values, "len_mi"].sum()
base_mi = base_isl["len_mi"].sum()
base_pods = base_isl.groupby("comp").ngroups
_bi = base_isl.copy(); _bi["nowmain"] = (sw_role == "main").values
recon_pods = int(_bi.groupby("comp")["nowmain"].any().sum())
print("\n=== sidewalks-on->25 (no bike lane) rule ===")
print(f"  reconnects {recon_mi:.1f} of {base_mi:.1f} otherwise-stranded mi "
      f"({100*recon_mi/base_mi:.0f}%) and {recon_pods} of {base_pods} stranded pods "
      f"({100*recon_pods/base_pods:.0f}%)")

# overlay for the main map: four readings (permissive/strict x sidewalk on/off).
# role == "na" means the edge isn't part of the rideable network under that
# reading (e.g. a >25 bike-lane link under strict, or a sidewalk-connector with
# the sidewalk rule off). trav_all_sw is the superset of all four.
out = ride_all_sw[["len_mi", "name", "geometry"]].copy()
out["role_all"] = ride_all["role"].reindex(out.index).fillna("na")
out["role_le25"] = ride_le["role"].reindex(out.index).fillna("na")
out["role_all_sw"] = ride_all_sw["role"].reindex(out.index).fillna("na")
out["role_le25_sw"] = ride_le_sw["role"].reindex(out.index).fillna("na")
out["name"] = out["name"].astype(str)
out.to_file("reachability.geojson", driver="GeoJSON")
print("saved reachability.geojson (roles: role_all, role_le25, role_all_sw, role_le25_sw)")

# ---- minimal >25 sidewalk sections to legalize to connect the town -----------
# Treat the baseline permitted network (trav_all) as free and the candidate
# sidewalk-connector roads as costed (length). The minimum Steiner tree over the
# baseline components (contracted to super-nodes) using only sidewalk edges is
# the least-mileage set of sidewalk sections whose legalization reconnects every
# sidewalk-reachable pod to the main network -- i.e. the segments to sign as
# exceptions instead of a blanket policy.
from networkx.algorithms.approximation import steiner_tree
P0 = nx.Graph()
for _, r in edges[edges["trav_all"]].iterrows():
    P0.add_edge(r["u"], r["v"], length=float(r.get("length") or 0))
for a, b in SNAP_PAIRS:
    if a in P0 and b in P0:
        P0.add_edge(a, b, length=0.0)
for a, b, s in CROSS_PAIRS:
    if a in P0 and b in P0:
        P0.add_edge(a, b, length=s)
comps0 = list(nx.connected_components(P0))
main0 = max(comps0, key=lambda c: sum(d["length"] for _, _, d in P0.subgraph(c).edges(data=True)))
n2c0 = {n: i for i, c in enumerate(comps0) for n in c}
mainid0 = n2c0[next(iter(main0))]
sw = edges[edges["sw_road"] & edges["intown"]].copy()


def _key(n):
    return ("C", n2c0[n]) if n in n2c0 else ("N", n)


H = nx.Graph()
swedge = {}
for idx, r in sw.iterrows():
    ku, kv = _key(r["u"]), _key(r["v"])
    if ku == kv:
        continue
    e = tuple(sorted([ku, kv], key=str))
    w = float(r["len_mi"])
    if (not H.has_edge(*e)) or w < H.edges[e]["weight"]:
        H.add_edge(*e, weight=w)
        swedge[e] = idx
minimal = sw.iloc[0:0]
n_pods = 0
mkey = ("C", mainid0)
if mkey in H:
    comp_main = nx.node_connected_component(H, mkey)
    terminals = [n for n in comp_main if n[0] == "C"]
    n_pods = len(terminals) - 1
    st = steiner_tree(H.subgraph(comp_main).copy(), terminals, weight="weight")
    chosen = [swedge[tuple(sorted(e, key=str))] for e in st.edges()
              if tuple(sorted(e, key=str)) in swedge]
    minimal = sw.loc[sw.index.isin(chosen)]
print("\n=== minimal >25 sidewalk sections to legalize (sign as exceptions) ===")
print(f"  {minimal['len_mi'].sum():.1f} mi across {len(minimal)} sections "
      f"(vs {sw['len_mi'].sum():.1f} mi of all candidate sidewalk-roads) "
      f"-> reconnects {n_pods} pods")
mout = minimal[["len_mi", "name", "geometry"]].copy()
mout["name"] = mout["name"].astype(str)
mout.to_file("sidewalk_minimal.geojson", driver="GeoJSON")
print("  saved sidewalk_minimal.geojson")

# ---- path-cost: a few EXTRA stretches to cut pathological detours -------------
# The minimal (Steiner) set minimizes legalized mileage but can attach a pod via
# a far bridge, forcing a long detour to downtown vs the full rule. Greedily add
# a few extra sidewalk stretches that most cut those detours.
import numpy as np
from shapely.geometry import Point
_xy = ox.graph_to_gdfs(G, edges=False).to_crs(PROJ)
_pos = {n: (_xy.loc[n].geometry.x, _xy.loc[n].geometry.y) for n in P0.nodes}
swinfo = {i: (int(r["u"]), int(r["v"]), float(r.get("length") or 0)) for i, r in sw.iterrows()}
swmap = {frozenset((int(r["u"]), int(r["v"]))): i for i, r in sw.iterrows()}
minset = set(minimal.index)


def _with(idxs):
    Gw = P0.copy()
    for i in idxs:
        u, v, w = swinfo[i]
        Gw.add_edge(u, v, length=w)
    return Gw


_dt = gpd.GeoSeries([Point(-78.5103, 35.9799)], crs=4326).to_crs(PROJ).iloc[0]
dest = min((n for n in P0.nodes if n2c0.get(n) == mainid0),
           key=lambda n: Point(_pos[n]).distance(_dt))
G_full = _with(list(sw.index))
df_len, df_path = nx.single_source_dijkstra(G_full, dest, weight="length")

pod_reps = {}
for cid in set(n2c0.values()):
    if cid == mainid0:
        continue
    ns = [n for n in P0.nodes if n2c0[n] == cid]
    cx = np.mean([_pos[n][0] for n in ns]); cy = np.mean([_pos[n][1] for n in ns])
    rep = min(ns, key=lambda n: (_pos[n][0] - cx) ** 2 + (_pos[n][1] - cy) ** 2)
    if df_len.get(rep, float("inf")) < float("inf"):
        pod_reps[cid] = rep
reps = list(pod_reps.values())

# candidate extra stretches = sw edges (not minimal) on some pod's full-rule path
relevant = set()
for r in reps:
    p = df_path.get(r, [])
    for a, b in zip(p, p[1:]):
        k = frozenset((a, b))
        if k in swmap and swmap[k] not in minset:
            relevant.add(swmap[k])
RSG = nx.Graph()
for i in relevant:
    u, v, _ = swinfo[i]; RSG.add_edge(u, v, idx=i)
cands = []
for comp in nx.connected_components(RSG):
    idxs = [d["idx"] for _, _, d in RSG.subgraph(comp).edges(data=True)]
    cands.append((idxs, sum(swinfo[i][2] for i in idxs) / 1609.34))


def _det(d):
    return [(d.get(r, float("inf")) - df_len[r]) / 1609.34 for r in reps]


cur = list(minimal.index)
dcur = nx.single_source_dijkstra_path_length(_with(cur), dest, weight="length")
det0 = _det(dcur)
chosen = []                       # (idxs, cumulative_extra_mi, mean_detour_after)
cum = 0.0
remaining = list(range(len(cands)))
while remaining:                  # run to completion (full route parity)
    cur_det = {r: dcur.get(r, float("inf")) - df_len[r] for r in reps}
    best = None
    for kk in remaining:
        idxs, smi = cands[kk]
        if smi <= 0:
            continue
        dt = nx.single_source_dijkstra_path_length(_with(cur + idxs), dest, weight="length")
        red = sum(max(0.0, cur_det[r] - (dt.get(r, float("inf")) - df_len[r])) for r in reps) / 1609.34
        if best is None or red / smi > best[1]:
            best = (kk, red / smi, red, idxs, smi)
    if best is None or best[2] < 0.02:
        break
    kk, _, red, idxs, smi = best
    remaining.remove(kk); cur += idxs; cum += smi
    dcur = nx.single_source_dijkstra_path_length(_with(cur), dest, weight="length")
    chosen.append((idxs, cum, float(np.mean(_det(dcur)))))
parity_mi = cum
# knee = first cumulative point where the mean detour falls to <=0.35 mi
knee_mi = next((c for _, c, m in chosen if m <= 0.35), parity_mi)
knee_idx = [i for idxs, c, m in chosen if c <= knee_mi for i in idxs]
parity_idx = [i for idxs, c, m in chosen if c > knee_mi for i in idxs]
det_k = _det(nx.single_source_dijkstra_path_length(_with(list(minimal.index) + knee_idx), dest, weight="length"))
print("\n=== path-cost: detour to downtown vs the FULL rule (3 budget levels) ===")
for lbl, d, mi in [("minimal (4.9mi)", det0, 0.0), (f"knee (+{knee_mi:.1f}mi)", det_k, knee_mi),
                   (f"full parity (+{parity_mi:.1f}mi)", _det(dcur), parity_mi)]:
    print(f"  {lbl:22s} mean {np.mean(d):.2f} mi | max {max(d):.2f} | pods>0.5: {sum(1 for x in d if x > 0.5)}")


def _savegj(idxs, fn):
    if idxs:
        a = edges.loc[idxs, ["len_mi", "name", "geometry"]].copy()
        a["name"] = a["name"].astype(str)
        a.to_file(fn, driver="GeoJSON")


_savegj(knee_idx, "sidewalk_knee.geojson")
_savegj(parity_idx, "sidewalk_parity.geojson")
print(f"  saved sidewalk_knee.geojson ({len(knee_idx)} seg) + sidewalk_parity.geojson ({len(parity_idx)} seg)")
