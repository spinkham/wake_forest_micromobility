#!/usr/bin/env python3
"""Wake Forest bike-route planner: point-to-point routing over the graph
build_route_graph.py exports, under three selectable rules:
  Legal        -- Chapter 30 + the sidewalk-legalization reading (matches
                  build_islands.py's trav_all_sw); legal is not always safe.
  Safe         -- the same physical-safety rule applied everywhere, in town
                  or not: <=25 mph, bike lane, path, or a >25 mph road with a
                  genuinely adjacent sidewalk. No freeways, ever.
  Least-unsafe -- Safe, plus a last-resort bridging allowance (26-44 mph, no
                  bike lane/sidewalk, heavily cost-penalized) for reaching
                  areas Safe alone can't connect to. Never uses a 45+ mph
                  road; crossing one at an intersection still works for free,
                  via the shared node where a safe cross-street continues.

The site deploys as static files (no backend), so all routing runs CLIENT-SIDE
in the browser: a hand-written Dijkstra over wf-route-graph.json (built by
build_route_graph.py), re-classifying each edge's tier-membership and cost at
routing time (see classify() in app_js below -- the same edge can be a
different "kind" under different tiers). This script only renders the
folium/Leaflet page (same scaffold as build_map.py: basemaps, the NC 6-inch
aerial CacheFirst layer, site header, SRI pinning) plus a faint
reachable-network context layer and the routing UI (HTML/CSS/JS, injected as
static elements -- no new CDN dependency).
"""
import os
import re
import geopandas as gpd
import folium

PROJ = 32617

juris = gpd.read_file("corporate_limits.geojson").to_crs(4326)

# ---- map ----------------------------------------------------------------
b = juris.total_bounds
m = folium.Map(tiles=None, control_scale=True, max_zoom=22)
m.fit_bounds([[b[1], b[0]], [b[3], b[2]]])

folium.TileLayer("CartoDB positron", name="CartoDB Positron",
                 overlay=False, control=True, max_native_zoom=20, max_zoom=22).add_to(m)
folium.TileLayer("OpenStreetMap", name="OpenStreetMap", overlay=False, control=True,
                 show=False, max_native_zoom=19, max_zoom=22).add_to(m)

folium.GeoJson(
    juris, name="Corporate limits",
    style_function=lambda f: {"color": "#222", "weight": 2, "fill": False, "dashArray": "6,4"},
).add_to(m)

# Faint context layer: the legally-rideable network under the sidewalk-fix
# reading (role_all_sw == main), so users can see roughly where riding is
# possible before they route a specific trip.
try:
    rg = gpd.read_file("reachability.geojson").to_crs(4326)
    rg = rg[rg["role_all_sw"] == "main"]
    rg["geometry"] = rg.to_crs(PROJ).geometry.simplify(3).to_crs(4326)
    fg = folium.FeatureGroup(name="Rideable network (context)", show=True)
    folium.GeoJson(
        rg[["geometry"]],
        style_function=lambda f: {"color": "#b0b0b0", "weight": 1.3, "opacity": 0.6},
    ).add_to(fg)
    fg.add_to(m)
except Exception as e:
    print("context layer skipped:", e)

lc = folium.LayerControl(collapsed=True)
lc.add_to(m)

