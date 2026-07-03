# TODO

- **Image classifier for unmarked bike lanes / sidewalks / footpaths.** Run a
  classifier over the optimized NC 6-inch aerial imagery (`map/tiles/`, built
  by `map/build_imagery_cache.py`) to flag candidate bike lanes, sidewalks,
  and other footpaths that aren't yet in OSM. Output candidate locations
  (lat/lon + confidence) for manual review, then hand-verified additions get
  mapped into OSM directly (the router and reachability analysis both already
  pull from OSM, so anything added there flows through automatically on the
  next `fetch_osm.py` + `build_islands.py` + `build_route_graph.py` run).
