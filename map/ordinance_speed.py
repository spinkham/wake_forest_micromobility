#!/usr/bin/env python3
"""Speed field from evidence rather than road class (hybrid precedence).

The reachability model currently guesses a speed for 227 of the 422 in-town road
miles from OSM's highway class (unclassified->30, tertiary->35, ...). Chapter 30
turns entirely on whether a street is <=25 mph, so that guess decides legal-or-not
for over half the town on no evidence at all.

But the Town does not have to be guessed at. Ch. 30 Art. VI Sec. 30-216
(Schedule XVII) sets the speed limit on every street it governs:

    "The speed limit on all streets, other than on state highways and in school
     districts during times specified, shall be 25 miles per hour unless
     otherwise posted."

and then enumerates all 94 exceptions by name. The precedence used here:

    1. NCDOT posted      -- state-maintained roads, which the ordinance
                            explicitly carves out ("other than on state
                            highways"). Already wired in via nc_speed.
    2. Schedule XVII     -- the ordinance names this street (or this stretch of
                            it); where it speaks, it IS the posting.
    3. OSM maxspeed      -- a surveyed sign. The 25 mph rule is explicitly
                            subordinate to posting ("unless otherwise posted"),
                            so a real tag displaces the default. Leaving OSM out
                            asserted 25 mph on 7.6 mi where a tag says otherwise
                            -- the ordinance-only field's worst error, and in the
                            permissive direction.
    4. 25 mph            -- the ordinance default: nothing posted, nothing listed.

Class-based inference disappears entirely bar the handful of state-grade edges
with no evidence of any kind.

Cross-checked before adopting: OSM agrees with the ordinance field on 79% of the
176.8 mi where both exist. It agrees with NCDOT on only 29% -- but 17.7 of the
25.1 conflicting miles are freeway-class road the model bars regardless of speed,
so that disagreement cannot change an outcome; on ordinary roads it is 7.4 mi,
resolved in NCDOT's favour by precedence.

This script does NOT change the model. It builds the field alongside the existing
one and prints the disagreements, so the two can be compared before anything is
rewired. Run from map/:

    python ordinance_speed.py            # comparison report
    python ordinance_speed.py --csv      # also write ordinance_speed_compare.csv

Where this stands, and what is left:

* Segment qualifiers ARE resolved. 28 of the 30 qualified entries are matched to
  actual edges by ordinance_segments.py (graph path between the named endpoints,
  with route `ref` used where OSM does not carry the ordinance's name -- OSM refs
  "NC 98 Business" rather than naming a street that). The 2 that do not resolve
  are listed at runtime and are NOT painted onto their whole street; both sit on
  state routes where NCDOT supplies the posted limit anyway, so the miles at risk
  of being wrongly called 25 mph are 0.00.
* Streets listed at several limits on different segments now resolve per segment
  rather than falling back to the highest.
* `unresolved` covers 2.70 mi of state-grade road with neither an NCDOT value nor
  a schedule entry. 2.53 mi of that is motorway/trunk class, which the model bars
  regardless of speed, so the value is moot; only 0.16 mi (slivers of North Main,
  South Main and Rogers Road) could change an outcome.
* SCHOOL ZONES are out of scope, checked and closed. Sec. 30-215 (Schedule XVI)
  is a rule, not a list: it names no streets, and its limits apply only "during
  the posted hours of enforcement on days when school is in session". They never
  apply all day, so they cannot change whether a street is rideable in general.
* Municode may lag amendments; see the header of ordinance_speed_schedule.csv.
"""
import re
import sys

import pandas as pd

import ordinance_segments as seg

SCHEDULE = "ordinance_speed_schedule.csv"
DEFAULT_MPH = 25
# OSM classes that stand in for "state highway", which Sec. 30-216 excludes
# from its 25 mph default. Never given the default; left to NCDOT or the model.
STATE_GRADE = {"motorway", "motorway_link", "trunk", "trunk_link",
               "primary", "primary_link"}

# Markers that separate a street name from a segment qualifier in an entry.
_QUAL = re.compile(
    r",\s*(?:from|between|beginning)\b"
    r"|\s+to its junction with\b"
    r"|,\s*east from\b"
    r"|\s+within the municipal limits\b",
    re.I,
)
_PARENS = re.compile(r"\s*\([^)]*\)")
_PUNCT = re.compile(r"[^a-z0-9 ]")


def norm(s):
    """Normalise a street name for matching: lowercase, no parentheticals, no
    punctuation, single spaces. 'St. Catherine's Drive' -> 'st catherines drive'."""
    s = _PARENS.sub("", str(s)).lower()
    return _PUNCT.sub("", s).strip()


