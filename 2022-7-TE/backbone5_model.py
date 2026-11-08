"""
2022-7-TE network model (NetworkX)

Builds the Backbone1 core from the "BackboneSplit" sheet of
Backbone-Split5.numbers, plus an AWS East cloud on-ramp homed to SEC1 and
MIA. The Edges and Backbone2 columns are ignored.

Each Backbone1 link carries an estimated one-way latency, derived from the
site locations listed on the "Backbone1" sheet:

    latency_ms = great_circle_km * ROUTE_FACTOR * FIBER_US_PER_KM / 1000

  - great-circle distance between the two sites (haversine)
  - ROUTE_FACTOR  : fiber path is longer than the straight line (1.3 typical)
  - FIBER_US_PER_KM: light in single-mode fiber, ~4.9 us/km (n ~= 1.468)
  - Propagation only: excludes equipment, queueing and serialization delay

Styling
  - Backbone1 nodes : white, dark outline, no labels
  - AWS East        : cloud shape, labelled
  - All links       : straight lines, drawn beneath the nodes
  - All links       : labelled with estimated latency (ms)
  - Every line is checked to clear every node it doesn't terminate on

Usage
  pip install networkx matplotlib numbers-parser
  python backbone5_model.py Backbone-Split5.numbers [output.png]
"""

import math
import sys

import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.patches import FancyBboxPatch
from numbers_parser import Document

SPLIT_SHEET = "BackboneSplit"
SITE_SHEET = "Backbone1"

# Column positions on the BackboneSplit sheet (0-based)
BB1_A, BB1_Z, BB1_LINKS, BB1_BW = 0, 1, 2, 3          # Backbone1 block
DATA_START_ROW = 2                                    # rows 0-1 are headers

# Site / Location columns on the Backbone1 sheet
SITE_COL, LOC_COL = 5, 6

# Latency model
ROUTE_FACTOR = 1.3
FIBER_US_PER_KM = 4.9

# Approximate coordinates for each Location listed on the Backbone1 sheet
LOCATION_COORDS = {
    "Universal City":          (34.1381, -118.3534),
    "Los Angeles":             (34.0522, -118.2437),
    "San Jose":                (37.3382, -121.8863),
    "Denver":                  (39.7392, -104.9903),
    "Dry Creek, South Denver": (39.5800, -104.8700),
    "Chicago":                 (41.8781, -87.6298),
    "Dallas":                  (32.7767, -96.7970),
    "Houston":                 (29.7604, -95.3698),
    "Secaucus NJ":             (40.7895, -74.0565),
    "Stamford":                (41.0534, -73.5387),
    "Halsey NJ":               (40.7357, -74.1724),   # Halsey St, Newark
    "Atlanta":                 (33.7490, -84.3880),
    "Digital Realty, VA":      (39.0300, -77.4700),   # Ashburn campus
    "Ashburn, VA":             (39.0438, -77.4874),
    "Telemundo Center":        (25.8200, -80.3400),   # Miami area
    "North Capital W.D.C.":    (38.9000, -77.0090),
    "30 Rockefeller, NYC":     (40.7587, -73.9787),
    "Woking, London":          (51.3190, -0.5580),
    "Telehouse, London":       (51.5110, -0.0010),
    "Miami":                   (25.7617, -80.1918),
    "Osterley, London":        (51.4880, -0.3510),
}

# The sheet's Location cells have a find/replace artefact ("SEC1aucus",
# "HAL1sey"); map them back to the real place names.
LOCATION_FIXES = {"SEC1aucus NJ": "Secaucus NJ", "HAL1sey NJ": "Halsey NJ"}

# Schematic positions for Backbone1 sites (west -> east), fixed so the
# core reads like a map and no link passes through another node.
BACKBONE_POS = {
    "SNJ": (0.0, 6.0),  "LA": (0.0, 2.0),
    "DEN": (4.0, 7.0),  "DRY": (4.0, 4.0),
    "CHI": (8.0, 8.0),  "DAL": (7.0, 2.0),  "HOU": (8.0, 0.0),
    "HAL1": (12.0, 9.5), "SEC1": (15.0, 8.5),
    "DRT": (11.0, 5.5), "ASH": (14.0, 4.5),
    "ATL": (11.0, 1.5), "MIA": (14.0, -0.5),
}

