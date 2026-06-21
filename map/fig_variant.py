import contextily as cx, geopandas as gpd, numpy as np
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
_src = open("build_islands.py").read().split('print("\\n=== GROUNDED reachability')[0]
exec(_src)
POS=cx.providers.CartoDB.Positron
lim=gpd.read_file("corporate_limits.geojson").to_crs(3857)
STREETm=edges["intown"] & edges["hw"].isin(ROAD-FREEWAY)      # streets (counted)
FREEm=edges["intown"] & edges["hw"].isin(FREEWAY)             # limited-access freeways (NOT counted)
ALLSTREET=edges[STREETm]["len_mi"].sum()
streets_red=edges[STREETm].to_crs(3857)
freeways=edges[FREEm].to_crs(3857)
LEG=[Line2D([],[],color="#1a9641",lw=3,label="Reachable by bike (connected network)"),
     Line2D([],[],color="#d7191c",lw=3,label="NOT reachable by bike (gated or stranded)"),
     Line2D([],[],color="#707070",lw=3,label="Limited-access highway (not counted)")]


def panel(ax, col, title=None, legend_loc=None):
    ride=reach(col); main=ride[ride.role=="main"].to_crs(3857); isl=ride[ride.role=="island"].to_crs(3857)
    pc=100*ride[(ride.role=="main") & ride.hw.isin(ROAD-FREEWAY)]["len_mi"].sum()/ALLSTREET
    lim.boundary.plot(ax=ax,color="#333",lw=1.2,ls="--",zorder=2)
    freeways.plot(ax=ax,color="#707070",lw=1.7,zorder=2)        # gray = highway, not a street
    streets_red.plot(ax=ax,color="#d7191c",lw=1.0,zorder=2)     # non-freeway streets, red base
    isl.plot(ax=ax,color="#d7191c",lw=1.3,zorder=3)             # stranded bike infra red
    main.plot(ax=ax,color="#1a9641",lw=1.1,zorder=4)            # connected bike network green
    b=lim.total_bounds; ax.set_xlim(b[0],b[2]); ax.set_ylim(b[1],b[3]); ax.set_aspect("equal")
    cx.add_basemap(ax,source=POS)
    if title:
        ax.set_title(title,fontsize=13)
    if legend_loc:
        ax.legend(handles=LEG,loc=legend_loc,fontsize=9,framealpha=.95)
    ax.set_xticks([]); ax.set_yticks([])
    return pc


# --- two SEPARATE panels: no title, legend lower-right (flexible layout: stack for PDF, side-by-side for web) ---
for fname,col in [("../figures/wake-forest-reachable-today.png","trav_all"),
                  ("../figures/wake-forest-reachable-fix.png","trav_all_sw")]:
    fig,ax=plt.subplots(figsize=(8,9))
    panel(ax,col,title=None,legend_loc="lower right")
    plt.tight_layout(); plt.savefig(fname,dpi=150,bbox_inches="tight"); plt.close()

# --- combined gray with titles (reference / wide screens) ---
fig,axs=plt.subplots(1,2,figsize=(17,9))
for ax,col,lab in [(axs[0],"trav_all","TODAY: bikes barred from >25 mph roads"),
                   (axs[1],"trav_all_sw","WITH the sidewalk fix")]:
    pc=panel(ax,col,legend_loc=("lower right" if ax is axs[0] else None))
    ax.set_title(f"{lab}\n{pc:.0f}% of the town’s streets reachable by bike",fontsize=13)
plt.tight_layout(); plt.savefig("../figures/wake-forest-policy-impact-roads-gray.png",dpi=140,bbox_inches="tight"); plt.close()


def allpc(col):
    r=reach(col); return 100*r[(r.role=="main")&r.hw.isin(ROAD-FREEWAY)]["len_mi"].sum()/ALLSTREET
print("saved today/fix separate panels + combined gray | streets reachable",
      round(allpc("trav_all")),"->",round(allpc("trav_all_sw")),"%")