# ---- site header bar (matches build_map.py; Route is aria-current here) ----
m.get_root().header.add_child(folium.Element(
    '''<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>&#x1F6B2;</text></svg>">'''
))
m.get_root().html.add_child(folium.Element("""
<style>
.wf-site-bar{position:fixed;top:0;left:0;right:0;z-index:1001;display:flex;
  align-items:center;justify-content:space-between;gap:1rem;
  padding:.6rem clamp(1rem,4vw,1.5rem);
  font:15px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:rgba(255,255,255,.92);border-bottom:1px solid #e4e7ea;
  -webkit-backdrop-filter:saturate(1.3) blur(8px);backdrop-filter:saturate(1.3) blur(8px)}
.wf-site-bar .brand{color:#1b1d20;text-decoration:none;font-weight:700}
.wf-site-bar nav{display:flex;gap:1.15rem}
.wf-site-bar nav a{color:#5b6168;text-decoration:none}
.wf-site-bar nav a:hover{color:#137a37}
.wf-site-bar nav a[aria-current=page]{color:#137a37;font-weight:700}
.leaflet-top{top:48px}
@media (prefers-color-scheme:dark){
  .wf-site-bar{background:rgba(20,23,26,.92);border-bottom-color:#2a2f34}
  .wf-site-bar .brand{color:#e8eaed}
  .wf-site-bar nav a{color:#9aa0a6}
  .wf-site-bar nav a:hover,.wf-site-bar nav a[aria-current=page]{color:#5fcf83}
  /* Leaflet's own controls (zoom, layers box, scale, attribution) ship
     hard-coded white/black -- retheme them to match the rest of the page. */
  .leaflet-control-zoom a,.leaflet-control-layers,.leaflet-bar a,
  .leaflet-control-attribution,.leaflet-control-scale-line{
    background:#26292c !important;color:#e8eaed !important;border-color:#444 !important}
  .leaflet-control-attribution a{color:#8ab4f8 !important}
  .leaflet-control-layers-toggle{filter:invert(1) hue-rotate(180deg)}
  .leaflet-control-layers-separator{border-top-color:#444 !important}
  /* The basemap tiles (Positron/OSM) are flat raster images -- CSS can't
     restyle them directly, so invert+rehue the tile pane to fake a dark
     basemap. Scoped off whenever the aerial imagery layer is active (a
     photograph inverted looks broken, not "dark mode"); see the toggle
     script below. */
  .leaflet-tile-pane.wf-dark-invert{filter:invert(1) hue-rotate(180deg) brightness(.95) contrast(.9)}
}
</style>
<div class="wf-site-bar">
  <a class="brand" href="./">Wake Forest Micromobility</a>
  <nav>
    <a href="./">Map</a>
    <a href="wake-forest-router.html" aria-current="page">Route</a>
    <a href="articles/june2026/">Article</a>
    <a href="https://github.com/spinkham/wake_forest_micromobility">Data</a>
  </nav>
</div>
"""))

# Basemap-invert toggle: keeps .wf-dark-invert (defined above, scoped to
# prefers-color-scheme:dark) off whenever the active base layer is the aerial
# photograph, and re-syncs if the OS theme flips while the tab is open.
m.get_root().html.add_child(folium.Element(f"""
<script>
(function() {{
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  var current = 'CartoDB Positron';
  function apply() {{
    var pane = document.querySelector('.leaflet-tile-pane');
    if (!pane) return;
    pane.classList.toggle('wf-dark-invert', mq.matches && current.indexOf('Aerial') === -1);
  }}
  function ready() {{ return typeof {m.get_name()} !== 'undefined'; }}
  function init() {{
    if (!ready()) {{ return setTimeout(init, 150); }}
    {m.get_name()}.on('baselayerchange', function(e) {{ current = e.name; apply(); }});
    mq.addEventListener('change', apply);
    apply();
  }}
  init();
}})();
</script>
"""))

# The aerial imagery layer: identical to build_map.py -- serve the local NC
# 6-inch tile cache with per-tile live fallback to NC OneMap. Shared cache
# (map/tiles/), relative to the saved HTML (repo root).
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

m.get_root().html.add_child(folium.Element(f"""
<script>
(function add() {{
  if (typeof {m.get_name()} === 'undefined' || !{m.get_name()}.attributionControl) {{ return setTimeout(add, 150); }}
  {m.get_name()}.attributionControl.addAttribution('Data: © OpenStreetMap contributors (ODbL) · Town of Wake Forest · NCDOT · NC OneMap · Search: Nominatim');
}})();
</script>
"""))

