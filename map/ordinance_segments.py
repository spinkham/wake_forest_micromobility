#!/usr/bin/env python3
"""Resolve Schedule XVII segment qualifiers to actual graph edges.

Most Sec. 30-216 entries name a whole street, but 30 of the 94 qualify a stretch
of one -- "from Wait Avenue to East Holding Avenue", "from South Wingate Street
to 0.4 miles west", "to the western corporate limits". Applying those to the
whole named street (what ordinance_speed.py did first) is wrong exactly where it
matters: the 35 and 45 mph entries are arterials whose limit changes partway.

The obvious approach -- merge the street's geometry and linear-reference along it
-- fragments badly. South Franklin Street comes out as 17 disjoint pieces even
after de-duplicating the two directions of every edge, because dual carriageways
and same-named stubs do not share endpoints. So this works on the GRAPH instead:

    1. take the subgraph of edges carrying the street's name
    2. resolve each end of the qualifier to a NODE in that subgraph
    3. the segment is the shortest path between those nodes, within the subgraph

which is immune to how the geometry happens to be split, and follows the road
around curves and through intersections for free.

Endpoint forms handled:
    cross street      "from Wait Avenue", "between A and B", "to its junction with X"
    corporate limits  "to the western corporate limits", "to the town municipal limits"
    turnaround        "to turnaround"  (the dead end)
    offset            "to 0.4 miles west", "0.20 miles north of US-1"

Anything that will not resolve is returned unresolved and stays flagged, rather
than silently falling back to the whole street.
"""
import re

import networkx as nx

MI = 1609.34

# Trailing words that are street TYPES, not part of the name. Directionals are
# deliberately absent -- "East Holding Avenue" and "West Oak Avenue" are distinct
# streets and stripping the direction would merge them.
_TYPES = {"street", "avenue", "road", "drive", "lane", "court", "circle",
          "place", "boulevard", "blvd", "way", "parkway", "trail", "highway",
          "loop", "run", "path", "terrace", "square", "crossing",
          "streets", "avenues", "roads", "drives", "lanes", "courts"}

_DIRPREFIX = re.compile(r"^(?:north|south|east|west|northeast|northwest|"
                        r"southeast|southwest)\s+", re.I)

_PUNCT = re.compile(r"[^a-z0-9 ]")
_PARENS = re.compile(r"\s*\([^)]*\)")
_DIRWORD = re.compile(r"\b(north|south|east|west|northeast|northwest|southeast|southwest)\b", re.I)
_OFFSET = re.compile(r"(\d+(?:\.\d+)?)\s*miles?\s+(north|south|east|west)\b", re.I)

_BARE_DIR = {"north", "south", "east", "west", "northeast", "northwest",
             "southeast", "southwest"}

_COMPASS = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0),
            "northeast": (0.7, 0.7), "northwest": (-0.7, 0.7),
            "southeast": (0.7, -0.7), "southwest": (-0.7, -0.7)}


def norm(s):
    return _PUNCT.sub("", _PARENS.sub("", str(s)).lower()).strip()


def stem(s):
    """Name without its trailing street-type word: 'caveness farms road' ->
    'caveness farms'. Lets the ordinance's 'Caveness Farms Road' find OSM's
    'Caveness Farms Avenue'."""
    w = norm(s).split()
    return " ".join(w[:-1]) if len(w) > 1 and w[-1] in _TYPES else " ".join(w)


_ROUTE = re.compile(r"^\s*(?:us|nc|sr|i)[- ]?\d+[a-z]?(?:\s+business)?\s*", re.I)


def candidates(text):
    """Ordinance street references carry route numbers and aliases that OSM does
    not use as names: 'NC 98 Business (Roosevelt/Wait Avenue)', 'US 1A/South
    Main', 'US 1 (Capital Boulevard)'. Yield the plausible plain-English names,
    best first."""
    t = str(text).strip()
    seen, out = set(), []

    def add(x):
        x = x.strip().strip(",.").strip()
        if x and norm(x) and norm(x) not in seen:
            seen.add(norm(x))
            out.append(x)

    add(t)
    for inner in re.findall(r"\(([^)]*)\)", t):      # "(Roosevelt/Wait Avenue)"
        for part in re.split(r"[/,]", inner):
            add(part)
    bare = _PARENS.sub("", t)
    add(bare)
    for part in re.split(r"[/]", bare):               # "US 1A/South Main"
        add(part)
    stripped = _ROUTE.sub("", bare)                   # "NC 98 Business Wait Ave"
    add(stripped)
    # "East and West Vernon Avenues" -> "Vernon Avenues"
    add(re.sub(r"^\s*(?:north|south|east|west)\s+and\s+(?:north|south|east|west)\s+",
               "", bare, flags=re.I))
    return out


