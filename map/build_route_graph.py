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

# ---- footways as sidewalk links (safe/least_unsafe only, never legal) -------
# The Legal tier bars footways and that is CORRECT: Ch.30 bars sidewalk riding,
# and Wake Forest's greenways connect to the street grid almost exclusively via
# sidewalks (measured: of the footway edges touching a greenway/path node, 85%
# are footway=sidewalk and 86% run within 18 m of a road centerline). So "you
# cannot legally ride from Rogers Road onto Smith Creek Greenway" is a finding,
# not a bug -- Legal keeps refusing them, and only a footway the Town's own GIS
# calls a greenway (is_greenway_footway, below) becomes legal.
#
# But safe/least_unsafe were INCONSISTENT: classify() already puts a rider on a
# >25 mph road's sidewalk (kind 'sidewalk_fast'), then refused the sidewalk that
# links that same road to a greenway -- stranding e.g. Smith Creek Greenway
# behind a 1034 m detour around a real 100 m footway bridge. Those two tiers
# weigh physical safety, and a sidewalk is low-stress, so allow footways there.
#
# Gated on a rideable surface: half the ambiguous footway mileage is
# surface=dirt (genuine hiking trail, not rideable), and bicycle=no/private is
# an explicit prohibition. Reading surface/bicycle at all is only possible
# because the graph is now re-pinned with them in useful_tags_way.
UNRIDEABLE_SURFACE = {"dirt", "ground", "sand", "grass", "mud", "earth", "woodchips"}


def _head_tag(v):
    """First value of a possibly-list OSM tag, as a str; None if absent/NaN."""
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v)


edges["surface1"] = (edges["surface"].map(_head_tag) if "surface" in edges.columns
                     else pd.Series(None, index=edges.index, dtype=object))
edges["bicycle1"] = (edges["bicycle"].map(_head_tag) if "bicycle" in edges.columns
                     else pd.Series(None, index=edges.index, dtype=object))
edges["is_footlink"] = (edges["hw"].isin(("footway", "pedestrian", "steps"))
                        & ~edges["surface1"].isin(UNRIDEABLE_SURFACE)
                        & ~edges["bicycle1"].isin(["no", "private"]))
print(f"footway/sidewalk links opened to the safe + least-unsafe tiers "
      f"(rideable surface, not bicycle=no): {edges['is_footlink'].sum()} edges, "
      f"{edges.loc[edges['is_footlink'], 'len_mi'].sum():.1f} mi")

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
    | (edges["sidewalk_safe"] & ~edges["is_freeway"]) | edges["is_footlink"]
_least_unsafe_extra = (edges["speed"] > 25) & (edges["speed"] < 45) & (~edges["is_freeway"])
# footway/pedestrian/steps stay blocked for the LEGAL tier (classify() gates
# kind 'sidewalk_link' on tier !== 'legal'), but are no longer dropped from the
# export -- the safe/least_unsafe tiers need them to reach greenways at all.
_blocked = (edges["hw"].isin(NONRIDE_STRICT) & ~edges["is_greenway_footway"]
            & ~edges["is_footlink"]) \
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
        # 0.1 m is far finer than the underlying geometry; the raw float ran to
        # ~15 significant digits and cost ~0.7 MB of pure noise.
        "len": round(float(r.get("length") or 0.0), 1),
        "name": clean_name(r.get("name")),
        "poly": line_coords(r["geom_simplified"]),
        "path": bool(r["is_path"]), "bikelane": bool(r["bikelane"]),
        "speed": int(r["speed"]), "sidewalk": bool(r["sidewalk_safe"]),
        "freeway": bool(r["is_freeway"]), "intown": bool(r["intown"]),
        "lot": bool(r["is_lot"]),
        # a footway/sidewalk usable only in the safe + least_unsafe tiers
        "foot": bool(r["is_footlink"] and not r["is_path"]),
    })

# ---- drop reverse duplicates ------------------------------------------------
# osmnx's MultiDiGraph carries a two-way street as BOTH u->v and v->u, and
# graph_to_gdfs hands back every directed edge -- so ~49% of the exported edge
# payload was a byte-for-byte reverse of another edge. The router never needed
# it: buildGraph() pushes each edge into the adjacency in BOTH directions
# itself, and drawRoute() re-orients the polyline per step
# (`String(e.u) === st.from ? coords : coords.slice().reverse()`). Collapse a
# pair only when EVERY other attribute matches too, so genuine parallel ways
# between the same node pair (different name/speed/geometry) both survive.
_before = len(out_edges)
_seen, _dedup = set(), []
for _e in out_edges:
    _key = (min(_e["u"], _e["v"]), max(_e["u"], _e["v"]),
            tuple(sorted((k, str(v)) for k, v in _e.items() if k not in ("u", "v", "poly"))))
    if _key in _seen:
        continue
    _seen.add(_key)
    _dedup.append(_e)
out_edges = _dedup
print(f"reverse-duplicate edges dropped: {_before - len(out_edges)} of {_before} "
      f"({100 * (_before - len(out_edges)) / _before:.0f}%)")

