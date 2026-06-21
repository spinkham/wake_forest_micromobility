#!/usr/bin/env python3
"""Count side-road intersections along the legalized corridors, to size signage
options that account for riders entering mid-corridor and crossing conflicts.
Reuses build_islands (graph, minimal/knee/parity edge sets)."""
import numpy as np
import networkx as nx

exec(open("build_islands.py").read())   # -> edges, minimal, knee_idx, parity_idx, G, ROAD, _xy, PROJ

Gd = nx.Graph()
for _, r in edges[edges["hw"].isin(ROAD)].iterrows():
    Gd.add_edge(int(r["u"]), int(r["v"]), hw=r["hw"])
deg = dict(Gd.degree())
MAJOR = {"primary", "secondary", "tertiary", "primary_link", "secondary_link",
         "tertiary_link", "trunk", "trunk_link"}


def xy(n):
    p = _xy.loc[n].geometry
    return (p.x, p.y)


def clust(ns, R=45):
    if not ns:
        return 0
    pts = np.array([xy(n) for n in ns])
    used = np.zeros(len(pts), bool)
    k = 0
    for i in range(len(pts)):
        if used[i]:
            continue
        used |= (np.hypot(pts[:, 0] - pts[i, 0], pts[:, 1] - pts[i, 1]) <= R)
        k += 1
    return k


levels = {"minimal": list(minimal.index),
          "knee": list(minimal.index) + knee_idx,
          "parity": list(minimal.index) + knee_idx + parity_idx}

print("\n=== signage options: posts (clustered 45m) ===")
print(f"  {'level':8s} | A ends-only | B ends+ALL side-rd xings | C ends+MAJOR side-rds")
for lv, L in levels.items():
    sub = edges.loc[L]
    CG = nx.Graph()
    for _, r in sub.iterrows():
        CG.add_edge(int(r["u"]), int(r["v"]))
    Lset = {frozenset((int(r["u"]), int(r["v"]))) for _, r in sub.iterrows()}
    cn = set(CG.nodes)
    term = [n for n in cn if CG.degree(n) == 1]
    tset = set(term)
    inter = [n for n in cn if n not in tset and deg.get(n, 0) >= 3]   # mid-corridor crossings
    major = []
    for n in inter:
        for nb in Gd.neighbors(n):
            if frozenset((n, nb)) in Lset:
                continue
            if Gd[n][nb].get("hw") in MAJOR:
                major.append(n)
                break
    A, B, C = clust(term), clust(term + inter), clust(term + major)
    print(f"  {lv:8s} |   {A:4d}     |  {B:4d}  ({len(inter)} xings)      |  {C:4d}  ({len(major)} major)")
