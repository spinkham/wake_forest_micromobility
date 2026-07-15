#!/usr/bin/env python3
"""MEASURE ONLY -- what the reachability headlines would become if footways were
modelled correctly. Writes nothing; does not touch reachability.geojson.

Two separate corrections, deliberately kept apart:

  today (trav_all): Ch.30's sidewalk ban binds only INSIDE the corporate limits.
      Outside them NC law permits sidewalk riding and no intervening
      jurisdiction is known, so an out-of-town sidewalk is legal TODAY.
      traversable() returns False for every footway before it ever reaches its
      own out-of-town branch, so today's number is UNDERSTATED.

  +sidewalks (trav_all_sw): the scenario is "sidewalk riding is legalised", but
      it only ever added sw_road (a ROAD that has a sidewalk alongside) -- it
      never made the sidewalk geometry itself traversable. So a greenway whose
      only link to the street is a sidewalk stays stranded even in the scenario
      that legalises sidewalks. That number is UNDERSTATED too.

Run from map/.
"""
_src = open("build_islands.py").read().split('print("\\n=== GROUNDED reachability')[0]
exec(_src)

UNRIDEABLE_SURFACE = {"dirt", "ground", "sand", "grass", "mud", "earth", "woodchips"}


def _tag1(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v)


_surface = (edges["surface"].map(_tag1) if "surface" in edges.columns
            else pd.Series(None, index=edges.index, dtype=object))
_bicycle = (edges["bicycle"].map(_tag1) if "bicycle" in edges.columns
            else pd.Series(None, index=edges.index, dtype=object))
_rideable_foot = (edges["hw"].isin(("footway", "pedestrian", "steps"))
                  & ~_surface.isin(UNRIDEABLE_SURFACE)
                  & ~_bicycle.isin(["no", "private"]))

edges["foot_out"] = _rideable_foot & ~edges["intown"]          # legal today
edges["foot_any"] = _rideable_foot                             # legal under the fix
edges["trav_all_v2"] = edges["trav_all"] | edges["foot_out"]
edges["trav_all_sw_v2"] = edges["trav_all_sw"] | edges["foot_any"]

print("\n=== MEASURE ONLY: footway modelling impact on reachability ===")
print(f"rideable footway mileage: {edges.loc[_rideable_foot, 'len_mi'].sum():.1f} mi total "
      f"({edges.loc[edges['foot_out'], 'len_mi'].sum():.1f} mi outside town = legal today, "
      f"{edges.loc[_rideable_foot & edges['intown'], 'len_mi'].sum():.1f} mi in town = barred by Ch.30)\n")

for col, label in (("trav_all", "today (as published)"),
                   ("trav_all_v2", "today + out-of-town sidewalks (legal now)"),
                   ("trav_all_sw", "+sidewalks (as published)"),
                   ("trav_all_sw_v2", "+sidewalks + sidewalk geometry")):
    summarize(reach(col), label)

# the article's own headline metric: % of in-town non-freeway STREET mi reachable
print("\n--- article headline metric: % of in-town non-freeway street mi reachable ---")
_streets = edges["hw"].isin(ROAD) & edges["intown"] & ~edges["hw"].isin(FREEWAY)
_tot_street = edges.loc[_streets, "len_mi"].sum()
for col, label in (("trav_all", "today (as published)"),
                   ("trav_all_v2", "today + out-of-town sidewalks"),
                   ("trav_all_sw", "+sidewalks (as published)"),
                   ("trav_all_sw_v2", "+sidewalks + sidewalk geometry")):
    ride = reach(col)
    main_ids = set(ride.loc[ride["role"] == "main", "u"]) | set(ride.loc[ride["role"] == "main", "v"])
    m = _streets & (edges["u"].isin(main_ids) | edges["v"].isin(main_ids)) & edges[col]
    print(f"  {label:42s} {100 * edges.loc[m, 'len_mi'].sum() / _tot_street:4.1f}% "
          f"of {_tot_street:.0f} in-town street mi")
