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
import os
import re
from html import escape
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

# Some of the town's own paved, shared-use greenway trails are tagged
# highway=footway in OSM instead of cycleway/path (e.g. Kiwanis Greenway,
# Dunn Creek Greenway -- confirmed via the Town's own GIS as 8-10 ft paved,
# Completed facilities, not narrow hiking paths). A plain footway is
# GATED here (right for an actual sidewalk), but that wrongly gates a real,
# named, town-built greenway too. Cross-reference footways against the
# Town's own greenways/mup layers (already loaded above as gw/mup): a
# footway that substantially coincides with a mapped greenway/MUP is
# EXCLUDED from the OSM-derived categories below (not reclassified as
# "bikelane") -- the dedicated Greenways/MUP layers already draw and count
# its real mileage from the Town's own data, so counting it again here
# would double it in the map and in the legend's permitted-mileage total.
_gw_buf = unary_union(pd.concat([gw, mup])[["geometry"]].to_crs(PROJ).geometry.buffer(15).values)
_foot_cand = osm["hw"].isin(("footway", "pedestrian"))
osm["is_greenway_footway"] = False
osm.loc[_foot_cand, "is_greenway_footway"] = osm.loc[_foot_cand].to_crs(PROJ).geometry.apply(
    lambda g: g.intersection(_gw_buf).length >= 0.5 * g.length).values


def categorize(row):
    if not row["in_town"]:
        return "outside", None
    if row["is_greenway_footway"]:
        return None, None
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
# max_zoom 22 lets layers "overzoom" (client-side upscale) past their own native
# level; each base layer sets max_native_zoom so it stops fetching real tiles
# there and scales those up instead of vanishing / 404ing.
m = folium.Map(tiles=None, control_scale=True, max_zoom=22)
m.fit_bounds([[b[1], b[0]], [b[3], b[2]]])

# default street basemap (added first => shown by default)
folium.TileLayer("CartoDB positron", name="CartoDB Positron",
                 overlay=False, control=True, max_native_zoom=20, max_zoom=22).add_to(m)

# alternate street basemap (choose in layer control); show=False so only CartoDB
# is on the map at load (folium adds every base layer to the map; without this
# the last one wins). The only aerial imagery is the NC 6-inch layer wired in
# below; redundant aerials (USGS, Esri World Imagery) are intentionally omitted.
folium.TileLayer("OpenStreetMap", name="OpenStreetMap", overlay=False, control=True,
                 show=False, max_native_zoom=19, max_zoom=22).add_to(m)

# The sole aerial imagery layer is wired in below (injected JS, after the layer
# control exists). It serves the locally built NC 6-inch tile cache
# (build_imagery_cache.py; max_native_zoom=20 so it overzooms 20->22 client-side
# with no extra tiles) and falls back PER TILE to the live NC OneMap ImageServer
# for anything not cached -- town edges, or every tile when no cache is built --
# so it works with or without a local cache. URL is relative to the saved HTML
# (repo root); tiles live under map/tiles/.
HAS_CACHE = os.path.isdir("tiles")
print("imagery cache present: serving map/tiles/ with per-tile NC OneMap fallback"
      if HAS_CACHE else
      "no map/tiles/ cache: aerial layer pulls every tile live from NC OneMap "
      "(build it with build_imagery_cache.py for fast local tiles)")

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
            popup=folium.Popup(  # escape OSM-derived values (street names contain &, etc.)
                f"<b>{escape(str(r['sign_type']))}</b><br>{escape(str(r.get('streets', '')))}", max_width=220),
        ).add_to(fg)
    fg.add_to(m)

lc = folium.LayerControl(collapsed=True)  # collapses to an icon; expands on hover
lc.add_to(m)

