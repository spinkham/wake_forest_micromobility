#!/usr/bin/env python3
"""Export the routable graph as compact JSON for the client-side router
(build_router.py), which offers THREE routing tiers:

  legal        -- what Chapter 30 allows TODAY: paths, on-road bike lanes,
                  and <=25 mph streets. Riding on a sidewalk is BARRED by
                  Chapter 30, so sidewalk-along-a-fast-road is NOT legal and
                  is excluded here (it stays usable in the safe/least_unsafe
                  tiers, which weigh physical safety, not legality). The
                  town's jurisdictional quirk (speed unrestricted outside the
                  corporate limits) is honored but CAPPED at 45 mph, and
                  freeways are excluded outright. Legal is not the same as
                  safe: a 45 mph road with no bike lane outside town is legal
                  but not safe.
  safe         -- the same speed-based safety rule applied UNIFORMLY, in town
                  or not (see the "safe riding map" fix): <=25 mph, a bike
                  lane, a path/cycleway, or a >25 mph road with a genuinely
                  adjacent sidewalk. Freeways always excluded.
  least_unsafe -- safe, PLUS a bridging allowance: roads posted 26-44 mph
                  (never >=45, per the user's "no 45 or over") with no bike
                  lane/sidewalk become usable at a steep cost penalty, so the
                  router takes the SHORTEST possible unsafe stretch only when
                  it's the sole way to reach an area safe-tier can't --
                  answering "what's the least-unsafe way to get there."

Crossing a fast road is NOT modeled as a special case: when a quiet street
crosses an arterial at a real intersection, both sides already share the same
OSM node, so a route can pass straight through that node on safe cross-street
edges alone, without ever using an edge that runs ALONG the fast road. No
extra logic needed -- just don't drop those shared nodes.

Rather than precomputing one "kind" per edge (impossible -- the same edge can
be a different kind in different tiers, e.g. a 30 mph edge outside town is
"legal_other" under Legal but "unsafe_connector" under Least-unsafe), this
script exports raw per-edge attributes; build_router.py's JS derives
traversability + kind + cost dynamically per tier at routing time.

Reuses the exec-prefix pattern (see fig_variant.py / export_signs.py /
side_roads.py) for the graph build and the per-edge speed/bikelane columns.
Must run from map/ (relative file paths).

Output: ../wf-route-graph.json
  {
    "bbox": [minlon, minlat, maxlon, maxlat],
    "nodes": {"<osmid>": [lat, lon], ...},
    "edges": [{"u": id, "v": id, "len": meters, "name": str|null,
                "poly": [[lat, lon], ...],
               "path": bool, "bikelane": bool, "speed": int,
               "sidewalk": bool, "freeway": bool, "intown": bool, "lot": bool}
              | {"u": id, "v": id, "len": 0, "snap": true, "poly": [...]}
              | {"u": id, "v": id, "len": meters, "cross": true, "poly": [...]}, ...]
  }
"""
import json

_src = open("build_islands.py").read().split('print("\\n=== GROUNDED reachability')[0]
exec(_src)

# ---- sidewalk-adjacency: extend build_islands.py's in-town-only footway
# buffer to every footway in the graph (town + 2km buffer), so a fast road
# just outside the corporate limits can qualify via a real adjacent sidewalk
# on equal footing with an in-town one (same "uniform rule" fix as before). --
_foot_all = edges[edges["hw"].isin(["footway", "steps", "pedestrian"])]
_swbuf_all = unary_union(_foot_all.to_crs(PROJ).geometry.buffer(18).values) if len(_foot_all) else None
_cand_all = edges["hw"].isin(ROAD) & (edges["speed"] > 25) & (~edges["bikelane"])
edges["sidewalk_safe"] = False
if _swbuf_all is not None:
    edges.loc[_cand_all, "sidewalk_safe"] = _ep.loc[_cand_all, "geometry"].apply(
        lambda g: g.intersection(_swbuf_all).length >= 0.5 * g.length).values

edges["is_freeway"] = edges["hw"].isin(FREEWAY)
edges["is_path"] = edges["hw"].isin(("cycleway", "path"))

# ---- greenway-as-footway: some of the town's own paved, shared-use greenway
# trails are tagged highway=footway in OSM instead of cycleway/path (e.g.
# "Kiwanis Greenway" and "Dunn Creek Greenway" -- confirmed via the Town's own
# GIS as 8-10 ft paved, Completed facilities, not narrow hiking paths). A
# plain highway=footway is excluded everywhere (NONRIDE) since most footways
# really are pedestrian-only sidewalks -- but that wrongly treats a real,
# named, town-built greenway the same as a random sidewalk, forcing detours
# around perfectly rideable trail. Cross-reference footways against the
# Town's authoritative greenways.geojson + mup.geojson (the same source
# build_map.py already trusts for its "permitted" greenway/MUP layers): a
# footway that substantially coincides with a mapped greenway/MUP is treated
# as a path, everywhere, same as a cycleway.
_gw_official = gpd.read_file("greenways.geojson")
_mup_official = gpd.read_file("mup.geojson")
_gw_buf = unary_union(pd.concat([_gw_official, _mup_official])[["geometry"]]
                       .to_crs(PROJ).geometry.buffer(15).values)
