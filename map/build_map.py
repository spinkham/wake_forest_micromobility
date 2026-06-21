#!/usr/bin/env python3
"""Wake Forest micromobility legal-layer map (Phase 1, v2).

Classifies lines PERMITTED vs GATED under Chapter 30 — but ONLY inside the
Town's corporate limits. Chapter 30 is a traffic/police-power ordinance, so it
applies within the corporate limits, not the ETJ and not the unincorporated
enclaves (the "holes"). Facilities outside the corporate limits are drawn
neutral grey ("rule N/A").

Boundary: OpenStreetMap admin_level=8 (corporate limits), made valid, with
interior sliver rings < ~3 acres dropped (digitizing artifacts); genuine
unincorporated enclaves are kept.

Speed: OSM `maxspeed` where tagged (~8% of lines); otherwise inferred from road
class (SPEED_INFER) and flagged as such.
"""
import re
import geopandas as gpd
import pandas as pd
import folium
from shapely import make_valid
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

PROJ = 32617          # UTM 17N (meters)
SQMI = 2.59e6         # m^2 per square mile
HOLE_MIN_SQMI = 0.005  # drop interior rings smaller than ~3 acres (artifacts)

SPEED_INFER = {
    "motorway": 55, "motorway_link": 45, "trunk": 45, "trunk_link": 35,
    "primary": 45, "primary_link": 35, "secondary": 35, "secondary_link": 35,
    "tertiary": 35, "tertiary_link": 30,
    "residential": 25, "living_street": 20, "unclassified": 30, "road": 30,
}


def _polygons(geom):
    t = geom.geom_type
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return list(geom.geoms)
    if t == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out += _polygons(g)
        return out
    return []


def corporate_limits():
    """Load (or build + cache) the cleaned OSM corporate-limits polygon."""
    try:
        return gpd.read_file("corporate_limits.geojson").to_crs(4326)
    except Exception:
        pass
    import osmnx as ox
    ox.settings.use_cache = True
    ox.settings.cache_folder = "osm_cache"
    o = ox.geocode_to_gdf("Wake Forest, North Carolina, USA").to_crs(PROJ)
    polys = _polygons(make_valid(o.geometry.iloc[0]))
    cleaned = []
    for p in polys:
        if p.area / SQMI < 0.01:           # drop sliver exterior parts
            continue
        holes = [r for r in p.interiors if Polygon(r).area / SQMI >= HOLE_MIN_SQMI]
        cleaned.append(Polygon(p.exterior, holes))
    mp = MultiPolygon(cleaned) if len(cleaned) > 1 else cleaned[0]
    out = gpd.GeoDataFrame(geometry=[mp], crs=PROJ).to_crs(4326)
    out.to_file("corporate_limits.geojson", driver="GeoJSON")
    return out


def parse_mph(v):
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0]
    m = re.search(r"(\d+)", str(v).lower())
    if not m:
        return None
    n = int(m.group(1))
    if "km" in str(v).lower():
        n = round(n * 0.621)
    return n


def road_speed(hw, maxspeed):
    sp = parse_mph(maxspeed)
    if sp is not None:
        return sp, "posted"
    return SPEED_INFER.get(hw, 30), "inferred"


# ---- load -------------------------------------------------------------------
gw = gpd.read_file("greenways.geojson").to_crs(4326)
mup = gpd.read_file("mup.geojson").to_crs(4326)
osm = gpd.read_file("osm_highways.geojson").to_crs(4326)
blanes = gpd.read_file("town_bike_lanes.geojson").to_crs(4326)   # Town CTP on-road bike lanes
blanes_ex = blanes[blanes["Status"] == "Existing"]
blanes_pr = blanes[blanes["Status"] == "Proposed"]
juris = corporate_limits()
juris_geom = unary_union(juris.geometry.values)
if "maxspeed" not in osm.columns:
    osm["maxspeed"] = None

# jurisdiction test on a point guaranteed to lie on each line
osm["in_town"] = osm.geometry.representative_point().within(juris_geom)

# NCDOT posted speeds joined onto road lines (along-segment match; see nc_speed)
import nc_speed
ROAD = set(SPEED_INFER)
osm["hw"] = osm["highway"].map(lambda h: h[0] if isinstance(h, list) else h)
ncdot = gpd.read_file("ncdot_speed.geojson").to_crs(PROJ)
_rm = osm["hw"].isin(ROAD)
osm["posted"] = nc_speed.assign_posted(osm, ncdot, _rm, PROJ)