def undirected_stem(n):
    """Stem with any leading directional removed: 'North Main Street' -> 'main'."""
    return _DIRPREFIX.sub("", stem(n)).strip()


def undirected_full(n):
    """Full name bar a leading directional, TYPE WORD KEPT: 'West Oak Avenue' ->
    'oak avenue'. Used for sibling families, where dropping the type word wrongly
    makes 'Oak Drive' a sibling of 'West Oak Avenue' -- a different street, and
    one whose edges break every path through the subgraph."""
    return _DIRPREFIX.sub("", norm(n)).strip()


def resolve_street_name(text, known):
    """Ordinance street reference -> the OSM name it means, or None."""
    by_norm = {norm(n): n for n in known}
    for cand in candidates(text):
        k = norm(cand)
        # "NC-98 East" strips to "East", which fuzzy-matched East Elm Avenue and
        # would have painted 45 mph onto a residential street. A bare directional
        # or a scrap this short identifies nothing.
        if not k or len(k) < 4 or k in _BARE_DIR:
            continue
        if k in by_norm:
            return by_norm[k]
        pref = [n for n in known if norm(n).startswith(k + " ")]
        if len(pref) == 1:
            return pref[0]
        st = stem(cand)
        if st:
            hits = [n for n in known if stem(n) == st]
            if len(hits) == 1:
                return hits[0]
            # "Main Street" -> "North Main Street"/"South Main Street": the
            # ordinance routinely drops the directional. Match ONLY streets that
            # are the same name bar a leading direction -- a substring test here
            # pulls in "Main Divide Drive" and "Paddstowe Main Way", which are
            # different roads entirely.
            sib = [n for n in known if undirected_stem(n) == undirected_stem(cand)]
            if sib:
                return sorted(sib, key=len)[0]
        if pref:
            return sorted(pref, key=len)[0]
    return None


def resolve_street_family(text, known):
    """All OSM names the reference could mean -- the match plus its directional
    siblings. Cross-street references need the whole family: 'Main Street' has to
    find whichever of North/South Main actually meets the street in question."""
    best = resolve_street_name(text, known)
    if not best:
        return []
    fam = [n for n in known if undirected_full(n) == undirected_full(best)]
    return sorted(set(fam) | {best}, key=lambda n: (n != best, len(n)))


_ROUTE_TOK = re.compile(r"\b(us|nc|sr|i)[-\s]?(\d+)\s*(a|alt|alternate|bus|business)?\b", re.I)


def route_refs(text):
    """Route designators in an ordinance reference, normalised to OSM `ref`
    spelling: 'NC-98 East' -> 'NC 98', 'US-1A' -> 'US 1A',
    'NC 98 Business (Wait Avenue)' -> 'NC 98 Business'."""
    out = []
    for m in _ROUTE_TOK.finditer(str(text)):
        sysid, num, suf = m.group(1).upper(), m.group(2), (m.group(3) or "").lower()
        ref = f"{sysid} {num}"
        if suf in ("a",):
            ref = f"{sysid} {num}A"
        elif suf in ("bus", "business"):
            ref = f"{sysid} {num} Business"
        elif suf in ("alt", "alternate"):
            ref = f"{sysid} {num} Alternate"
        if ref not in out:
            out.append(ref)
    return out


def _ref_head(v):
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def select_edges(edges, text, known):
    """Edges the ordinance reference means: the street-name family, plus any
    edges carrying a matching route `ref`. OSM does not name ways "NC 98
    Business" -- it refs them -- so name matching alone cannot find the three
    NC 98 / NC-98 East entries at all. Route selection can over-reach, which is
    safe here for the same reason directional siblings are: the two endpoints
    bound whatever path is taken."""
    fam = resolve_street_family(text, known)
    by_name = street_edges(edges, fam) if fam else edges.iloc[0:0]
    refs = route_refs(text)
    if not refs or "ref" not in edges.columns:
        return by_name
    rf = edges["ref"].map(_ref_head).astype("string")
    mask = False
    for r in refs:
        # "US 1A;NC 98 Business" -- OSM joins concurrent routes with ';'
        mask = mask | rf.fillna("").str.contains(re.escape(r), case=False, regex=True)
    by_ref = edges[mask].copy()
    if not len(by_ref):
        return by_name
    both = edges.loc[sorted(set(by_name.index) | set(by_ref.index))].copy()
    both["_pair"] = [frozenset((u, v)) for u, v in zip(both["u"], both["v"])]
    return both.drop_duplicates(subset=["_pair", "key"])


def street_edges(edges, name):
    """Deduplicated edges carrying this street name, or any of several names (a
    MultiDiGraph holds both directions of every edge; keeping both would double
    every path length)."""
    names = [name] if isinstance(name, str) else list(name)
    s = edges[edges["nm"].isin(names)].copy()
    s["_pair"] = [frozenset((u, v)) for u, v in zip(s["u"], s["v"])]
    return s.drop_duplicates(subset=["_pair", "key"])