# Site header bar — matches the article page at /articles/june2026/ so navigation
# is consistent across the site. A slim FIXED overlay across the top of the map
# (Map / Article / Data); Leaflet's top controls are nudged down so the bar never
# covers them. Pure CSS + links: no JS, no external assets (keeps SRI clean).
m.get_root().header.add_child(folium.Element(
    '''<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>&#x1F6B2;</text></svg>">'''
))
# Theme bootstrap: runs synchronously in <head>, before the (large) body
# paints, so there's no flash of the wrong theme. Manual choice (localStorage)
# wins; absent that, follows the OS preference live via the media-query
# listener. .wf-dark on <html> is the single source of truth every dark-mode
# rule below keys off.
m.get_root().header.add_child(folium.Element("""
<script>
(function() {
  try {
    var stored = localStorage.getItem('wfTheme');
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    function apply(dark) { document.documentElement.classList.toggle('wf-dark', dark); }
    apply(stored ? stored === 'dark' : mq.matches);
    if (!stored) { mq.addEventListener('change', function(e) { apply(e.matches); }); }
  } catch (e) {}
})();
</script>
"""))
m.get_root().html.add_child(folium.Element("""
<style>
:root.wf-dark{color-scheme:dark}
.wf-site-bar{position:fixed;top:0;left:0;right:0;z-index:1001;display:flex;
  align-items:center;justify-content:space-between;gap:1rem;
  padding:.6rem clamp(1rem,4vw,1.5rem);
  font:15px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:rgba(255,255,255,.92);border-bottom:1px solid #e4e7ea;
  -webkit-backdrop-filter:saturate(1.3) blur(8px);backdrop-filter:saturate(1.3) blur(8px)}
.wf-site-bar .brand{color:#1b1d20;text-decoration:none;font-weight:700}
.wf-site-bar nav{display:flex;align-items:center;gap:1.15rem}
.wf-site-bar nav a{color:#5b6168;text-decoration:none}
.wf-site-bar nav a:hover{color:#137a37}
.wf-site-bar nav a[aria-current=page]{color:#137a37;font-weight:700}
.wf-theme-toggle{background:none;border:1px solid #ccd0d4;border-radius:5px;cursor:pointer;
  font-size:14px;line-height:1;padding:4px 7px;color:#5b6168}
.wf-theme-toggle:hover{border-color:#9aa0a6}
.wf-theme-toggle .wf-icon-sun{display:none}
.leaflet-top{top:48px}
.wf-dark .wf-site-bar{background:rgba(20,23,26,.92);border-bottom-color:#2a2f34}
.wf-dark .wf-site-bar .brand{color:#e8eaed}
.wf-dark .wf-site-bar nav a{color:#9aa0a6}
.wf-dark .wf-site-bar nav a:hover,.wf-dark .wf-site-bar nav a[aria-current=page]{color:#5fcf83}
.wf-dark .wf-theme-toggle{border-color:#444;color:#e8eaed}
.wf-dark .wf-theme-toggle:hover{border-color:#5fcf83}
.wf-dark .wf-theme-toggle .wf-icon-moon{display:none}
.wf-dark .wf-theme-toggle .wf-icon-sun{display:inline}
/* Leaflet's own controls (zoom, layers box, scale, attribution) ship
   hard-coded white/black -- retheme them to match the rest of the page. */
.wf-dark .leaflet-control-zoom a,.wf-dark .leaflet-control-layers,.wf-dark .leaflet-bar a,
.wf-dark .leaflet-control-attribution,.wf-dark .leaflet-control-scale-line{
  background:#26292c !important;color:#e8eaed !important;border-color:#444 !important}
.wf-dark .leaflet-control-attribution a{color:#8ab4f8 !important}
.wf-dark .leaflet-control-layers-toggle{filter:invert(1) hue-rotate(180deg)}
.wf-dark .leaflet-control-layers-separator{border-top-color:#444 !important}
.wf-dark .wf-interp{color:#e8eaed}
.wf-dark .wf-interp-hr{border-top-color:#444 !important}
.wf-dark .wf-interp-note{color:#9aa0a6 !important}
/* The basemap tiles (Positron/OSM) are flat raster images -- CSS can't
   restyle them directly, so invert+rehue the tile pane to fake a dark
   basemap. Scoped off whenever the aerial imagery layer is active (a
   photograph inverted looks broken, not "dark mode"); see the toggle
   script below. The container's own background (visible through any gap
   before a tile has painted in -- panning, zooming, or just slow network)
   is set dark too, so those gaps don't flash white against everything else.
   Gated on .wf-dark-invert (JS-computed: dark mode AND not the aerial
   layer), not .wf-dark directly, so it stays correctly scoped. */
.leaflet-container.wf-dark-invert{background:#0e0f10}
.leaflet-container.wf-dark-invert .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.95) contrast(.9)}
</style>
<div class="wf-site-bar">
  <a class="brand" href="./">Wake Forest Micromobility</a>
  <nav>
    <a href="./" aria-current="page">Map</a>
    <a href="wake-forest-router.html">Route</a>
    <a href="articles/june2026/">Article</a>
    <a href="https://github.com/spinkham/wake_forest_micromobility">Data</a>
    <button id="wfThemeToggle" class="wf-theme-toggle" type="button"
     title="Toggle dark / light theme" aria-label="Toggle dark / light theme"
     onclick="var h=document.documentElement,d=!h.classList.contains('wf-dark');
       h.classList.toggle('wf-dark',d);try{localStorage.setItem('wfTheme',d?'dark':'light')}catch(e){}">
      <span class="wf-icon-moon">&#x1F319;</span><span class="wf-icon-sun">&#x2600;&#xFE0F;</span>
    </button>
  </nav>
</div>
"""))

