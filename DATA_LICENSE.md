# Data sources & licenses

The micromobility maps in this repository combine several public datasets and
basemaps. This file summarizes each source's license and the attribution to
display. **This is a non-commercial hobby project.** This is not legal advice —
confirm each source's current terms before relying on it.

## Attribution to display on the map / site
> © OpenStreetMap contributors (ODbL) · Town of Wake Forest · NCDOT · NC OneMap ·
> USGS The National Map · © CARTO

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
- **USGS Imagery** — The National Map, USGS. **Public domain** (US federal work);
  free for any use. Best-fit aerial for a public site.
- **NC OneMap 6-inch orthoimagery** — NC OneMap. Free public imagery; attribute
  "NC OneMap." (Served via a dynamic ArcGIS ImageServer + esri-leaflet.)
- **CARTO Positron** (default street basemap) — © OpenStreetMap, © CARTO. Free
  for **non-commercial** use with attribution; commercial use needs a CARTO plan
  (<https://carto.com/basemaps/>).
- **OpenStreetMap tiles** — © OpenStreetMap contributors. Subject to the OSMF
  **Tile Usage Policy** (<https://operations.osmfoundation.org/policies/tiles/>):
  light use only — not for heavy/commercial production. Self-host or use a
  provider if traffic grows.
- **Esri World Imagery** — *removed.* Governed by Esri's terms, and the
  underlying Maxar high-res has redistribution limits, so it isn't clean for a
  public site without an Esri license. USGS + NC OneMap cover imagery instead.

## Software
Leaflet (BSD-2-Clause), folium (MIT), esri-leaflet (Apache-2.0), GeoPandas /
Shapely / NetworkX / osmnx, and this repository's own scripts. All permissive;
note that esri-leaflet is only the client — services pulled through it remain
bound by their own terms above.
