#!/usr/bin/env python3
"""Export the routable graph as compact JSON for the client-side router
(build_router.py), which offers THREE routing tiers:

  legal        -- Chapter 30 + the sidewalk-legalization reading, including
                  the town's jurisdictional quirk that speed is unrestricted
                  outside the corporate limits -- but CAPPED at 45 mph: a
                  road over 45 with no bike lane/sidewalk is never legal here,
                  in town or out, and freeways are excluded outright
                  regardless of posted speed. (Differs from build_islands.py's
                  own trav_all_sw, which has no such cap -- that reading is
                  used for the main map's reachability analysis, not here.)
                  Legal is still not the same as safe: a 45 mph road with no
                  bike lane outside town is legal but not safe.
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
              | {"u": id, "v": id, "len": 0, "snap": true, "poly": [...]}, ...]
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
edges["is_lot"] = _is_service & ~edges["access1"].isin(["private", "no"])
_svc_mph = edges["maxspeed"].map(lambda v: mph(v))
_svc_needs_default = edges["is_lot"] & edges["posted"].isna() & _svc_mph.isna()
edges.loc[_svc_needs_default, "speed"] = 10

NONRIDE_STRICT = NONRIDE - {"service"}

# ---- master traversability: union of all three tiers (what to export) -----
# legal:        path | bikelane | speed<=25 | sidewalk | (not intown & speed<=45 & not freeway)
# safe:         path | bikelane | speed<=25 | (sidewalk & not freeway)
# least_unsafe: safe | (25 < speed < 45 & not freeway)
#
# The jurisdiction exemption (Chapter 30 doesn't restrict speed outside the
# corporate limits) is now CAPPED at 45 mph: a road over 45 with no bike lane
# or sidewalk is never "legal" here, in town or out. Freeways are excluded
# from the exemption outright regardless of their posted speed -- NC law bars
# bikes from limited-access highways independent of Chapter 30, and there's
# no shoulder data to say a fast freeway ramp is any safer than the mainline.
_legal_outside = (~edges["intown"]) & (edges["speed"] <= 45) & (~edges["is_freeway"])
_legal = edges["is_path"] | edges["bikelane"] | (edges["speed"] <= 25) \
    | edges["sidewalk_safe"] | _legal_outside
_least_unsafe_extra = (edges["speed"] > 25) & (edges["speed"] < 45) & (~edges["is_freeway"])
_blocked = edges["hw"].isin(NONRIDE_STRICT) | (_is_service & ~edges["is_lot"])
edges["trav_master"] = (~_blocked) & (_legal | _least_unsafe_extra)
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

# ---- node coordinates ---------------------------------------------------
node_gdf = ox.graph_to_gdfs(G, edges=False)
node_gdf = node_gdf[node_gdf.index.isin(used_nodes)]
out_nodes = {str(idx): [round(row.geometry.y, 5), round(row.geometry.x, 5)]
             for idx, row in node_gdf.iterrows()}

for e in out_edges:
    if e.get("snap"):
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
      f"({snap_added} snap connectors)")