# ---- routing control panel (HTML/CSS) ------------------------------------
panel_html = """
<div id="wf-panel" style="position:fixed;top:60px;left:16px;z-index:9999;
padding:10px 14px;border-radius:6px;font:13px/1.5 sans-serif;
box-shadow:0 1px 5px rgba(0,0,0,.3);width:290px;max-height:calc(100vh - 90px);overflow-y:auto">
<div style="font-weight:700;cursor:pointer;user-select:none" title="click to collapse / expand"
 onclick="var b=document.getElementById('wf-panel-body'),t=document.getElementById('wf-panel-tog'),h=b.style.display==='none';b.style.display=h?'block':'none';t.textContent=h?'▾':'▸';">
<span id="wf-panel-tog">▾</span> Bike route planner</div>
<div id="wf-panel-body" style="margin-top:8px">
<div class="wf-desc" style="font-size:11.5px;margin-bottom:8px">Click the map (or search) to set
start &amp; end -- drag pins to adjust. Click into a field below to re-target it.</div>
<div class="wf-row" id="wfStartRow"><span class="wf-dot" style="background:#1a9641"></span>
  <input id="wfStartAddr" placeholder="Start address"><button id="wfStartGo">Go</button></div>
<div class="wf-row" id="wfEndRow"><span class="wf-dot" style="background:#d7191c"></span>
  <input id="wfEndAddr" placeholder="End address"><button id="wfEndGo">Go</button></div>
<div id="wfPlacingHint" class="wf-hint" style="font-size:11px;margin:4px 0 8px">
  Next map click sets: <b id="wfPlacingWho">start</b></div>
<div style="margin-bottom:4px"><b style="font-size:11.5px">Routing rule</b></div>
<div style="margin-bottom:2px">
  <label style="cursor:pointer"><input type="radio" name="wfTier" value="legal"> Legal</label>
  &nbsp;&nbsp;
  <label style="cursor:pointer"><input type="radio" name="wfTier" value="safe" checked> Safe</label>
  &nbsp;&nbsp;
  <label style="cursor:pointer"><input type="radio" name="wfTier" value="least_unsafe"> Least-unsafe</label>
</div>
<div id="wfTierNote" class="wf-hint" style="font-size:11px;margin:2px 0 8px"></div>
<div style="margin-bottom:8px">
  <label style="cursor:pointer"><input type="radio" name="wfMode" value="shortest" checked> Shortest</label>
  &nbsp;&nbsp;
  <label style="cursor:pointer"><input type="radio" name="wfMode" value="comfortable"> Most comfortable</label>
</div>
<button id="wfClear">Clear route</button>
<div id="wfStatus" class="wf-status" style="font-size:11.5px;margin-top:6px"></div>
<hr style="margin:10px 0">
<div id="wfResults" style="font-size:12.5px">Set a start and end point to see a route.</div>
</div>
</div>
<style>
#wf-panel{background:#fff;border:1px solid #999}
#wf-panel .wf-row{display:flex;align-items:center;gap:6px;margin-bottom:5px}
#wf-panel .wf-dot{display:inline-block;width:10px;height:10px;border-radius:50%;flex:none}
#wf-panel input{flex:1;min-width:0;padding:3px 5px;border:1px solid #ccc;border-radius:4px;font:inherit;
  transition:box-shadow .15s,border-color .15s}
#wf-panel .wf-row.wf-active input{border-color:#888;box-shadow:0 0 0 2px rgba(0,0,0,.12)}
#wf-panel #wfStartRow.wf-active input{border-color:#1a9641;box-shadow:0 0 0 2px rgba(26,150,65,.25)}
#wf-panel #wfEndRow.wf-active input{border-color:#d7191c;box-shadow:0 0 0 2px rgba(215,25,28,.25)}
#wf-panel button{padding:3px 8px;border:1px solid #999;border-radius:4px;background:#f4f4f4;
  cursor:pointer;font:inherit}
#wf-panel button:hover{background:#e8e8e8}
.wf-desc{color:#555}
.wf-hint{color:#888}
.wf-status{color:#a33}
.wf-bd-row{display:flex;align-items:center;gap:6px;margin:2px 0}
.wf-bd-sw{display:inline-block;width:14px;height:4px;border-radius:2px;flex:none}
.wf-dist{margin-bottom:6px}
.wf-cur{margin-top:6px;padding-top:6px;border-top:1px dashed #ccc;color:#444}
.wf-cur-bad{color:#a33}
.wf-sw-note{margin-top:6px;color:#a3630a;font-size:11.5px}
.wf-unsafe-note{margin-top:6px;color:#a33;font-size:11.5px}
.wf-pin{border-radius:50%;color:#fff;font-weight:700;text-align:center;line-height:22px;
  box-shadow:0 1px 3px rgba(0,0,0,.5);border:2px solid #fff}
.wf-pin-start{background:#1a9641}
.wf-pin-end{background:#d7191c}
@media (prefers-color-scheme:dark){
  #wf-panel{background:#1a1d20;border-color:#3a3f44;color:#e8eaed}
  #wf-panel input{background:#26292c;color:#e8eaed;border-color:#444}
  #wf-panel button{background:#2a2e32;color:#e8eaed;border-color:#555}
  #wf-panel button:hover{background:#33383d}
  .wf-desc,.wf-hint{color:#9aa0a6}
  .wf-status,.wf-cur-bad,.wf-unsafe-note{color:#e07a7a}
  .wf-sw-note{color:#e0a95a}
  .wf-cur{border-top-color:#444;color:#c7cace}
}
</style>
"""