_foot_cand = edges["hw"].isin(("footway", "pedestrian"))
edges["is_greenway_footway"] = False
edges.loc[_foot_cand, "is_greenway_footway"] = _ep.loc[edges[_foot_cand].index, "geometry"].apply(
    lambda g: g.intersection(_gw_buf).length >= 0.5 * g.length).values
edges["is_path"] = edges["is_path"] | edges["is_greenway_footway"]
print(f"footway segments reclassified as rideable greenway/path (matched "
      f"Town GIS): {edges['is_greenway_footway'].sum()} edges, "
      f"{edges.loc[edges['is_greenway_footway'], 'len_mi'].sum():.1f} mi")

# ---- parking lots: OSM tags parking-lot driving aisles and lot-entrance
# driveways as highway=service (service=parking_aisle / driveway / alley /
# drive-through), which build_islands.py's own NONRIDE excludes wholesale --
# meaning a store whose only OSM connectivity is through a lot has NO
# routable node near it at all, so a click snaps to the distant public road
# instead. Un-exclude service roads UNLESS OSM marks them private/no access
# (residential driveways are overwhelmingly access=private; store lots and
# their entrance driveways are not) -- so a private homeowner's driveway
# still isn't routed through, but a shopping-center lot is. Real-world lot
# speeds are far below any posted/inferred road speed, so give them a low
# default (10 mph) instead of the generic 30 mph service-road fallback,
# UNLESS an actual posted/tagged speed exists (rare, but respected).
def _head(v):
    return v[0] if isinstance(v, list) else v


edges["service1"] = edges["service"].map(_head)
edges["access1"] = edges["access"].map(_head)
_is_service = edges["hw"] == "service"

# Manually-excluded private drives that OSM doesn't (yet) tag as private. OSM
# way 18889026 is the "no thru traffic" private cut-through between South White
# St and South Main St -- untagged in OSM, so the access rule below can't catch
# it, and the router was sending riders down it (the article's route avoids it
# by skipping every service road). Drop it here until it's tagged in OSM and the
# pinned graph is re-fetched; then this override becomes a harmless no-op.
PRIVATE_WAYS = {18889026}


def _is_private_way(o):
    return any(i in PRIVATE_WAYS for i in (o if isinstance(o, list) else [o]))


# access=destination is "no through traffic" -- not a valid cut-through, so it's
# excluded too. access=customers is NOT excluded: those are store parking lots we
# specifically want reachable (that's the whole point of routing through lots).
edges["is_lot"] = _is_service & ~edges["access1"].isin(["private", "no", "destination"]) \
    & ~edges["osmid"].map(_is_private_way)
_svc_mph = edges["maxspeed"].map(lambda v: mph(v))
_svc_needs_default = edges["is_lot"] & edges["posted"].isna() & _svc_mph.isna()
edges.loc[_svc_needs_default, "speed"] = 10

NONRIDE_STRICT = NONRIDE - {"service"}

# ---- master traversability: union of all three tiers (what to export) -----
# legal (today):  path | bikelane | speed<=25 | (not intown & speed<=45 & not freeway)
# safe:           path | bikelane | speed<=25 | (sidewalk & not freeway)
# least_unsafe:   safe | (25 < speed < 45 & not freeway)
#
# Legal today does NOT include sidewalk riding -- Chapter 30 bars micromobility
# from sidewalks, so a >25 mph road that's only usable via its sidewalk is not
# legal (that stays in the SAFE tier, which is about physical safety). The
# jurisdiction exemption (Chapter 30 doesn't restrict speed outside the
# corporate limits) is honored but CAPPED at 45 mph, and freeways are excluded
# outright -- NC law bars bikes from limited-access highways independent of
# Chapter 30, and there's no shoulder data to say a fast freeway ramp is any
# safer than the mainline.
_legal_outside = (~edges["intown"]) & (edges["speed"] <= 45) & (~edges["is_freeway"])
_legal = edges["is_path"] | edges["bikelane"] | (edges["speed"] <= 25) | _legal_outside
_safe = edges["is_path"] | edges["bikelane"] | (edges["speed"] <= 25) \
    | (edges["sidewalk_safe"] & ~edges["is_freeway"])
_least_unsafe_extra = (edges["speed"] > 25) & (edges["speed"] < 45) & (~edges["is_freeway"])
_blocked = (edges["hw"].isin(NONRIDE_STRICT) & ~edges["is_greenway_footway"]) \
    | (_is_service & ~edges["is_lot"])
# export the union of everything any tier can traverse -- sidewalk edges are
# kept (the SAFE tier needs them) even though they're no longer "legal".
edges["trav_master"] = (~_blocked) & (_legal | _safe | _least_unsafe_extra)
# (footway/steps/pedestrian/construction/proposed/track, plus private/no-
# access service roads, are never rideable in any tier.)