def edge_speed(row):
    """NCDOT posted -> OSM maxspeed -> inferred by class."""
    if pd.notna(row["posted"]):
        return int(row["posted"])
    s = parse_mph(row.get("maxspeed"))
    return s if s is not None else SPEED_INFER.get(row["hw"], 30)


osm["speed"] = osm.apply(edge_speed, axis=1)


def categorize(row):
    if not row["in_town"]:
        return "outside", None
    hw = row["hw"]
    if hw in ("footway", "steps", "pedestrian"):
        return "sidewalk_gated", None
    if hw == "cycleway":
        return "bikelane", None
    if hw == "path":
        return "path_ambiguous", None
    if hw in ("service", "construction", "proposed", "track"):
        return None, None
    sp = row["speed"]
    return ("street_permitted" if sp <= 25 else "road_gated"), sp


res = osm.apply(categorize, axis=1)
osm["cat"] = [r[0] for r in res]
osm = osm[osm["cat"].notna()].copy()


def miles(gdf):
    return round(gdf.to_crs(PROJ).length.sum() / 1609.34, 1) if len(gdf) else 0.0


# Classify each bike lane by the speed of the road it sits on. amb = on a >25 mph
# road (legally ambiguous: §30-90(b)(2) permits "bicycle lanes" but bars >25 mph
# roads). The rule-interpretation checkbox keys off this flag.
_roads = osm[osm["hw"].isin(ROAD)][["speed", "geometry"]].to_crs(PROJ)
def _mark_amb(gdf):
    g = gdf.copy()
    if not len(g):
        g["amb"] = False
        return g
    pts = g.to_crs(PROJ).copy()
    pts["geometry"] = pts.geometry.representative_point()
    jn = gpd.sjoin_nearest(pts[["geometry"]], _roads, how="left", max_distance=25, distance_col="d")
    jn = jn.sort_values("d").groupby(level=0).first()
    g["amb"] = (jn["speed"].reindex(g.index) > 25).fillna(False).values
    return g
blanes_ex = _mark_amb(blanes_ex)
blanes_pr = _mark_amb(blanes_pr)
blanes_ex_safe = blanes_ex[~blanes_ex["amb"]]
blanes_ex_amb = blanes_ex[blanes_ex["amb"]]
try:
    sw_min = gpd.read_file("sidewalk_minimal.geojson").to_crs(4326)
except Exception:
    sw_min = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=4326)
try:
    sw_knee = gpd.read_file("sidewalk_knee.geojson").to_crs(4326)
except Exception:
    sw_knee = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=4326)
try:
    sw_parity = gpd.read_file("sidewalk_parity.geojson").to_crs(4326)
except Exception:
    sw_parity = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=4326)

# (gdf, name, color, weight, dash, default_on)
LAYERS = [
    (gw, "Greenways (permitted)", "#1a9641", 4, None, True),
    (mup, "Multi-Use Paths (permitted)", "#1f78b4", 4, None, True),
    (osm[osm.cat == "bikelane"], "Cycleways / cycle tracks (permitted)", "#2c7fb8", 3, None, True),
    (blanes_ex_safe, "On-road bike lanes ≤25 mph (permitted)", "#984ea3", 3, None, True),
    (blanes_ex_amb, "Bike lanes on >25 mph roads (ambiguous)", "#e08214", 3.5, None, False),
    (blanes_pr, "Bike lanes — proposed (not yet built)", "#984ea3", 2.2, "5,5", False),
    (sw_min, "Sidewalks to sign: minimal — connect (4.9 mi)", "#e7298a", 4, "1,2", False),
    (sw_knee, "Sidewalks to sign: + to the knee (cuts most detours)", "#1b9e77", 4, "1,2", False),
    (sw_parity, "Sidewalks to sign: + to full route parity (0 detour)", "#3690c0", 4, "1,2", False),
    (osm[osm.cat == "street_permitted"], "Streets ≤25 mph (permitted)", "#a6d96a", 1.5, None, True),
    (osm[osm.cat == "road_gated"], "Roads >25 mph (GATED)", "#d7191c", 3, None, True),
    (osm[osm.cat == "sidewalk_gated"], "Sidewalks / footways (GATED)", "#fdae61", 1.3, "4,4", True),
    (osm[osm.cat == "outside"], "Outside Town limits — rule N/A", "#c4c4c4", 1.0, None, True),
    (osm[osm.cat == "path_ambiguous"], "Paths — ambiguous", "#888888", 1.6, "2,4", False),
]