def load_schedule(path=SCHEDULE):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#") or line.startswith("mph,"):
                continue
            mph, _, entry = line.partition(",")
            entry = entry.strip()
            if entry.startswith('"') and entry.endswith('"'):
                entry = entry[1:-1]
            m = _QUAL.search(entry)
            name = entry[:m.start()] if m else entry
            rows.append({"mph": int(mph), "entry": entry,
                         "name": name.strip().rstrip(","),
                         "key": norm(name), "approx": bool(m)})
    return pd.DataFrame(rows)


def match_schedule(names, sched):
    """Map each distinct OSM street name to (mph, approx, how). Exact normalised
    match wins; otherwise a schedule name that is a whole-word prefix of the OSM
    name ('west oak' -> 'west oak avenue'). A name listed at two different
    speeds on different segments resolves to the HIGHEST of them and is reported;
    see the comment below for why the permissive fallback is not acceptable."""
    exact = {}
    for _, r in sched.iterrows():
        exact.setdefault(r.key, []).append(r)
    out, ambiguous = {}, []
    for nm in names:
        k = norm(nm)
        if not k:
            continue
        hits = exact.get(k)
        how = "exact"
        if not hits:
            hits = [r for _, r in sched.iterrows()
                    if k.startswith(r.key + " ") or k == r.key]
            how = "prefix"
        if not hits:
            continue
        speeds = {h.mph for h in hits}
        if len(speeds) > 1:
            # The street is listed at several limits on different segments
            # (South Main is 35 on one stretch and 45 on another). Name matching
            # cannot tell which edge is which, so take the HIGHEST -- falling
            # through to the 25 mph default here would silently make an arterial
            # look legal, which is the one error this analysis must not make.
            ambiguous.append((nm, sorted(speeds)))
            out[nm] = (max(speeds), True, "ambiguous-max")
            continue
        out[nm] = (hits[0].mph, any(h.approx for h in hits), how)
    return out, ambiguous


