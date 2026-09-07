# Data sources & licenses

The micromobility maps in this repository combine several public datasets and
basemaps. This file summarizes each source's license and the attribution to
display. **This is a non-commercial hobby project.** This is not legal advice —
confirm each source's current terms before relying on it.

## Attribution to display on the map / site
> © OpenStreetMap contributors (ODbL) · Town of Wake Forest · NCDOT · NC OneMap ·
> © CARTO

## Vector / feature data
- **OpenStreetMap** — roads, sidewalks, cycleways, the routing graph, and the
  corporate-limits boundary. © OpenStreetMap contributors, **ODbL 1.0**
  (<https://opendatacommons.org/licenses/odbl/>).
  **Share-alike note:** the derived data files committed here —
  `map/osm_highways.geojson`, `map/corporate_limits.geojson`,
  `map/reachability.geojson` — are *Derivative Databases* of OpenStreetMap and
  are therefore also **ODbL**. They must keep the OSM attribution, and any
  redistribution must remain under ODbL (share-alike).
- **Town of Wake Forest GIS** — greenways, multi-use paths, bike lanes/sharrows,
  town limits. Town of Wake Forest, via its ArcGIS open-data portal
  (<https://data2-wakeforestnc.opendata.arcgis.com/>). NC public records; no
  warranty. Attribute "Town of Wake Forest."
- **NCDOT posted speed limits** — `NCDOT_SpeedLimitQtr`, NC Dept. of
  Transportation. Public data; NCDOT disclaims liability for errors. Attribute
  "NCDOT."
- **NC OneMap** — NC OneMap / NC 911 Board (<https://www.nconemap.gov/>). Free
  public data. Attribute "NC OneMap."

## Basemap / imagery tiles (selectable in the map's layer control)
- **NC OneMap 6-inch orthoimagery** (the map's only aerial layer) — NC OneMap.
  Free public imagery; attribute "NC OneMap." Served from a locally built tile
  cache (`map/build_imagery_cache.py`, gitignored), falling back per tile to the
  live NC OneMap ArcGIS ImageServer (`exportImage`) for anything not cached — so
  the map shows aerial imagery with or without a local cache.
- **USGS Imagery** — *removed.* The National Map (USGS) is public domain and was
  an alternate aerial; the higher-resolution NC OneMap 6-inch layer now covers
  imagery, so it was dropped to keep a single aerial source.
- **CARTO Positron** (default street basemap) — © OpenStreetMap, © CARTO.
  **Needs a free API key** as of August 2026: tiles fetched without one still
  serve, but come back stamped with an "API KEY REQUIRED" watermark. The key is
  free — 5 million tile requests per calendar month, no CARTO account — and is
  meant for **non-commercial** use (research, teaching, personal projects, which
  is what this is); commercial use needs a CARTO plan
  (<https://carto.com/basemaps/>). CARTO *and* OpenStreetMap must both stay
  credited on the map. This project's key lives in `map/basemap.py` and is
  registered to the domain the maps are published from — **anyone hosting these
  maps elsewhere must request their own key**
  (<https://carto.com/basemaps/apikey/>) and replace it. CARTO has said it is
  considering stopping data updates to the raster basemaps (vector is the
  going-forward product), so this layer will need revisiting eventually.
- **OpenStreetMap tiles** — © OpenStreetMap contributors. Subject to the OSMF
  **Tile Usage Policy** (<https://operations.osmfoundation.org/policies/tiles/>):
  light use only — not for heavy/commercial production. Self-host or use a
  provider if traffic grows.
- **Esri World Imagery** — *removed.* Governed by Esri's terms, and the
  underlying Maxar high-res has redistribution limits, so it isn't clean for a
  public site without an Esri license. USGS + NC OneMap cover imagery instead.

## Software
Leaflet (BSD-2-Clause), folium (MIT), GeoPandas / Shapely / NetworkX / osmnx,
and this repository's own scripts. All permissive; note that the NC OneMap
imagery is pulled directly from its ArcGIS REST endpoint (no client library),
and that service remains bound by its own terms above.
