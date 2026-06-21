#!/usr/bin/env python3
"""Sweep the extra-sidewalk budget to plot detour-vs-mileage. Reuses build_islands
setup (graph, minimal Steiner set, candidate stretches, downtown reference) and
runs the greedy to completion, recording mean/max detour and #pods>0.5mi after
each stretch added. Writes detour_curve.csv + detour_curve.png."""
import csv
import numpy as np
import networkx as nx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# run build_islands up to (but not including) its capped greedy -> gives _with,
# dest, df_len, reps, cands, _det, swinfo, minimal, sw, P0, ...
_src = open("build_islands.py").read().split("cur = list(minimal.index)")[0]
exec(_src)


def stats(d):
    det = [(d.get(r, float("inf")) - df_len[r]) / 1609.34 for r in reps]
    det = [x for x in det if x < float("inf")]
    return float(np.mean(det)), float(max(det)), int(sum(1 for x in det if x > 0.5))


cur = list(minimal.index)
dcur = nx.single_source_dijkstra_path_length(_with(cur), dest, weight="length")
added = 0.0
traj = [(0.0,) + stats(dcur)]
remaining = list(range(len(cands)))
while remaining:
    cur_det = {r: dcur.get(r, float("inf")) - df_len[r] for r in reps}
    best = None
    for kk in remaining:
        idxs, smi = cands[kk]
        if smi <= 0:
            continue
        dt = nx.single_source_dijkstra_path_length(_with(cur + idxs), dest, weight="length")
        red = sum(max(0.0, cur_det[r] - (dt.get(r, float("inf")) - df_len[r])) for r in reps) / 1609.34
        if best is None or red / smi > best[1]:
            best = (kk, red / smi, red, idxs, smi)
    if best is None or best[2] < 0.02:
        break
    kk, _, red, idxs, smi = best
    remaining.remove(kk); cur += idxs; added += smi
    dcur = nx.single_source_dijkstra_path_length(_with(cur), dest, weight="length")
    traj.append((added,) + stats(dcur))

with open("detour_curve.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["extra_mi", "mean_detour_mi", "max_detour_mi", "pods_gt_0p5mi"])
    for t in traj:
        w.writerow([round(t[0], 2), round(t[1], 3), round(t[2], 3), t[3]])

xs = [t[0] for t in traj]
fig, ax1 = plt.subplots(figsize=(9, 6))
ax1.plot(xs, [t[1] for t in traj], "-o", color="#1b9e77", label="mean detour")
ax1.plot(xs, [t[2] for t in traj], "-s", color="#d95f02", label="max detour")
ax1.set_xlabel("extra sidewalk miles added beyond the 4.9-mi minimal set")
ax1.set_ylabel("detour vs full rule (mi)")
ax1.set_ylim(bottom=0)
ax2 = ax1.twinx()
ax2.plot(xs, [t[3] for t in traj], "-^", color="#7570b3", label="pods >0.5 mi")
ax2.set_ylabel("# pods with >0.5 mi detour")
ax2.set_ylim(bottom=0)
ax1.legend(loc="upper right"); ax2.legend(loc="center right")
ax1.set_title("Detour vs added mileage (greedy, beyond minimal) — full rule = +%.1f mi" %
              (sum(swinfo[i][2] for i in sw.index if i not in set(minimal.index)) / 1609.34))
ax1.grid(alpha=0.3)
plt.tight_layout(); plt.savefig("detour_curve.png", dpi=120)
print(f"{len(traj)} points -> detour_curve.csv/.png")
for t in traj:
    print(f"  +{t[0]:5.1f} mi | mean {t[1]:.2f} | max {t[2]:.2f} | pods>0.5: {t[3]}")