# ---- map --------------------------------------------------------------------
b = juris.total_bounds
m = folium.Map(tiles="CartoDB positron", control_scale=True)
m.fit_bounds([[b[1], b[0]], [b[3], b[2]]])

# free aerial basemaps (choose in layer control). Esri World Imagery is omitted:
# its terms + Maxar redistribution limits aren't clean for a public site.
folium.TileLayer(
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
    attr="USGS The National Map", name="Aerial — USGS Imagery",
    overlay=False, control=True, max_zoom=16).add_to(m)
folium.TileLayer("OpenStreetMap", name="OpenStreetMap", overlay=False, control=True).add_to(m)

folium.GeoJson(
    juris, name="Corporate limits",
    style_function=lambda f: {"color": "#222", "weight": 2, "fill": False, "dashArray": "6,4"},
).add_to(m)

stats = {}
js_names = {}
for gdf, name, color, weight, dash, on in LAYERS:
    mi = miles(gdf)
    stats[name] = (len(gdf), mi)
    if len(gdf) == 0:
        continue
    g2 = gdf.copy()
    g2["geometry"] = g2.to_crs(PROJ).geometry.simplify(3).to_crs(4326)
    style = {"color": color, "weight": weight, "opacity": 0.85}
    if dash:
        style["dashArray"] = dash
    cols = ["geometry"] + (["amb"] if "amb" in g2.columns else [])
    fg = folium.FeatureGroup(name=f"{name} — {mi} mi", show=on)
    gj = folium.GeoJson(g2[cols], style_function=lambda f, s=style: s)
    gj.add_to(fg)
    fg.add_to(m)
    js_names[name] = gj.get_name()

# reachability overlay (Phase 2) — ONE layer; the rule-interpretation checkbox
# (top-right) swaps it between the permissive and strict readings via setStyle.
REACH = "null"
try:
    _rg = gpd.read_file("reachability.geojson").to_crs(4326)
    _rg["geometry"] = _rg.to_crs(PROJ).geometry.simplify(3).to_crs(4326)
    _reach_fg = folium.FeatureGroup(name="▸ Reachability (analysis) — set interpretation in box", show=False)
    _reach_gj = folium.GeoJson(
        _rg[["geometry", "role_all", "role_le25", "role_all_sw", "role_le25_sw"]],
        style_function=lambda f: ({"color": "#1a9641", "weight": 3, "opacity": 0.85}
                                  if f["properties"]["role_all"] == "main"
                                  else ({"color": "#d7191c", "weight": 3, "opacity": 0.85}
                                        if f["properties"]["role_all"] == "island"
                                        else {"opacity": 0, "fillOpacity": 0})))
    _reach_gj.add_to(_reach_fg)
    _reach_fg.add_to(m)
    REACH = _reach_gj.get_name()
except Exception as _e:
    print("reachability overlay skipped:", _e)

# sidewalk-exception SIGN POSTS (Option C: begin/end + major cross-streets,
# placed per road side). Points colored by sign type; all off by default.
SIGN_FILES = [
    ("sign_posts.geojson", "Sign posts — minimal set"),
    ("sign_posts_knee.geojson", "Sign posts — knee set"),
    ("sign_posts_parity.geojson", "Sign posts — full-parity set"),
]
SIGN_COLOR = {"begin/end": "#b30000", "major-crossing": "#e08214"}
for fn, nm in SIGN_FILES:
    try:
        sp = gpd.read_file(fn).to_crs(4326)
    except Exception:
        continue
    if len(sp) == 0:
        continue
    nbe = int((sp["sign_type"] == "begin/end").sum())
    nmx = len(sp) - nbe
    fg = folium.FeatureGroup(
        name=f"● {nm} — {len(sp)} posts ({nbe} begin/end + {nmx} crossing)", show=False)
    for _, r in sp.iterrows():
        col = SIGN_COLOR.get(r["sign_type"], "#333333")
        folium.CircleMarker(
            location=[r.geometry.y, r.geometry.x], radius=4, color="#222", weight=1,
            fill=True, fill_color=col, fill_opacity=0.95,
            popup=folium.Popup(f"<b>{r['sign_type']}</b><br>{r.get('streets', '')}", max_width=220),
        ).add_to(fg)
    fg.add_to(m)