# Cloud on-ramps: name -> (homing sites, schematic position, (lat, lon))
# AWS East is placed at AWS us-east-1 (Ashburn, VA) for the latency estimate.
CLOUDS = {
    "AWS East": (("SEC1", "MIA"), (17.6, 4.0), (39.0400, -77.4900)),
}
CLOUD_W, CLOUD_H = 1.9, 1.0      # cloud box size in layout units
NODE_CLEAR = 0.45                # min gap between a line and a node it skips


def clean(v):
    return str(v).strip() if v not in (None, "") else None


# ---------------------------------------------------------------- load

def load(path):
    doc = Document(path)
    sheets = {s.name: s for s in doc.sheets}

    rows = list(sheets[SPLIT_SHEET].tables[0].rows(values_only=True))
    backbone = []
    for r in rows[DATA_START_ROW:]:
        a, z = clean(r[BB1_A]), clean(r[BB1_Z])
        if a and z:
            backbone.append((a, z, int(r[BB1_LINKS] or 1), clean(r[BB1_BW])))

    sites = {}
    for r in list(sheets[SITE_SHEET].tables[0].rows(values_only=True))[1:]:
        site, loc = clean(r[SITE_COL]), clean(r[LOC_COL])
        if site and loc:
            sites[site] = LOCATION_FIXES.get(loc, loc)

    return backbone, sites


# ---------------------------------------------------------------- latency

def great_circle_km(p1, p2):
    lat1, lon1 = map(math.radians, p1)
    lat2, lon2 = map(math.radians, p2)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def latency_ms(km):
    return km * ROUTE_FACTOR * FIBER_US_PER_KM / 1000.0


# ---------------------------------------------------------------- graph

def build_graph(backbone, sites):
    G = nx.Graph()
    dupes = []

    for a, z, links, bw in backbone:
        for n in (a, z):
            loc = sites.get(n)
            G.add_node(n, role="backbone1", location=loc,
                       coords=LOCATION_COORDS.get(loc))
        if G.has_edge(a, z):
            dupes.append((a, z))
            continue
        G.add_edge(a, z, role="backbone1", links=links, bw=bw)

    for name, (hubs, _, coords) in CLOUDS.items():
        G.add_node(name, role="cloud", coords=coords)
        for hub in hubs:
            G.add_edge(hub, name, role="cloud")

    # Estimated latency on every link
    for a, z, d in G.edges(data=True):
        ca, cz = G.nodes[a]["coords"], G.nodes[z]["coords"]
        if ca and cz:
            d["km"] = great_circle_km(ca, cz)
            d["latency_ms"] = latency_ms(d["km"])

    return G, dupes


# ---------------------------------------------------------------- layout

def layout(G):
    pos = {}
    for n, d in G.nodes(data=True):
        if d["role"] == "cloud":
            pos[n] = CLOUDS[n][1]
        else:
            pos[n] = BACKBONE_POS.get(n, (7.0, 4.0))
    return pos


def seg_point_dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def check_visibility(G, pos):
    """Every line must clear every node it doesn't terminate on."""
    problems = []
    for u, v in G.edges():
        for n in G.nodes:
            if n in (u, v):
                continue
            clear = NODE_CLEAR + (CLOUD_H / 2 if G.nodes[n]["role"] == "cloud" else 0)
            d = seg_point_dist(pos[n], pos[u], pos[v])
            if d < clear:
                problems.append(f"{u}-{v} passes {d:.2f} from {n}")
    return problems


# Where along each backbone link to place its latency label (0.5 = middle).
# Shifted off the midpoint where two links cross.
LABEL_POS = {
    ("SNJ", "DRY"): 0.30, ("DEN", "LA"): 0.30,
    ("DRY", "HAL1"): 0.35, ("SEC1", "CHI"): 0.62,
    ("ASH", "CHI"): 0.30, ("CHI", "ATL"): 0.40,
    ("CHI", "DAL"): 0.55, ("DRT", "SEC1"): 0.30,
    ("ATL", "DRT"): 0.40, ("HAL1", "SEC1"): 0.5,
}


def fmt_ms(ms):
    return f"{ms:.2f} ms" if ms < 1 else f"{ms:.1f} ms"


# ---------------------------------------------------------------- draw