# Basemap-invert toggle: keeps .wf-dark-invert (background on the container,
# invert filter on the tile pane -- see the stylesheet above) off whenever
# the active base layer is the aerial photograph. Reacts to the theme-toggle
# button too, via a MutationObserver on <html class> -- that's the same
# attribute the header bootstrap script and the toggle button both write to,
# so this one observer covers "OS preference changed" and "user clicked the
# toggle" without the two scripts needing to know about each other.
m.get_root().html.add_child(folium.Element(f"""
<script>
(function() {{
  var current = 'CartoDB Positron';
  function apply() {{
    var el = document.querySelector('.leaflet-container');
    if (!el) return;
    var dark = document.documentElement.classList.contains('wf-dark');
    el.classList.toggle('wf-dark-invert', dark && current.indexOf('Aerial') === -1);
  }}
  function ready() {{ return typeof {m.get_name()} !== 'undefined'; }}
  function init() {{
    if (!ready()) {{ return setTimeout(init, 150); }}
    {m.get_name()}.on('baselayerchange', function(e) {{ current = e.name; apply(); }});
    new MutationObserver(apply).observe(document.documentElement, {{attributes: true, attributeFilter: ['class']}});
    apply();
  }}
  init();
}})();
</script>
"""))

# The aerial imagery layer: serve the local NC 6-inch tile cache, and for any
# tile we don't have (town edges, or everything when no cache is built) fall back
# PER TILE to the live NC OneMap ImageServer (exportImage on that tile's bbox).
# One selectable base layer, off by default. Always emitted -- with no local
# cache every tile simply misses and is fetched live, degrading to pure NC OneMap
# with no extra dependency (this replaces the old esri-leaflet ImageServer layer).
m.get_root().html.add_child(folium.Element(f"""
<script>
(function() {{
  function ready() {{ return window.L && typeof {m.get_name()} !== 'undefined'
                       && typeof {lc.get_name()} !== 'undefined'; }}
  function add() {{
    if (!ready()) {{ return setTimeout(add, 150); }}
    var R = 20037508.342789244;
    var NC = 'https://services.nconemap.gov/secure/rest/services/Imagery/'
           + 'Orthoimagery_Latest/ImageServer/exportImage';
    var CacheFirst = L.TileLayer.extend({{
      createTile: function(coords, done) {{
        var tile = document.createElement('img');
        tile.setAttribute('role', 'presentation'); tile.alt = '';
        var self = this, fell = false;
        L.DomEvent.on(tile, 'load', L.bind(this._tileOnLoad, this, done, tile));
        L.DomEvent.on(tile, 'error', function(e) {{
          if (!fell) {{                       // local miss -> live NC OneMap for this tile's bbox
            fell = true;
            var n = Math.pow(2, coords.z), t = 2 * R / n;
            var x0 = -R + coords.x * t, x1 = x0 + t, y1 = R - coords.y * t, y0 = y1 - t;
            tile.src = NC + '?bbox=' + x0 + ',' + y0 + ',' + x1 + ',' + y1 +
                       '&bboxSR=3857&imageSR=3857&size=256,256&format=jpgpng&f=image';
          }} else {{
            L.bind(self._tileOnError, self, done, tile)(e);
          }}
        }});
        tile.src = this.getTileUrl(coords);
        return tile;
      }}
    }});
    var layer = new CacheFirst('map/tiles/{{z}}/{{x}}/{{y}}.webp', {{
      minZoom: 12, maxNativeZoom: 20, maxZoom: 22,
      attribution: 'NC OneMap Orthoimagery (NC 911 Board)'
    }});
    {lc.get_name()}.addBaseLayer(layer, 'Aerial imagery (6 in)');
  }}
  add();
}})();
</script>
"""))