lc = folium.LayerControl(collapsed=True)  # collapses to an icon; expands on hover
lc.add_to(m)

# NC OneMap 6-inch orthoimagery (dynamic ArcGIS ImageServer) via esri-leaflet,
# loaded dynamically AFTER Leaflet exists so there is no script-order race.
m.get_root().html.add_child(folium.Element(f"""
<script>
(function() {{
  function ready() {{ return window.L && typeof {m.get_name()} !== 'undefined'
                       && typeof {lc.get_name()} !== 'undefined'; }}
  function addNC() {{
    var nc = L.esri.imageMapLayer({{
      url: 'https://services.nconemap.gov/secure/rest/services/Imagery/Orthoimagery_Latest/ImageServer',
      attribution: 'NC OneMap Orthoimagery (NC 911 Board)', maxZoom: 21
    }});
    {lc.get_name()}.addBaseLayer(nc, 'Aerial — NC OneMap orthos (6 in)');
  }}
  function go() {{
    if (!ready()) {{ return setTimeout(go, 150); }}
    if (window.L.esri) {{ return addNC(); }}
    var s = document.createElement('script');
    s.src = 'https://unpkg.com/esri-leaflet@3.1.0/dist/esri-leaflet.js';
    s.onload = addNC; document.head.appendChild(s);
  }}
  go();
}})();
</script>
"""))

# persistent data attribution in the Leaflet attribution control
m.get_root().html.add_child(folium.Element(f"""
<script>
(function add() {{
  if (typeof {m.get_name()} === 'undefined' || !{m.get_name()}.attributionControl) {{ return setTimeout(add, 150); }}
  {m.get_name()}.attributionControl.addAttribution('Data: © OpenStreetMap contributors (ODbL) · Town of Wake Forest · NCDOT · NC OneMap · USGS');
}})();
</script>
"""))

# rule-interpretation checkbox — rendered INSIDE the layer-control box (below the
# layers, after a divider). Flips permissive/strict and restyles the reachability
# + >25 / proposed bike-lane layers via setStyle. Re-inserted after any list
# rebuild (e.g. when the NC-orthos base layer is added) so it isn't wiped.
m.get_root().html.add_child(folium.Element(f"""
<script>
(function() {{
  var PERM = true, SW = false;
  function init() {{
    if (typeof {m.get_name()} === 'undefined' || typeof L === 'undefined'
        || typeof {REACH} === 'undefined' || typeof {lc.get_name()} === 'undefined') {{
      return setTimeout(init, 150);
    }}
    var REACH = {REACH},
        BAMB = {js_names.get('Bike lanes on >25 mph roads (ambiguous)', 'null')},
        BPROP = {js_names.get('Bike lanes — proposed (not yet built)', 'null')},
        LC = {lc.get_name()};
    function reachStyle(f) {{
      var p = f.properties;
      var r = SW ? (PERM ? p.role_all_sw : p.role_le25_sw) : (PERM ? p.role_all : p.role_le25);
      if (r !== 'main' && r !== 'island') return {{opacity: 0, fillOpacity: 0}};
      return {{color: r === 'main' ? '#1a9641' : '#d7191c', weight: 3, opacity: 0.85}};
    }}
    function ambStyle(f) {{
      return PERM ? {{color: '#984ea3', weight: 3.5, opacity: 0.95}}
                  : {{color: '#e08214', weight: 3.5, opacity: 0.95, dashArray: '5,5'}};
    }}
    function propStyle(f) {{
      if (f.properties.amb && !PERM) return {{color: '#e08214', weight: 2.2, opacity: 0.85, dashArray: '3,4'}};
      return {{color: '#984ea3', weight: 2.2, opacity: 0.8, dashArray: '5,5'}};
    }}
    function apply() {{
      try {{ REACH.setStyle(reachStyle); }} catch (e) {{}}
      try {{ BAMB.setStyle(ambStyle); }} catch (e) {{}}
      try {{ BPROP.setStyle(propStyle); }} catch (e) {{}}
    }}
    function ensureSection() {{
      var list = LC.getContainer().querySelector('.leaflet-control-layers-list');
      if (!list || list.querySelector('#interpChk')) return;
      var sec = L.DomUtil.create('div', 'wf-interp', list);
      sec.innerHTML = '<div style="border-top:1px solid #bbb;margin:7px 0 5px"></div>' +
        '<label style="cursor:pointer;display:block"><input type="checkbox" id="interpChk"' + (PERM ? ' checked' : '') + '> <b>Bike lanes count on &gt;25&nbsp;mph roads</b></label>' +
        '<div style="color:#888;font-size:10px;margin:2px 0 5px 2px">rule interpretation — affects reachability &amp; bike-lane layers</div>' +
        '<label style="cursor:pointer;display:block"><input type="checkbox" id="swChk"' + (SW ? ' checked' : '') + '> <b>Sidewalks usable on &gt;25&nbsp;mph roads (no bike lane)</b></label>' +
        '<div style="color:#888;font-size:10px;margin:2px 0 0 2px">reachability only — reconnects ~69% of otherwise-stranded miles</div>';
      L.DomEvent.disableClickPropagation(sec);
      list.querySelector('#interpChk').addEventListener('change', function(e) {{ PERM = e.target.checked; apply(); }});
      list.querySelector('#swChk').addEventListener('change', function(e) {{ SW = e.target.checked; apply(); }});
    }}
    var _u = LC._update;
    LC._update = function() {{ _u.call(this); ensureSection(); }};
    ensureSection();
    apply();
  }}
  init();
}})();
</script>
"""))