def head(v):
    """OSM tags arrive as scalars or lists; take the first non-null."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def main():
    sched = load_schedule()
    print(f"Schedule XVII: {len(sched)} entries "
          f"({sched.approx.sum()} carry a segment qualifier)")
    print("  by limit:", sched.groupby("mph").size().to_dict())

    # Same exec idiom the other downstream scripts use: everything up to the
    # reachability section, which is where `edges` is fully built.
    src = open("build_islands.py").read().split(
        'print("\\n=== GROUNDED reachability')[0]
    g = {"__name__": "__ordinance__"}
    exec(compile(src, "build_islands.py", "exec"), g)
    edges = g["edges"]
    ROAD = g["ROAD"]

    e = edges.copy()
    e["nm"] = e["name"].map(head)
    roads = e[e["hw"].isin(ROAD)].copy()

    # --- resolve the 30 segment-qualified entries to actual edges -------------
    import osmnx as ox
    ep = e.to_crs(g["PROJ"])
    nodes = ox.graph_to_gdfs(g["G"], edges=False).to_crs(g["PROJ"])
    coords = {i: (pt.x, pt.y) for i, pt in nodes.geometry.items()}
    known = sorted({n for n in ep["nm"] if isinstance(n, str)})

    seg_mph, seg_ok, seg_fail = {}, 0, []
    for _, r in sched[sched.approx].iterrows():
        nm = str(r["name"])
        qual = str(r["entry"])[len(nm):].strip().lstrip(",").strip()
        idx, note = seg.resolve_segment(nm, qual, ep, coords, known)
        if idx:
            seg_ok += 1
            for i in idx:
                # Overlapping entries: keep the higher limit. Same reasoning as
                # the ambiguity rule -- never resolve a conflict permissively.
                seg_mph[i] = max(seg_mph.get(i, 0), int(r["mph"]))
        else:
            seg_fail.append((int(r["mph"]), nm, note))
    seg_mi = ep.loc[list(seg_mph)].length.sum() / 1609.34 if seg_mph else 0.0
    print(f"\nSegment-qualified entries resolved to edges: {seg_ok}/"
          f"{int(sched.approx.sum())}  ({seg_mi:.2f} mi)")
    for mph, nm, note in seg_fail:
        print(f"    UNRESOLVED [{mph:2d}] {nm[:34]:36s} {note[:44]}")

    # Whole-street entries only; a qualified entry must NOT paint its whole street.
    whole = sched[~sched.approx]
    matched, ambiguous = match_schedule(
        sorted({n for n in roads["nm"] if isinstance(n, str)}), whole)
    print(f"\nOSM street names matched to a Schedule XVII entry: {len(matched)}"
          f"  ({sum(1 for v in matched.values() if v[2]=='prefix')} by prefix)")
    if ambiguous:
        print(f"  listed at multiple limits, resolved to the highest: {len(ambiguous)}")
        for nm, sp in ambiguous[:8]:
            print(f"     {nm} -> {sp}")

    def parse_mph(v):
        """OSM maxspeed is free text: '25 mph', '35', 'NC:urban', 'walk'."""
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        m = re.search(r"\d+", str(v))
        return float(m.group()) if m else None

    def ordinance_speed(r):
        if not r["intown"]:
            return (None, "outside", False)
        if pd.notna(r.get("posted")):
            return (r["posted"], "ncdot", False)
        if r.name in seg_mph:
            return (seg_mph[r.name], "schedule-segment", False)
        hit = matched.get(r["nm"]) if isinstance(r["nm"], str) else None
        if hit:
            return (hit[0], "schedule", hit[1])
        osm = parse_mph(head(r.get("maxspeed")))
        if osm is not None:
            return (osm, "osm", False)
        # The 25 mph default applies to town streets, NOT to "state highways",
        # which the ordinance carves out. Where NCDOT has no posted value for a
        # road OSM classes as a state-highway-grade route, we simply do not know
        # -- so keep the existing model speed and mark it unresolved rather than
        # declaring a trunk road 25 mph. (Capital Boulevard has 0.09 mi with no
        # NCDOT value; defaulting that to 25 would be absurd.)
        if r["hw"] in STATE_GRADE:
            return (r["speed"], "unresolved", False)
        return (DEFAULT_MPH, "default25", False)

    got = roads.apply(ordinance_speed, axis=1, result_type="expand")
    roads[["speed_ord", "ord_basis", "ord_approx"]] = got

    it = roads[roads["intown"]]
    print(f"\nIN-TOWN ROAD MILES BY EVIDENCE (total {it.len_mi.sum():.1f} mi)")
    for basis, grp in it.groupby("ord_basis"):
        print(f"  {basis:10s} {grp.len_mi.sum():7.1f} mi  ({len(grp)} edges)")

    cmp = it[it.speed_ord.notna()].copy()
    cmp["agree"] = cmp.speed_ord == cmp.speed
    print(f"\nHYBRID vs CURRENT MODEL")
    print(f"  agree    {cmp[cmp.agree].len_mi.sum():7.1f} mi "
          f"({cmp[cmp.agree].len_mi.sum()/cmp.len_mi.sum()*100:.0f}%)")
    print(f"  disagree {cmp[~cmp.agree].len_mi.sum():7.1f} mi")
    d = cmp[~cmp.agree]
    t = d.groupby([d.speed.astype(int), d.speed_ord.astype(int)]).len_mi.sum()
    print("\n  (model -> hybrid), biggest first:")
    for (a, b), mi in t.sort_values(ascending=False).head(12).items():
        flag = ""
        if a > 25 >= b:
            flag = "   <-- model bars it, evidence allows it"
        elif a <= 25 < b:
            flag = "   <-- model allows it, evidence bars it"
        print(f"     {a:2d} -> {b:2d}   {mi:6.2f} mi{flag}")

    legal_now = it[(it.speed <= 25)].len_mi.sum()
    legal_ord = it[(it.speed_ord <= 25)].len_mi.sum()
    FREEWAY = g["FREEWAY"]
    rd = it[~it["hw"].isin(FREEWAY)]
    ln = rd[rd.speed <= 25].len_mi.sum()
    lo = rd[rd.speed_ord <= 25].len_mi.sum()
    print(f"\n  in-town road miles at <=25 mph (excl. freeway class, barred anyway):")
    print(f"     current model {ln:7.1f} mi   ->   hybrid {lo:7.1f} mi  ({lo-ln:+.1f})")

    if "--csv" in sys.argv:
        out = roads[["nm", "hw", "intown", "len_mi", "speed", "posted",
                     "speed_ord", "ord_basis", "ord_approx"]].copy()
        # Keep the edge index: without it the table cannot be joined back to the
        # graph, and any follow-up analysis silently lines up the wrong rows.
        # Internally the columns are still speed_ord/ord_basis for continuity;
        # export them under names that say what the field actually is now.
        out = out.rename(columns={"speed_ord": "speed_hybrid",
                                  "ord_basis": "speed_basis"})
        out = out.drop(columns=["ord_approx"])
        out.insert(0, "edge_idx", roads.index)
        out["osm_maxspeed"] = roads.index.map(
            lambda i: head(edges.loc[i, "maxspeed"]) if i in edges.index else None)
        out.to_csv("ordinance_speed_compare.csv", index=False)
        print(f"\nwrote ordinance_speed_compare.csv ({len(out)} rows)")


if __name__ == "__main__":
    main()