# persistent data attribution in the Leaflet attribution control
m.get_root().html.add_child(folium.Element(f"""
<script>
(function add() {{
  if (typeof {m.get_name()} === 'undefined' || !{m.get_name()}.attributionControl) {{ return setTimeout(add, 150); }}
  {m.get_name()}.attributionControl.addAttribution('Data: © OpenStreetMap contributors (ODbL) · Town of Wake Forest · NCDOT · NC OneMap');
}})();
</script>
"""))

# rule-interpretation checkbox — rendered INSIDE the layer-control box (below the
# layers, after a divider). Flips permissive/strict and restyles the reachability
# + >25 / proposed bike-lane layers via setStyle. Re-inserted after any list
# rebuild (e.g. when the aerial-imagery base layer is added) so it isn't wiped.
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
      sec.innerHTML = '<div class="wf-interp-hr" style="border-top:1px solid #bbb;margin:7px 0 5px"></div>' +
        '<label style="cursor:pointer;display:block"><input type="checkbox" id="interpChk"' + (PERM ? ' checked' : '') + '> <b>Bike lanes count on &gt;25&nbsp;mph roads</b></label>' +
        '<div class="wf-interp-note" style="color:#888;font-size:10px;margin:2px 0 5px 2px">rule interpretation — affects reachability &amp; bike-lane layers</div>' +
        '<label style="cursor:pointer;display:block"><input type="checkbox" id="swChk"' + (SW ? ' checked' : '') + '> <b>Sidewalks usable on &gt;25&nbsp;mph roads (no bike lane)</b></label>' +
        '<div class="wf-interp-note" style="color:#888;font-size:10px;margin:2px 0 0 2px">reachability only — reconnects ~63% of otherwise-stranded miles</div>';
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
<style>
#wf-legend{{background:#fff;border:1px solid #999}}
.wf-legend-summary{{color:#555}}
.wf-legend-note{{color:#888}}
.wf-dark #wf-legend{{background:#1a1d20;border-color:#3a3f44;color:#e8eaed}}
.wf-dark #wf-legend hr{{border-top-color:#444}}
.wf-dark .wf-legend-summary{{color:#c7cace}}
.wf-dark .wf-legend-note{{color:#9aa0a6}}
</style>
<div id="wf-legend" style="position:fixed;bottom:24px;left:24px;z-index:9999;
padding:8px 12px;border-radius:6px;font:12px/1.5 sans-serif;
box-shadow:0 1px 5px rgba(0,0,0,.3);max-width:340px">
<div style="font-weight:700;cursor:pointer;user-select:none" title="click to collapse / expand"
 onclick="var b=document.getElementById('wf-legend-body'),t=document.getElementById('wf-legend-tog'),h=b.style.display==='none';b.style.display=h?'block':'none';t.textContent=h?'▾':'▸';">
<span id="wf-legend-tog">▾</span> Where micromobility can ride — Wake Forest Ch.30</div>
<div id="wf-legend-body" style="margin-top:6px">
{rows}
<hr style="margin:8px 0">
<div class="wf-legend-summary"><b>{perm_off} mi</b> permitted off-street (greenway/MUP/bike)
&middot; <b>{gated_rd} mi</b> roads &gt;25mph + <b>{gated_sw} mi</b> sidewalks gated
<i>(within corporate limits)</i>.</div>
<div class="wf-legend-note" style="margin-top:5px;font-size:11px">Ch.30 is a traffic ordinance: it
applies inside the corporate limits only — not the ETJ or the unincorporated
enclaves (grey = outside, rule N/A). Boundary: OSM. Facilities: Town of Wake Forest
ArcGIS. Roads/sidewalks: OpenStreetMap; speed posted where tagged (~8%), else
inferred by road class. Not legal advice.</div>
</div>
</div>"""
m.get_root().html.add_child(folium.Element(legend))

out = "../wake-forest-micromobility-map.html"
m.save(out)

# Subresource Integrity: pin every third-party CDN asset folium emits (scripts +
# styles) so a CDN compromise can't inject code or styling into the page. Hashes
# are sha384 of the exact uncompressed bytes of these version-pinned URLs;
# crossorigin is required for SRI and all these CDNs send CORS. Any external
# subresource without a known hash is flagged (e.g. if a folium upgrade bumps a
# version) but left untouched -- fail-open, never silently broken.
SRI = {
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js":
        "sha384-okbbMvvx/qfQkmiQKfd5VifbKZ/W8p1qIsWvE1ROPUfHWsDcC8/BnHohF7vPg2T6",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css":
        "sha384-o/2yZuJZWGJ4s/adjxVW71R+EO/LyCwdQfP5UWSgX/w87iiTXuvDZaejd3TsN7mf",
    "https://code.jquery.com/jquery-3.7.1.min.js":
        "sha384-1H217gwSVyLSIfaLxHbE7dRb3v4mYCKbpQvzx0cegeju1MVsGrX5xXxAvs/HgeFs",
    "https://cdn.jsdelivr.net/npm/bootstrap@5.2.2/dist/js/bootstrap.bundle.min.js":
        "sha384-OERcA2EqjJCMA+/3y+gxIOqMEjwtxJY7qPCqsdltbNJuaOe923+mo//f6V8Qbsw3",
    "https://cdn.jsdelivr.net/npm/bootstrap@5.2.2/dist/css/bootstrap.min.css":
        "sha384-Zenh87qX5JnK2Jl0vWa8Ck2rdkQ2Bzep5IDxbcnCeuOxjzrPF/et3URy9Bv1WTRi",
    "https://cdnjs.cloudflare.com/ajax/libs/Leaflet.awesome-markers/2.0.2/leaflet.awesome-markers.js":
        "sha384-p96PkhiqMxDcor51hgckjZOJvsNNKl4Uy25L8da4p+suI14Ftn3sOuFs+IPN4vFm",
    "https://cdnjs.cloudflare.com/ajax/libs/Leaflet.awesome-markers/2.0.2/leaflet.awesome-markers.css":
        "sha384-AHhmp36MxTYxqK8q9BF7ifcAjDEpcCT+OOmZrBb8vBP6At1I/htDyRK/M8wgzuqx",
    "https://cdn.jsdelivr.net/gh/python-visualization/folium/folium/templates/leaflet.awesome.rotate.min.css":
        "sha384-BTIWC/F2/I+C/O+ojS/83R360V1iJjuVMMx7RKE9ngqKkXUNVbWv/izbGgjTgbuA",
    "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.2.0/css/all.min.css":
        "sha384-SOnAn/m2fVJCwnbEYgD4xzrPtvsXdElhOVvR8ND1YjB5nhGNwwf7nBQlhfAwHAZC",
    "https://netdna.bootstrapcdn.com/bootstrap/3.0.0/css/bootstrap-glyphicons.css":
        "sha384-f+5ueJUSVTts5w31cpiAeriD3134eXSnL//1cJCcmTPkyO6v7j98iJKup9dv6+fg",
}
html = open(out, encoding="utf-8").read()
for url, h in SRI.items():
    html, n = re.subn(re.escape(f'"{url}"'),
                      f'"{url}" integrity="{h}" crossorigin="anonymous"', html, count=1)
    if n == 0:
        print("  SRI: URL not found in HTML (skipped):", url)
for mt in re.finditer(r'<(?:script|link)\b[^>]*?(?:src|href)="(https://[^"]+)"[^>]*?>', html):
    if "integrity=" not in mt.group(0):
        print("  SRI WARNING: external subresource without integrity:", mt.group(1))
open(out, "w", encoding="utf-8").write(html)
print("saved", out, f"(SRI pinned: {len(SRI)} CDN assets)")
print("\n=== category stats (features, miles) ===")
for n, (cnt, mi) in stats.items():
    print(f"  {n:42s} {cnt:5d} feats  {mi:7.1f} mi")
print(f"\nPermitted off-street (in town): {perm_off:.1f} mi")
print(f"Gated (in town): roads>25 {gated_rd:.1f} mi + sidewalks {gated_sw:.1f} mi")
print(f"Outside corporate limits (rule N/A): {stats['Outside Town limits — rule N/A'][1]:.1f} mi")