used_nodes = {e["u"] for e in out_edges} | {e["v"] for e in out_edges}
# Nodes reachable WITHOUT setting foot on a footway -- i.e. the node set the
# router had before footways were exported at all. Both connector families below
# are computed over this set FIRST so the Legal tier's connectivity is bit-for-bit
# what it was pre-fix; the footway-only extras are then added, tainted foot=True.
#
# Why this matters (both caught by diffing against a pre-fix control build):
#  * snap/cross connectors are traversable in EVERY tier, so a 0-length snap onto
#    a sidewalk node and back off it handed the LEGAL tier a free bridge through
#    the sidewalk network -- 1724 footway nodes were bridging >=2 road nodes,
#    inflating Legal's main component by ~4000 nodes.
#  * crosswalk_connectors() snaps each crossing END to its NEAREST routable node.
#    Once sidewalk nodes existed they were usually nearer than the road node, so
#    372 of the original 474 connectors silently RE-TARGETED off the road network
#    and Legal LOST real connectivity it used to have.
road_nodes = ({e["u"] for e in out_edges if not e["foot"]}
              | {e["v"] for e in out_edges if not e["foot"]})


def _snap_pairs(nodeset):
    np_ = ox.graph_to_gdfs(G, edges=False).to_crs(PROJ)
    np_ = np_[np_.index.isin(nodeset)].reset_index()[["osmid", "geometry"]]
    buf = np_.copy()
    buf["geometry"] = buf.geometry.buffer(SNAP_M)
    pr = gpd.sjoin(buf, np_, predicate="intersects")
    return {(a, b) for a, b in zip(pr["osmid_left"], pr["osmid_right"]) if a < b}


SNAP_ROAD = _snap_pairs(road_nodes)
SNAP_PAIRS_ALL = _snap_pairs(used_nodes)

snap_added = snap_foot = 0
for a, b in SNAP_PAIRS_ALL:
    tainted = (a, b) not in SNAP_ROAD
    out_edges.append({"u": int(a), "v": int(b), "len": 0.0, "name": None,
                      "poly": [], "snap": True, "foot": tainted})
    snap_added += 1
    snap_foot += tainted

# ---- marked-crosswalk connectors: let a bike route over a marked crosswalk
# where a barrier road (esp. a freeway/trunk you can neither ride along nor
# share an intersection node with) would otherwise split the rideable network.
# crosswalk_connectors() is the shared helper defined in build_islands.py, so
# the router and the reachability map agree on this connectivity. A crossing is
# usable in every tier UNLESS it only exists by landing on a sidewalk node
# (foot=True), which the Legal tier can't use -- see the road_nodes note above.
CROSS_ROAD = crosswalk_connectors(road_nodes)
_cross_road_keys = {(min(a, b), max(a, b)) for a, b, _ in CROSS_ROAD}
cross_added = cross_foot = 0
for a, b, span in CROSS_ROAD:
    out_edges.append({"u": int(a), "v": int(b), "len": float(span), "name": None,
                      "poly": [], "cross": True, "foot": False})
    cross_added += 1
for a, b, span in crosswalk_connectors(used_nodes):
    if (min(a, b), max(a, b)) in _cross_road_keys:
        continue
    out_edges.append({"u": int(a), "v": int(b), "len": float(span), "name": None,
                      "poly": [], "cross": True, "foot": True})
    cross_added += 1
    cross_foot += 1

# ---- node coordinates ---------------------------------------------------
node_gdf = ox.graph_to_gdfs(G, edges=False)
node_gdf = node_gdf[node_gdf.index.isin(used_nodes)]
out_nodes = {str(idx): [round(row.geometry.y, 5), round(row.geometry.x, 5)]
             for idx, row in node_gdf.iterrows()}

# ---- slim the payload ------------------------------------------------------
# A snap/cross connector is a straight line between its two nodes, so its poly
# was just a copy of two entries already in `nodes` -- drawRoute() rebuilds it
# from the node coords instead. And seven per-edge booleans, written out in full
# on every edge even when false, cost more than the geometry did: pack them into
# one integer, omitted entirely when nothing is set. FLAG_BITS must stay in sync
# with the F_* constants in build_router.py's classify().
FLAG_BITS = {"path": 1, "bikelane": 2, "sidewalk": 4, "freeway": 8,
             "intown": 16, "lot": 32, "foot": 64}
for e in out_edges:
    if e.get("snap") or e.get("cross"):
        e.pop("poly", None)
    # connector spans are computed, not measured, so they carried the same float
    # noise the real edges did; drop the trailing ".0" while we're here.
    _l = round(e["len"], 1)
    e["len"] = int(_l) if _l == int(_l) else _l
    f = 0
    for _name, _bit in FLAG_BITS.items():
        if e.pop(_name, False):
            f |= _bit
    if f:
        e["f"] = f
    if e.get("name") is None:
        e.pop("name", None)

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
      f"({snap_added} snap connectors, {snap_foot} of them footway-only; "
      f"{cross_added} crosswalk connectors, {cross_foot} footway-only). "
      f"Legal sees {snap_added - snap_foot} snap + {cross_added - cross_foot} crosswalk "
      f"connectors -- unchanged from before footways were exported.")
