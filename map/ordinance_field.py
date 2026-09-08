#!/usr/bin/env python3
"""The speed field itself: evidence-based, not road-class inference.

Precedence, highest first:

    1. NCDOT posted   -- state-maintained roads, which Sec. 30-216 explicitly
                         carves out ("other than on state highways").
    2. Schedule XVII  -- the ordinance names this street, or this stretch of it.
                         Where it speaks, it IS the posting.
    3. OSM maxspeed   -- a surveyed sign. The 25 mph rule is subordinate to
                         posting ("unless otherwise posted"), so a real tag
                         displaces the default.
    4. 25 mph         -- the ordinance default, in town: nothing posted, nothing
                         listed.
    5. road class     -- out of town only, where Ch. 30 does not reach and there
                         is no ordinance default to fall back on.

Lives in its own module because build_islands.py consumes it while
ordinance_speed.py (the comparison harness) execs build_islands.py -- importing
in both directions would be circular.

Cross-checked before adoption: OSM agrees with the ordinance on 79% of the
176.8 mi where both exist. Its 29% agreement with NCDOT looks alarming but 17.7
of the 25.1 conflicting miles are freeway-class road barred regardless of speed;
on ordinary roads the conflict is 7.4 mi, resolved to NCDOT by precedence.
"""
import re

import pandas as pd

import ordinance_segments as seg

SCHEDULE = "ordinance_speed_schedule.csv"
DEFAULT_MPH = 25
# OSM classes standing in for "state highway", which Sec. 30-216 excludes from
# its 25 mph default. Never given the default.
STATE_GRADE = {"motorway", "motorway_link", "trunk", "trunk_link",
               "primary", "primary_link"}

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
    return _PUNCT.sub("", _PARENS.sub("", str(s)).lower()).strip()


def head(v):
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def parse_mph(v):
    """OSM maxspeed is free text: '25 mph', '35', 'NC:urban', 'walk'."""
    v = head(v)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    m = re.search(r"\d+", str(v))
    return float(m.group()) if m else None


def load_schedule(path=SCHEDULE):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#") or line.startswith("mph,"):
                continue
            mph_s, _, entry = line.partition(",")
            entry = entry.strip()
            if entry.startswith('"') and entry.endswith('"'):
                entry = entry[1:-1]
            m = _QUAL.search(entry)
            name = entry[:m.start()] if m else entry
            rows.append({"mph": int(mph_s), "entry": entry,
                         "name": name.strip().rstrip(","),
                         "key": norm(name), "approx": bool(m)})
    return pd.DataFrame(rows)


def match_whole_street(names, sched):
    """OSM street name -> (mph, how) for the un-qualified entries."""
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
            # Listed at several limits on different stretches. Take the highest:
            # falling through permissively here would make an arterial look legal.
            ambiguous.append((nm, sorted(speeds)))
            out[nm] = (max(speeds), "ambiguous-max")
            continue
        out[nm] = (hits[0].mph, how)
    return out, ambiguous


def resolve_segments(edges_proj, coords, known, sched, verbose=True):
    """{edge index -> mph} for the segment-qualified entries."""
    seg_mph, ok, fail = {}, 0, []
    for _, r in sched[sched.approx].iterrows():
        nm = str(r["name"])
        qual = str(r["entry"])[len(nm):].strip().lstrip(",").strip()
        idx, note = seg.resolve_segment(nm, qual, edges_proj, coords, known)
        if idx:
            ok += 1
            for i in idx:
                # Overlaps keep the higher limit -- never resolve permissively.
                seg_mph[i] = max(seg_mph.get(i, 0), int(r["mph"]))
        else:
            fail.append((int(r["mph"]), nm, note))
    if verbose:
        print(f"Schedule XVII segments resolved: {ok}/{int(sched.approx.sum())}")
        for mph_v, nm, note in fail:
            print(f"    UNRESOLVED [{mph_v:2d}] {nm[:34]:36s} {note[:44]}")
    return seg_mph


def build(edges, coords, proj, class_default, verbose=True, segments=True):
    """-> (speed Series, basis Series), indexed like `edges`.

    `edges` needs columns: hw, intown, posted, maxspeed, name, u, v, key, geometry.
    `class_default` maps an OSM highway class to the old inferred mph, used only
    outside the corporate limits.
    """
    sched = load_schedule()
    e = edges.copy()
    e["nm"] = e["name"].map(head)
    ep = e.to_crs(proj)
    known = sorted({n for n in ep["nm"] if isinstance(n, str)})

    # Segment resolution needs graph topology (u/v/key). build_map.py works on
    # plain line features, so it opts out and takes whole-street entries only --
    # 5.0 in-town miles of difference, all of it on arterials NCDOT also covers.
    seg_mph = (resolve_segments(ep, coords, known, sched, verbose=verbose)
               if segments else {})
    whole, ambiguous = match_whole_street(known, sched[~sched.approx])
    if verbose and ambiguous:
        print(f"    listed at multiple limits, resolved to the highest: "
              f"{len(ambiguous)}")

    speeds, bases = [], []
    for i, r in e.iterrows():
        if pd.notna(r.get("posted")):
            speeds.append(float(r["posted"])); bases.append("ncdot"); continue
        if i in seg_mph:
            speeds.append(float(seg_mph[i])); bases.append("schedule-segment"); continue
        hit = whole.get(r["nm"]) if isinstance(r["nm"], str) else None
        if hit and r["intown"]:
            speeds.append(float(hit[0])); bases.append("schedule"); continue
        osm = parse_mph(r.get("maxspeed"))
        if osm is not None:
            speeds.append(osm); bases.append("osm"); continue
        if r["intown"] and r["hw"] not in STATE_GRADE:
            speeds.append(float(DEFAULT_MPH)); bases.append("default25"); continue
        speeds.append(float(class_default.get(r["hw"], 30))); bases.append("class")
    return (pd.Series(speeds, index=e.index, dtype=float),
            pd.Series(bases, index=e.index))
