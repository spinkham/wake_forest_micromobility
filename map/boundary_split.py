#!/usr/bin/env python3
"""Split graph edges where they cross the corporate limits.

Chapter 30 applies inside the corporate limits and not outside, so every edge
has to be wholly one or the other before it can be classified. It wasn't:

    edges["intown"] = edges.geometry.representative_point().within(jg)

decided a whole edge from a single point. For an edge that straddles the
boundary that is a coin flip, and a real one landed badly: a mapper added two
vertices to a 631 ft stretch of Jones Dairy Road, which moved its representative
point 37 m -- from 24 m outside the line to 0.6 m inside it -- flipping the edge
from out-of-town to in-town. That edge was the only link holding a 96.5 mi pod
onto the network, so the published figure moved 8.8 points because of an edit
that changed no road. 1018 edges cross the boundary and 203 sit in the 35-65%
band where a nudge flips them.

The tempting fix -- hold a straddling edge to BOTH rules -- was tried and
rejected: it bars an entire long out-of-town road because a sliver of it crosses
the line, and moved the *today* figure 8 points, a number otherwise stable
across every data change. It trades a coin flip for a systematic bias.

So split the geometry instead. Each piece is then wholly in or wholly out, gets
the rule that actually applies to it, and a rider can traverse the outside
portion and stop at the line -- which is what a rider does.

Both directions of an edge must split at the SAME new nodes or the graph tears
in half, so split points are interned by rounded coordinate.
"""
import itertools

from shapely.geometry import LineString, Point
from shapely.ops import split as shp_split


def _geom(G, u, v, data):
    g = data.get("geometry")
    if g is not None:
        return g
    return LineString([(G.nodes[u]["x"], G.nodes[u]["y"]),
                       (G.nodes[v]["x"], G.nodes[v]["y"])])


def _pieces(geom, poly):
    """Ordered sub-lines of `geom`, each wholly inside or wholly outside `poly`."""
    try:
        parts = shp_split(geom, poly.boundary)
    except Exception:
        return [geom]
    out = [g for g in getattr(parts, "geoms", [parts])
           if g.geom_type == "LineString" and g.length > 0]
    if len(out) < 2:
        return [geom]
    # shapely does not promise order; sort by position along the original line
    out.sort(key=lambda g: geom.project(Point(g.coords[0])))
    return out


def split_at_boundary(G, poly, tol_deg=1e-9, min_frac=0.001):
    """Return G with every boundary-crossing edge cut into in/out pieces.

    `poly` must be in the same CRS as the graph's node coordinates (WGS84 for an
    osmnx graph). Applied after loading the pinned graph rather than baked into
    it, so the pickle stays raw OSM and this stays a deterministic derivation.
    """
    boundary = poly.boundary
    crossing = []
    for u, v, k, data in G.edges(keys=True, data=True):
        g = _geom(G, u, v, data)
        if g.intersects(boundary):
            crossing.append((u, v, k, data, g))
    if not crossing:
        return G, {"crossed": 0, "split": 0, "new_nodes": 0, "new_edges": 0}

    nid = itertools.count(max(int(n) for n in G.nodes) + 1)
    interned = {}                      # rounded coord -> node id

    def node_at(pt):
        key = (round(pt.x, 9), round(pt.y, 9))
        if key not in interned:
            n = next(nid)
            G.add_node(n, x=pt.x, y=pt.y, split=True)
            interned[key] = n
        return interned[key]

    n_split = n_edges = 0
    for u, v, k, data, g in crossing:
        parts = _pieces(g, poly)
        if len(parts) < 2:
            continue
        total = g.length or 1.0
        # Drop hairline slivers: a boundary that merely grazes an edge should not
        # manufacture a 5 cm sub-edge and a node to go with it.
        parts = [p for p in parts if p.length / total >= min_frac]
        if len(parts) < 2:
            continue
        length = data.get("length")
        chain = []
        prev = u
        for i, part in enumerate(parts):
            end = v if i == len(parts) - 1 else node_at(Point(part.coords[-1]))
            d = dict(data)
            d["geometry"] = part
            if length is not None:
                d["length"] = length * (part.length / total)
            d["boundary_split"] = True
            chain.append((prev, end, d))
            prev = end
        G.remove_edge(u, v, k)
        for a, b, d in chain:
            G.add_edge(a, b, **d)
            n_edges += 1
        n_split += 1

    return G, {"crossed": len(crossing), "split": n_split,
               "new_nodes": len(interned), "new_edges": n_edges}