perm_off = round(sum(stats[n][1] for n in (
    "Greenways (permitted)", "Multi-Use Paths (permitted)",
    "Cycleways / cycle tracks (permitted)", "On-road bike lanes ≤25 mph (permitted)")), 1)
gated_rd = stats["Roads >25 mph (GATED)"][1]
gated_sw = stats["Sidewalks / footways (GATED)"][1]
rows = "".join(
    f'<div><span style="display:inline-block;width:22px;border-top:{w}px '
    f'{"dashed" if d else "solid"} {c};vertical-align:middle;margin-right:6px"></span>'
    f'{n.split(" — ")[0]} <b>{stats[n][1]} mi</b></div>'
    for (g, n, c, w, d, o) in LAYERS)
legend = f"""
<div id="wf-legend" style="position:fixed;bottom:24px;left:24px;z-index:9999;background:#fff;
padding:8px 12px;border:1px solid #999;border-radius:6px;font:12px/1.5 sans-serif;
box-shadow:0 1px 5px rgba(0,0,0,.3);max-width:340px">
<div style="font-weight:700;cursor:pointer;user-select:none" title="click to collapse / expand"
 onclick="var b=document.getElementById('wf-legend-body'),t=document.getElementById('wf-legend-tog'),h=b.style.display==='none';b.style.display=h?'block':'none';t.textContent=h?'▾':'▸';">
<span id="wf-legend-tog">▾</span> Where micromobility can ride — Wake Forest Ch.30</div>
<div id="wf-legend-body" style="margin-top:6px">
{rows}
<hr style="margin:8px 0">
<div style="color:#555"><b>{perm_off} mi</b> permitted off-street (greenway/MUP/bike)
&middot; <b>{gated_rd} mi</b> roads &gt;25mph + <b>{gated_sw} mi</b> sidewalks gated
<i>(within corporate limits)</i>.</div>
<div style="color:#888;margin-top:5px;font-size:11px">Ch.30 is a traffic ordinance: it
applies inside the corporate limits only — not the ETJ or the unincorporated
enclaves (grey = outside, rule N/A). Boundary: OSM. Facilities: Town of Wake Forest
ArcGIS. Roads/sidewalks: OpenStreetMap; speed posted where tagged (~8%), else
inferred by road class. Not legal advice.</div>
</div>
</div>"""
m.get_root().html.add_child(folium.Element(legend))

out = "../wake-forest-micromobility-map.html"
m.save(out)
print("saved", out)
print("\n=== category stats (features, miles) ===")
for n, (cnt, mi) in stats.items():
    print(f"  {n:42s} {cnt:5d} feats  {mi:7.1f} mi")
print(f"\nPermitted off-street (in town): {perm_off:.1f} mi")
print(f"Gated (in town): roads>25 {gated_rd:.1f} mi + sidewalks {gated_sw:.1f} mi")
print(f"Outside corporate limits (rule N/A): {stats['Outside Town limits — rule N/A'][1]:.1f} mi")