def draw(G, pos, out):
    bb = [n for n, d in G.nodes(data=True) if d["role"] == "backbone1"]
    clouds = [n for n, d in G.nodes(data=True) if d["role"] == "cloud"]

    fig, ax = plt.subplots(figsize=(18, 11))
    fig.patch.set_facecolor("white")

    # Straight lines, beneath the nodes
    nx.draw_networkx_edges(G, pos, edgelist=list(G.edges()), ax=ax,
                           edge_color="#1f1f1f", width=2.2, arrows=False)

    nx.draw_networkx_nodes(G, pos, nodelist=bb, ax=ax, node_size=520,
                           node_color="white", edgecolors="#1f1f1f",
                           linewidths=2.0)

    # Clouds: rounded box with the cloud name
    for n in clouds:
        x, y = pos[n]
        ax.add_patch(FancyBboxPatch(
            (x - CLOUD_W / 2, y - CLOUD_H / 2), CLOUD_W, CLOUD_H,
            boxstyle="round,pad=0,rounding_size=0.45", fc="white",
            ec="#1f1f1f", lw=2.0, zorder=4))
        ax.text(x, y, n, ha="center", va="center", fontsize=12,
                fontweight="bold", color="#1f1f1f", zorder=5)

    # Latency labels on every link (no backbone node labels)
    for u, v in G.edges():
        ms = G.edges[u, v].get("latency_ms")
        if ms is None:
            continue
        t = LABEL_POS.get((u, v), LABEL_POS.get((v, u), 0.5))
        if (v, u) in LABEL_POS:
            t = 1 - t
        x = pos[u][0] + (pos[v][0] - pos[u][0]) * t
        y = pos[u][1] + (pos[v][1] - pos[u][1]) * t
        ax.text(x, y, fmt_ms(ms), fontsize=8.5, color="#1f1f1f",
                ha="center", va="center", zorder=5,
                bbox=dict(boxstyle="round,pad=0.22", fc="white",
                          ec="#1f1f1f", lw=0.6))

    ax.set_title("2022-7-TE — Backbone1 + AWS East "
                 "(estimated one-way latency)", fontsize=15, pad=14)
    fig.text(0.5, 0.02,
             f"Latency = great-circle distance × {ROUTE_FACTOR} route factor × "
             f"{FIBER_US_PER_KM} µs/km fiber propagation. "
             "Excludes equipment and queueing delay.",
             ha="center", fontsize=9.5, color="#555555")
    ax.axis("off")
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 0.8, max(xs) + CLOUD_W / 2 + 0.6)
    ax.set_ylim(min(ys) - 0.8, max(ys) + 0.8)
    plt.tight_layout(rect=(0, 0.04, 1, 1))
    plt.savefig(out, dpi=200, facecolor="white")
    print(f"Saved {out}")


# ---------------------------------------------------------------- main

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "Backbone-Split5.numbers"
    out = sys.argv[2] if len(sys.argv) > 2 else "backbone5_model.png"

    backbone, sites = load(src)
    G, dupes = build_graph(backbone, sites)

    bb = [n for n, d in G.nodes(data=True) if d["role"] == "backbone1"]
    clouds = [n for n, d in G.nodes(data=True) if d["role"] == "cloud"]
    n_bb = sum(1 for *_, d in G.edges(data=True) if d["role"] == "backbone1")
    print(f"Backbone1 nodes: {len(bb)}   Clouds: {', '.join(clouds)}")
    print(f"Backbone1 links: {n_bb}   Cloud links: {G.number_of_edges() - n_bb}")
    print(f"Connected: {nx.is_connected(G)}")
    core = nx.Graph(G.subgraph(bb))
    print(f"Backbone articulation points: {list(nx.articulation_points(core)) or 'none'}")
    if dupes:
        print("Duplicate rows skipped:", ", ".join(f"{a}-{z}" for a, z in dupes))

    print("\nLink latency (one-way, estimated)")
    rows = sorted(G.edges(data=True), key=lambda e: -e[2].get("latency_ms", 0))
    for u, v, d in rows:
        print(f"  {u:>8} - {v:<8} {d['km']:7.0f} km  {d['latency_ms']:6.2f} ms")

    pos = layout(G)
    bad = check_visibility(G, pos)
    print("\nLine visibility:", "all clear" if not bad else "; ".join(bad))
    draw(G, pos, out)


if __name__ == "__main__":
    main()