# ---- routing app (vanilla JS -- no library, no server; Dijkstra over the
# exported graph). Uses @@TOKEN@@ placeholders instead of Python f-string
# brace-escaping, since this block is large and almost entirely JS braces.
app_js = """
<script>
(function() {
  function ready() { return window.L && typeof @@MAP@@ !== 'undefined'
                     && document.getElementById('wf-panel'); }
  function init() {
    if (!ready()) { return setTimeout(init, 150); }
    var map = @@MAP@@;
    var state = { start: null, end: null, mode: 'shortest', tier: 'safe', placing: 'start' };
    var startMarker = null, endMarker = null;
    var routeLayer = L.layerGroup().addTo(map);
    var graph = null;

    var TIERS = ['legal', 'safe', 'least_unsafe'];
    var TIER_LABEL = {legal: 'Legal', safe: 'Safe', least_unsafe: 'Least-unsafe'};
    var TIER_NOTE = {
      legal: 'What Chapter 30 (plus the sidewalk fix) legally allows. Outside town, speed is '
        + 'capped at 45 mph (never a faster road without a bike lane or sidewalk, and never a '
        + 'freeway) \\u2014 still not always as safe as the Safe tier.',
      safe: 'Only \\u226425 mph streets, bike lanes, paths, or a >25 mph road with a genuinely '
        + 'adjacent sidewalk. No exceptions, in town or out.',
      least_unsafe: 'Safe routes, plus a last resort: 26\\u201344 mph roads with no bike lane or '
        + 'sidewalk, used only when nothing safer connects. Never a 45+ mph road (crossing one '
        + 'at an intersection is fine).',
    };
    var COLORS = {path: '#1a9641', bikelane: '#984ea3', slow_street: '#a6d96a',
                  sidewalk_fast: '#e08214', legal_other: '#4575b4',
                  unsafe_connector: '#d7191c', lot: '#8c8c8c', snap: '#999999'};
    var LABELS = {path: 'Greenway / multi-use path / cycleway', bikelane: 'On-road bike lane',
                  slow_street: 'Street ≤25 mph',
                  sidewalk_fast: 'Sidewalk on a road >25 mph (needs a signed exception)',
                  legal_other: 'Legal via jurisdiction exemption (outside town, \\u226445 mph, no freeway)',
                  unsafe_connector: 'Moderate-speed connector (26\\u201344 mph, no bike lane/sidewalk)',
                  lot: 'Parking lot aisle / driveway'};
    // sidewalk_fast was 4.0 -- too punitive: it could make "comfortable" mode
    // take a multi-mile detour through parking lots/greenways to dodge even a
    // short, perfectly rideable signed-sidewalk stretch, producing a LONGER
    // and objectively worse route than just using the sidewalk. 1.8 still
    // nudges away from long sidewalk-riding in favor of a comparable-length
    // path/street, without preferring a much longer route to avoid it.
    var COMFORT_MULT = {path: 1.0, bikelane: 1.15, slow_street: 1.4, sidewalk_fast: 1.8,
                        legal_other: 2.0, unsafe_connector: 1.0, lot: 1.2, snap: 1.0};
    var UNSAFE_PENALTY = 20;

    // ---- graph load ------------------------------------------------------
    fetch('wf-route-graph.json').then(function(r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    }).then(function(g) {
      graph = buildGraph(g);
      setStatus('');
    }).catch(function(err) {
      setStatus('Could not load the route graph (' + err.message + ').');
    });

    function buildGraph(g) {
      var adjAll = new Map();
      function push(m, u, v, idx) {
        if (!m.has(u)) { m.set(u, []); }
        m.get(u).push({to: v, idx: idx});
      }
      for (var i = 0; i < g.edges.length; i++) {
        var e = g.edges[i], u = String(e.u), v = String(e.v);
        push(adjAll, u, v, i); push(adjAll, v, u, i);
      }
      return {nodes: g.nodes, edges: g.edges, adjAll: adjAll, bbox: g.bbox};
    }

    // ---- per-tier traversability + kind, derived from raw edge attributes.
    // The SAME edge can be a different kind under different tiers (e.g. a
    // 30 mph road outside town is "legal_other" under Legal but
    // "unsafe_connector" under Least-unsafe) -- so this can't be precomputed
    // once per edge; it's evaluated at routing time. ------------------------
    function classify(e, tier) {
      if (e.snap) { return {ok: true, kind: 'snap'}; }
      if (e.path) { return {ok: true, kind: 'path'}; }
      if (e.bikelane) { return {ok: true, kind: 'bikelane'}; }
      if (e.lot) { return {ok: true, kind: 'lot'}; }
      if (e.speed <= 25) { return {ok: true, kind: 'slow_street'}; }
      if (e.sidewalk && !e.freeway) { return {ok: true, kind: 'sidewalk_fast'}; }
      if (tier === 'legal' && !e.intown && e.speed <= 45 && !e.freeway) {
        return {ok: true, kind: 'legal_other'};
      }
      if (tier === 'least_unsafe' && e.speed < 45 && !e.freeway) {
        return {ok: true, kind: 'unsafe_connector'};
      }
      return {ok: false, kind: null};
    }

    function edgeWeight(e, kind, mode) {
      var w = e.len;
      if (mode === 'comfortable') { w *= (COMFORT_MULT[kind] || 1.0); }
      if (kind === 'unsafe_connector') { w *= UNSAFE_PENALTY; }
      return w;
    }

    // ---- binary-heap Dijkstra --------------------------------------------
    function MinHeap() { this.a = []; }
    MinHeap.prototype.size = function() { return this.a.length; };
    MinHeap.prototype.push = function(d, v) {
      this.a.push([d, v]);
      var i = this.a.length - 1;
      while (i > 0) {
        var p = (i - 1) >> 1;
        if (this.a[p][0] <= this.a[i][0]) { break; }
        var t = this.a[p]; this.a[p] = this.a[i]; this.a[i] = t; i = p;
      }
    };
    MinHeap.prototype.pop = function() {
      var top = this.a[0], last = this.a.pop();
      if (this.a.length) {
        this.a[0] = last;
        var i = 0, n = this.a.length;
        while (true) {
          var l = 2 * i + 1, r = 2 * i + 2, s = i;
          if (l < n && this.a[l][0] < this.a[s][0]) { s = l; }
          if (r < n && this.a[r][0] < this.a[s][0]) { s = r; }
          if (s === i) { break; }
          var t = this.a[s]; this.a[s] = this.a[i]; this.a[i] = t; i = s;
        }
      }
      return top;
    };

    function dijkstra(adj, edgesArr, tier, mode, src, dst) {
      if (!adj.has(src) || !adj.has(dst)) { return null; }
      var dist = new Map(), prevEdge = new Map(), prevNode = new Map(), visited = new Set();
      var heap = new MinHeap();
      dist.set(src, 0); heap.push(0, src);
      while (heap.size() > 0) {
        var top = heap.pop(), d = top[0], u = top[1];
        if (visited.has(u)) { continue; }
        visited.add(u);
        if (u === dst) { break; }
        var out = adj.get(u) || [];
        for (var i = 0; i < out.length; i++) {
          var edge = out[i], e = edgesArr[edge.idx];
          var cls = classify(e, tier);
          if (!cls.ok) { continue; }
          var w = edgeWeight(e, cls.kind, mode), nd = d + w;
          if (!dist.has(edge.to) || nd < dist.get(edge.to)) {
            dist.set(edge.to, nd); prevEdge.set(edge.to, edge.idx); prevNode.set(edge.to, u);
            heap.push(nd, edge.to);
          }
        }
      }
      if (!dist.has(dst)) { return null; }
      var steps = [], cur = dst;
      while (cur !== src) {
        var idx = prevEdge.get(cur), prev = prevNode.get(cur);
        steps.push({idx: idx, from: prev, to: cur});
        cur = prev;
      }
      steps.reverse();
      return {costDist: dist.get(dst), steps: steps};
    }

    // ---- nearest-node snapping (linear scan; ~11k nodes, trivial) --------
    var COSLAT = Math.cos(35.95 * Math.PI / 180);
    function nearestNode(nodes, lat, lon) {
      var best = null, bestD = Infinity;
      for (var id in nodes) {
        var p = nodes[id], dlat = p[0] - lat, dlon = (p[1] - lon) * COSLAT;
        var d = dlat * dlat + dlon * dlon;
        if (d < bestD) { bestD = d; best = id; }
      }
      return best;
    }

    // ---- summarize a path into a distance/kind breakdown (real meters,
    // independent of routing mode -- comfortable-mode "cost" is not mileage) --
    function summarize(steps, tier) {
      var totals = {path: 0, bikelane: 0, slow_street: 0, sidewalk_fast: 0,
                    legal_other: 0, unsafe_connector: 0, lot: 0, snap: 0};
      var swNames = [], unsafeNames = [];
      for (var i = 0; i < steps.length; i++) {
        var e = graph.edges[steps[i].idx];
        var cls = classify(e, tier);
        totals[cls.kind] = (totals[cls.kind] || 0) + e.len;
        if (cls.kind === 'sidewalk_fast' && e.name && swNames.indexOf(e.name) === -1) {
          swNames.push(e.name);
        }
        if (cls.kind === 'unsafe_connector' && e.name && unsafeNames.indexOf(e.name) === -1) {
          unsafeNames.push(e.name);
        }
      }
      var totalLen = 0;
      for (var k in totals) { totalLen += totals[k]; }
      return {totals: totals, totalLen: totalLen, swNames: swNames, unsafeNames: unsafeNames};
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, function(c) {
        return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
      });
    }

    function mi(m) { return (m / 1609.34).toFixed(2); }

    function drawRoute(layerGroup, steps, tier) {
      for (var i = 0; i < steps.length; i++) {
        var st = steps[i], e = graph.edges[st.idx], coords = e.poly;
        if (!coords || coords.length < 2) { continue; }
        var cls = classify(e, tier);
        var oriented = (String(e.u) === st.from) ? coords : coords.slice().reverse();
        L.polyline(oriented, {
          color: COLORS[cls.kind] || '#3388ff', weight: 5, opacity: 0.9,
        }).addTo(layerGroup);
      }
    }

    function renderResults(sel, allResults, tier) {
      var compareRows = TIERS.map(function(t) {
        var r = allResults[t];
        var txt = r ? '<b>' + mi(r.breakdown.totalLen) + ' mi</b>' : 'no route';
        return '<div' + (t === tier ? ' style="font-weight:700"' : '') + '>'
          + TIER_LABEL[t] + ': ' + txt + '</div>';
      }).join('');
      var compareHtml = '<div class="wf-cur">' + compareRows + '</div>';

      if (!sel) {
        setResults('<div class="wf-dist">No <b>' + TIER_LABEL[tier] + '</b> route found between '
          + 'these points.</div>' + compareHtml);
        return;
      }

      var b = sel.breakdown;
      var rows = ['path', 'bikelane', 'lot', 'slow_street', 'sidewalk_fast', 'legal_other', 'unsafe_connector']
        .map(function(k) {
          var m = b.totals[k] || 0;
          if (m < 1) { return ''; }
          return '<div class="wf-bd-row"><span class="wf-bd-sw" style="background:' + COLORS[k]
            + '"></span>' + LABELS[k] + ' <b>' + mi(m) + ' mi</b></div>';
        }).join('');

      var notes = '';
      if (b.swNames.length) {
        notes += '<div class="wf-sw-note">Relies on a signed sidewalk exception on: '
          + b.swNames.map(escapeHtml).join(', ') + '.</div>';
      }
      if (b.unsafeNames.length) {
        notes += '<div class="wf-unsafe-note">Uses a moderate-speed connector on: '
          + b.unsafeNames.map(escapeHtml).join(', ') + '.</div>';
      }

      setResults('<div class="wf-dist"><b>' + mi(b.totalLen) + ' mi</b> under the <b>'
        + TIER_LABEL[tier] + '</b> rule</div>' + rows + notes + compareHtml);
    }

    function setResults(html) { document.getElementById('wfResults').innerHTML = html; }
    function setStatus(msg) { document.getElementById('wfStatus').textContent = msg; }
    function updatePlacingUI() {
      document.getElementById('wfPlacingWho').textContent = state.placing;
      document.getElementById('wfStartRow').classList.toggle('wf-active', state.placing === 'start');
      document.getElementById('wfEndRow').classList.toggle('wf-active', state.placing === 'end');
    }

    // advance to the OTHER endpoint after either field gets filled -- by a
    // map click OR an address search, so setting start by address correctly
    // hands off to "end" next instead of leaving the target stuck on start.
    function advancePlacing(justSet) {
      state.placing = (justSet === 'start') ? 'end' : 'start';
      updatePlacingUI();
    }
    function updateTierNote() { document.getElementById('wfTierNote').textContent = TIER_NOTE[state.tier]; }

    function runRoute() {
      routeLayer.clearLayers();
      var allResults = {};
      TIERS.forEach(function(t) {
        var r = dijkstra(graph.adjAll, graph.edges, t, state.mode, state.start.node, state.end.node);
        allResults[t] = r ? {dist: r, breakdown: summarize(r.steps, t)} : null;
      });
      var sel = allResults[state.tier];
      if (sel) { drawRoute(routeLayer, sel.dist.steps, state.tier); }
      renderResults(sel, allResults, state.tier);
    }

    function maybeRoute() { if (state.start && state.end && graph) { runRoute(); } }

    function setEndpoint(which, lat, lon) {
      var nodeId = nearestNode(graph.nodes, lat, lon);
      var p = graph.nodes[nodeId];
      state[which] = {node: nodeId, lat: p[0], lon: p[1]};
      var icon = L.divIcon({
        className: 'wf-pin wf-pin-' + which, html: which === 'start' ? 'A' : 'B',
        iconSize: [22, 22], iconAnchor: [11, 11],
      });
      var marker = which === 'start' ? startMarker : endMarker;
      if (marker) {
        marker.setLatLng([p[0], p[1]]);
      } else {
        marker = L.marker([p[0], p[1]], {icon: icon, draggable: true}).addTo(map);
        marker.on('dragend', function(e) {
          var ll = e.target.getLatLng();
          setEndpoint(which, ll.lat, ll.lng);
          maybeRoute();
        });
        if (which === 'start') { startMarker = marker; } else { endMarker = marker; }
      }
    }

    function geocode(which, query) {
      if (!graph) { setStatus('Route graph still loading\\u2026'); return; }
      query = (query || '').trim();
      if (!query) { return; }
      var vb = graph.bbox[0] + ',' + graph.bbox[3] + ',' + graph.bbox[2] + ',' + graph.bbox[1];
      var url = 'https://nominatim.openstreetmap.org/search?format=json&limit=1&viewbox=' + vb
        + '&bounded=1&q=' + encodeURIComponent(query + ', Wake Forest, NC');
      setStatus('Searching\\u2026');
      fetch(url, {headers: {'Accept': 'application/json'}}).then(function(r) { return r.json(); })
        .then(function(res) {
          if (!res.length) { setStatus('No match for "' + query + '".'); return; }
          setStatus('');
          setEndpoint(which, parseFloat(res[0].lat), parseFloat(res[0].lon));
          advancePlacing(which);
          maybeRoute();
        }).catch(function() { setStatus('Search failed (network error).'); });
    }

    function clearRoute() {
      state.start = null; state.end = null; state.placing = 'start';
      if (startMarker) { map.removeLayer(startMarker); startMarker = null; }
      if (endMarker) { map.removeLayer(endMarker); endMarker = null; }
      routeLayer.clearLayers();
      document.getElementById('wfStartAddr').value = '';
      document.getElementById('wfEndAddr').value = '';
      setResults('Set a start and end point to see a route.');
      setStatus('');
      updatePlacingUI();
    }

    map.on('click', function(ev) {
      if (!graph) { setStatus('Route graph still loading\\u2026'); return; }
      var which = state.placing;
      setEndpoint(which, ev.latlng.lat, ev.latlng.lng);
      advancePlacing(which);
      maybeRoute();
    });

    document.getElementById('wfStartGo').addEventListener('click', function() {
      geocode('start', document.getElementById('wfStartAddr').value);
    });
    document.getElementById('wfEndGo').addEventListener('click', function() {
      geocode('end', document.getElementById('wfEndAddr').value);
    });
    document.getElementById('wfStartAddr').addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); geocode('start', this.value); }
    });
    document.getElementById('wfEndAddr').addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); geocode('end', this.value); }
    });
    // explicit override: clicking/tabbing into a field directly targets it
    // for the next map click, regardless of where auto-advance left off.
    document.getElementById('wfStartAddr').addEventListener('focus', function() {
      state.placing = 'start'; updatePlacingUI();
    });
    document.getElementById('wfEndAddr').addEventListener('focus', function() {
      state.placing = 'end'; updatePlacingUI();
    });
    document.getElementById('wfClear').addEventListener('click', clearRoute);
    var modeInputs = document.getElementsByName('wfMode');
    for (var i = 0; i < modeInputs.length; i++) {
      modeInputs[i].addEventListener('change', function() { state.mode = this.value; maybeRoute(); });
    }
    var tierInputs = document.getElementsByName('wfTier');
    for (var i = 0; i < tierInputs.length; i++) {
      tierInputs[i].addEventListener('change', function() {
        state.tier = this.value; updateTierNote(); maybeRoute();
      });
    }
    updatePlacingUI();
    updateTierNote();
  }
  init();
})();
</script>
"""
app_js = app_js.replace("@@MAP@@", m.get_name())

m.get_root().html.add_child(folium.Element(panel_html))
m.get_root().html.add_child(folium.Element(app_js))

out = "../wake-forest-router.html"
m.save(out)

# Subresource Integrity: same pinned CDN assets as build_map.py (identical
# folium/Leaflet/jQuery/Bootstrap/Awesome-Markers bundle -- no new library).
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