def subgraph(sedges, bridge=40.0):
    """Graph of just this street. Its pieces are often not topologically joined
    -- a roundabout, a bridge deck or a short unnamed link splits the name -- so
    components whose endpoints are within `bridge` metres are stitched together.
    Without this, South Franklin, Stadium Drive and East Juniper all report "no
    path" between two points plainly on the same road."""
    g = nx.Graph()
    for _, r in sedges.iterrows():
        g.add_edge(r["u"], r["v"], length=r.geometry.length, idx=r.name)
    comps = list(nx.connected_components(g))
    if len(comps) > 1:
        pos = {}
        for _, r in sedges.iterrows():
            pos[r["u"]] = r.geometry.interpolate(0)
            pos[r["v"]] = r.geometry.interpolate(1, normalized=True)
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                best = None
                for a in comps[i]:
                    for b in comps[j]:
                        if a in pos and b in pos:
                            d = pos[a].distance(pos[b])
                            if d <= bridge and (best is None or d < best[0]):
                                best = (d, a, b)
                if best:
                    g.add_edge(best[1], best[2], length=best[0], idx=None)
    return g


def junction_nodes(sedges, other_edges, tol=20.0):
    """Nodes of the street that touch another street: shared graph nodes first,
    else nodes within `tol` of the other street's geometry (streets that cross
    without a mapped junction node, e.g. a bridge or a missing connection)."""
    if other_edges is None or not len(other_edges):
        return set()
    shared = (set(sedges["u"]) | set(sedges["v"])) & \
             (set(other_edges["u"]) | set(other_edges["v"]))
    if shared:
        return shared
    from shapely.ops import unary_union
    ou = unary_union(other_edges.geometry.values)
    out = set()
    for _, r in sedges.iterrows():
        for nd, pt in ((r["u"], r.geometry.interpolate(0)),
                       (r["v"], r.geometry.interpolate(1, normalized=True))):
            if pt.distance(ou) <= tol:
                out.add(nd)
    return out


def extreme_node(g, coords, direction):
    """The node furthest in a compass direction -- used for 'to the western
    corporate limits' and friends, where the ordinance means the end of the
    street on that side of town."""
    v = _COMPASS.get(direction.lower())
    if v is None or not g:
        return None
    return max(g.nodes, key=lambda n: coords[n][0] * v[0] + coords[n][1] * v[1])


def dead_ends(g):
    return {n for n in g.nodes if g.degree(n) == 1}


def walk_from(g, start, meters, coords, direction=None):
    """Walk `meters` along the street from `start`, preferring the branch whose
    heading matches `direction`. Returns the node reached (or the far end)."""
    if start not in g:
        return None
    best, bestscore = None, None
    v = _COMPASS.get((direction or "").lower())
    for target in g.nodes:
        if target == start:
            continue
        try:
            d = nx.shortest_path_length(g, start, target, weight="length")
        except nx.NetworkXNoPath:
            continue
        score = -abs(d - meters)
        if v is not None:
            dx = coords[target][0] - coords[start][0]
            dy = coords[target][1] - coords[start][1]
            n = (dx * dx + dy * dy) ** 0.5 or 1.0
            if (dx * v[0] + dy * v[1]) / n < 0.2:      # wrong way down the street
                continue
        if bestscore is None or score > bestscore:
            best, bestscore = target, score
    return best


def _truncate_at_comma(t):
    """Drop everything from the first comma that is NOT inside parentheses.
    A blind /,.*$/ turns "North Avenue (US-1A, SR 1933)" into "North Avenue
    (US-1A" -- an unclosed paren that then matches no street at all."""
    depth = 0
    for i, ch in enumerate(t):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            return t[:i].strip()
    return t.strip()