print(f"parking-lot / driveway mileage newly opened up (service, not "
      f"private/no access): {edges.loc[edges['is_lot'], 'len_mi'].sum():.1f} mi "
      f"({edges['is_lot'].sum()} edges)")

_roadonly = edges["hw"].isin(ROAD)
print(f"legal-tier-only road miles (jurisdiction exemption outside town, not "
      f"otherwise safe): {edges.loc[_roadonly & _legal & ~edges['sidewalk_safe'] & ~edges['bikelane'] & (edges['speed'] > 25) & (~edges['intown']), 'len_mi'].sum():.1f} mi")
print(f"least-unsafe-only bridging road miles (26-44 mph, no bike lane/sidewalk): "
      f"{edges.loc[_roadonly & _least_unsafe_extra & ~_legal, 'len_mi'].sum():.1f} mi")


def clean_name(v):
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v)
    return s if s and s != "nan" else None


def line_coords(geom):
    """[[lat, lon], ...] for a LineString (take the longest part of a
    MultiLineString, same convention as nc_speed._ls)."""
    if geom.geom_type == "MultiLineString":
        geom = max(geom.geoms, key=lambda g: g.length)
    return [[round(y, 5), round(x, 5)] for x, y in geom.coords]


rt = edges[edges["trav_master"]].copy()
rt["geom_simplified"] = rt.to_crs(PROJ).geometry.simplify(3).to_crs(4326)

out_edges = []
for _, r in rt.iterrows():
    out_edges.append({
        "u": int(r["u"]), "v": int(r["v"]),
        "len": float(r.get("length") or 0.0),
        "name": clean_name(r.get("name")),
        "poly": line_coords(r["geom_simplified"]),
        "path": bool(r["is_path"]), "bikelane": bool(r["bikelane"]),
        "speed": int(r["speed"]), "sidewalk": bool(r["sidewalk_safe"]),
        "freeway": bool(r["is_freeway"]), "intown": bool(r["intown"]),
        "lot": bool(r["is_lot"]),
    })

used_nodes = {e["u"] for e in out_edges} | {e["v"] for e in out_edges}

# ---- intersection-gap snap connectors, recomputed for the master node set --
_np_all = ox.graph_to_gdfs(G, edges=False).to_crs(PROJ)
_np_all = _np_all[_np_all.index.isin(used_nodes)].reset_index()[["osmid", "geometry"]]
_buf_all = _np_all.copy()
_buf_all["geometry"] = _buf_all.geometry.buffer(SNAP_M)
_pr_all = gpd.sjoin(_buf_all, _np_all, predicate="intersects")
SNAP_PAIRS_ALL = [(a, b) for a, b in zip(_pr_all["osmid_left"], _pr_all["osmid_right"]) if a < b]

snap_added = 0
for a, b in SNAP_PAIRS_ALL:
    if a in used_nodes and b in used_nodes:
        out_edges.append({"u": int(a), "v": int(b), "len": 0.0, "name": None,
                          "poly": [], "snap": True})
        snap_added += 1

# ---- marked-crosswalk connectors: let a bike route over a marked crosswalk
# where a barrier road (esp. a freeway/trunk you can neither ride along nor
# share an intersection node with) would otherwise split the rideable network.
# crosswalk_connectors() is the shared helper defined in build_islands.py, so
# the router and the reachability map agree on this connectivity. A crossing is
# usable in every tier (kind 'crossing' in build_router.py's classify()). --
cross_added = 0
for a, b, span in crosswalk_connectors(used_nodes):
    out_edges.append({"u": int(a), "v": int(b), "len": float(span), "name": None,
                      "poly": [], "cross": True})
    cross_added += 1

# ---- node coordinates ---------------------------------------------------
node_gdf = ox.graph_to_gdfs(G, edges=False)
node_gdf = node_gdf[node_gdf.index.isin(used_nodes)]
out_nodes = {str(idx): [round(row.geometry.y, 5), round(row.geometry.x, 5)]
             for idx, row in node_gdf.iterrows()}

for e in out_edges:
    if e.get("snap") or e.get("cross"):
        e["poly"] = [out_nodes[str(e["u"])], out_nodes[str(e["v"])]]

b = juris.total_bounds  # [minx, miny, maxx, maxy] in EPSG:4326 (lon, lat)
result = {
    "bbox": [round(b[0], 5), round(b[1], 5), round(b[2], 5), round(b[3], 5)],
    "nodes": out_nodes,
    "edges": out_edges,
}

out_path = "../wf-route-graph.json"
with open(out_path, "w") as fh:
    json.dump(result, fh, separators=(",", ":"))

print(f"saved {out_path}: {len(out_nodes)} nodes, {len(out_edges)} edges "
      f"({snap_added} snap connectors, {cross_added} crosswalk connectors)")