def parse_endpoints(qual):
    """Qualifier text -> (endpoint_a, endpoint_b), each a small dict."""
    q = " " + qual.strip().rstrip(".") + " "
    ql = q.lower()

    def ep(text):
        t = text.strip().strip(",").strip()
        if not t:
            return None
        if re.search(r"corporate limits|municipal limits|town limits|county line", t, re.I):
            d = _DIRWORD.search(t)
            return {"kind": "limits", "dir": d.group(1).lower() if d else None}
        if re.search(r"\bturnaround\b|\bdead end\b", t, re.I):
            return {"kind": "end"}
        m = _OFFSET.search(t)
        if m:
            ref = re.split(r"\bof\b", t, 1)
            return {"kind": "offset", "miles": float(m.group(1)),
                    "dir": m.group(2).lower(),
                    "ref": ref[1] if len(ref) > 1 else None}
        t = re.sub(r"^(?:the )?intersection of\s+", "", t, flags=re.I)
        t = re.sub(r"^its junction with\s+", "", t, flags=re.I)
        t = re.sub(r"\ba point .*$", "", t, flags=re.I).strip().strip(",")
        t = re.sub(r"\btraveling \w+\b.*$", "", t, flags=re.I).strip().strip(",")
        t = _truncate_at_comma(t)
        return {"kind": "cross", "name": t} if t else None

    m = re.search(r"\bbetween\s+(.*?)\s+and\s+(.*)$", q, re.I)
    if m:
        return ep(m.group(1)), ep(m.group(2))
    m = re.search(r"\bbetween\s+(.*?)\s+to\s+(.*)$", q, re.I)     # "between A to B" (sic)
    if m:
        return ep(m.group(1)), ep(m.group(2))
    m = re.search(r"\bfrom\s+(.*?),?\s+(?:east|west|north|south)?w?a?r?d?\s*to\s+(.*)$", q, re.I)
    if m:
        return ep(m.group(1)), ep(m.group(2))
    m = re.search(r"^\s*to its junction with\s+(.*)$", q, re.I)
    if m:
        return {"kind": "street_start"}, ep(m.group(1))
    m = re.search(r"^\s*east from\s+(.*?)\s+to\s+(.*)$", q, re.I)
    if m:
        return ep(m.group(1)), ep(m.group(2))
    return None, None


def resolve_segment(entry_name, qual, edges, coords, known_names):
    """-> (set of edge indices, note). Empty set means unresolved."""
    # Name family plus route ref. Over-including is safe: the endpoints bound it.
    se = select_edges(edges, entry_name, known_names)
    if not len(se):
        return set(), "no edges for street"
    g = subgraph(se)
    a, b = parse_endpoints(qual)
    if a is None and b is None:
        return set(), "qualifier not parsed"

    def node_candidates(ep, other=None):
        """All nodes an endpoint could mean. Returning a SET matters: a cross
        street can meet this one at several places, and picking one arbitrarily
        (lowest node id, say) is a coin flip of exactly the kind that produced
        direction-dependent classifications before. The caller picks the pair
        that yields the shortest path, which is what the ordinance means by
        "from A to B"."""
        if ep is None:
            return set()
        if ep["kind"] == "street_start":
            return dead_ends(g)
        if ep["kind"] == "end":
            return dead_ends(g)
        if ep["kind"] == "limits":
            # Restrict to the component the other endpoint lives in, or the
            # "westernmost node" can land on a disconnected piece of the family
            # and leave no path at all.
            sub = g
            if other:
                reach = set()
                for o in list(other)[:6]:
                    if o in g:
                        reach |= nx.node_connected_component(g, o)
                if reach:
                    sub = g.subgraph(reach)
            n = extreme_node(sub, coords, ep.get("dir") or "north")
            return {n} if n else set()
        if ep["kind"] == "cross":
            ce = select_edges(edges, ep["name"], known_names)
            if not len(ce):
                return set()
            return junction_nodes(se, ce) & set(g.nodes)
        if ep["kind"] == "offset":
            starts = set()
            ref = ep.get("ref")
            if ref:
                ce = select_edges(edges, ref, known_names)
                if len(ce):
                    starts = junction_nodes(se, ce) & set(g.nodes)
            if not starts and other:
                starts = set(other)
            out = set()
            for st_node in sorted(starts)[:6]:
                n = walk_from(g, st_node, ep["miles"] * MI, coords, ep.get("dir"))
                if n:
                    out.add(n)
            return out
        return set()

    ca = node_candidates(a)
    cb = node_candidates(b, other=ca)
    if not ca:
        ca = node_candidates(a, other=cb)
    if not ca or not cb:
        miss = "start" if not ca else "end"
        return set(), f"could not place {miss} of: {qual[:60]}"
    # "from A to B" between two cross streets means the SHORTEST connection --
    # but "to turnaround" and "to the western corporate limits" mean the far end
    # of the road, so those maximise instead. Minimising there picked a 0.02 mi
    # stub off East Holding Avenue instead of the 0.43 mi the ordinance means.
    far = {"end", "limits"}
    maximise = (a and a["kind"] in far) or (b and b["kind"] in far)
    best = None
    for na in sorted(ca)[:12]:
        for nb in sorted(cb)[:12]:
            if na == nb or not nx.has_path(g, na, nb):
                continue
            d = nx.shortest_path_length(g, na, nb, weight="length")
            score = d if maximise else -d
            if best is None or score > best[0]:
                best = (score, na, nb)
    if best is None:
        return set(), "no path along the street between endpoints"
    _, na, nb = best
    path = nx.shortest_path(g, na, nb, weight="length")
    idx = set()
    for u, v in zip(path, path[1:]):
        i = g[u][v]["idx"]
        if i is not None:          # None = a stitched gap, not a real edge
            idx.add(i)
    return idx, "ok"
