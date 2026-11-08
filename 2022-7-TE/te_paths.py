"""
2022-7-TE — diverse traffic-engineering paths A and B for SMPTE ST 2022-7 video

ST 2022-7 seamless protection sends the same video stream twice, on path A and
path B, and the receiver rebuilds one clean stream from whichever packets
arrive. That only protects the stream if:

  1. A and B share no POP and no link between the source and destination
     (node- and link-disjoint), so one failure can't take out both, and
  2. the difference in delay between A and B (the skew) fits the receiver's
     buffer.

For a chosen A-end and Z-end, this program enumerates every simple path on the
Backbone1 graph (backbone5_model.py), pairs up the disjoint ones and picks:

    best pair = lowest worst-path latency among pairs within the skew budget
                (ties, to 0.001 ms: lower total latency, fewer hops, lower skew)

If no disjoint pair fits the skew budget, it returns the lowest-skew pair and
marks it as over budget. With --metro-km, POPs within that distance of each
other count as one site (shared risk), so A and B can't both run through, say,
DRT and ASH on the same campus.

Latency is the backbone model's estimate (great-circle x 1.3 x 4.9 us/km), so
skew here is propagation skew only.

Usage
  python te_paths.py --a SEC1 --z LA                   # PNG for one A/Z pair
  python te_paths.py --a SE --z LA --skew-ms 5         # 2-letter labels work too
  python te_paths.py --a SEC1 --z LA --metro-km 25     # metro shared-risk on
  python te_paths.py --all                             # every A/Z pair -> CSV
  python te_paths.py --html te_paths.html              # interactive A/Z picker
"""

import argparse
import csv
import itertools
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.lines import Line2D

import backbone5_model as bm

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "Backbone-Split5.numbers")

SKEW_MS = 10.0        # default A/B skew budget; set to your receivers' buffer
METRO_KM = 0.0        # 0 = off; e.g. 25 groups DRT/ASH, HAL1/SEC1, DEN/DRY
A_COLOR, B_COLOR = "#1f6fd1", "#e0711c"
GREY = "#c4c4c4"
INK = "#1f1f1f"


# ---------------------------------------------------------------- graph

def load_graph(src=SRC):
    backbone, sites = bm.load(src)
    G, _ = bm.build_graph(backbone, sites)
    return nx.Graph(G.subgraph(n for n, d in G.nodes(data=True)
                               if d["role"] == "backbone1"))


def resolve(G, name):
    """Accept a full POP code (SEC1) or its 2-letter label (SE)."""
    if name in G:
        return name
    hits = [n for n in G if bm.node_label(n) == name.upper()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        sys.exit(f"'{name}' matches {', '.join(hits)}; use the full POP code")
    sys.exit(f"Unknown POP '{name}'. POPs: {', '.join(sorted(G))}")


def metro_groups(G, km):
    """POP -> shared-risk group id; POPs within km of each other share one."""
    group = {n: n for n in G}

    def root(n):
        while group[n] != n:
            n = group[n]
        return n

    if km > 0:
        for u, v in itertools.combinations(G.nodes, 2):
            cu, cv = G.nodes[u]["coords"], G.nodes[v]["coords"]
            if cu and cv and bm.great_circle_km(cu, cv) <= km:
                group[root(u)] = root(v)
    return {n: root(n) for n in G}


# ---------------------------------------------------------------- paths

def path_ms(G, p):
    return sum(G.edges[u, v]["latency_ms"] for u, v in zip(p, p[1:]))


def diverse_pair(G, a, z, skew_ms=SKEW_MS, metro_km=METRO_KM):
    """Pick paths A and B for a 2022-7 stream from a to z. Returns a dict."""
    grp = metro_groups(G, metro_km)
    ends = {grp[a], grp[z]}
    paths = []
    for p in nx.all_simple_paths(G, a, z):
        risk = {grp[n] for n in p[1:-1]} - ends
        paths.append((path_ms(G, p), p, frozenset(risk), frozenset(p[1:-1])))
    paths.sort(key=lambda t: t[0])

    best, fallback, fastest, n_pairs = None, None, None, 0
    for (ma, pa, ra, ia), (mb, pb, rb, ib) in itertools.combinations(paths, 2):
        if ia & ib or ra & rb:
            continue
        if len(pa) == 2 and len(pb) == 2:          # both the direct link
            continue
        n_pairs += 1
        skew = abs(ma - mb)
        key = (round(max(ma, mb), 3), round(ma + mb, 3), len(pa) + len(pb), skew)
        if skew <= skew_ms + 1e-9 and (best is None or key < best[0]):
            best = (key, pa, pb, ma, mb)
        if fastest is None or key < fastest[0]:
            fastest = (key, pa, pb, ma, mb)
        fkey = (round(skew, 3), round(max(ma, mb), 3), len(pa) + len(pb))
        if fallback is None or fkey < fallback[0]:
            fallback = (fkey, pa, pb, ma, mb)

    res = dict(a=a, z=z, skew_budget=skew_ms, metro_km=metro_km,
               simple_paths=len(paths), disjoint_pairs=n_pairs,
               shortest=paths[0][1] if paths else None,
               shortest_ms=paths[0][0] if paths else None)
    pick = best or fallback
    if pick is None:
        res.update(status="NO DIVERSE PAIR", path_a=None, path_b=None)
        return res
    _, pa, pb, ma, mb = pick
    if mb < ma:                                    # A is the faster path
        pa, pb, ma, mb = pb, pa, mb, ma
    res.update(path_a=pa, path_b=pb, ms_a=ma, ms_b=mb, skew=mb - ma,
               status="OK" if best else "OVER SKEW BUDGET")
    # fastest disjoint pair ignoring skew, to show what the budget costs
    _, fa, fb, fma, fmb = fastest
    if fastest is not pick:
        res.update(fastest_a=min(fma, fmb), fastest_b=max(fma, fmb),
                   fastest_skew=abs(fma - fmb))
    return res


def fmt_path(p):
    return " - ".join(p) if p else "-"


def describe(r):
    out = [f"2022-7 TE paths {r['a']} -> {r['z']}   "
           f"(skew budget {r['skew_budget']:g} ms"
           + (f", metro shared-risk {r['metro_km']:g} km" if r['metro_km'] else "")
           + ")",
           f"  {r['simple_paths']} simple paths, {r['disjoint_pairs']} disjoint A/B pairs"]
    if not r["path_a"]:
        out.append("  NO DIVERSE PAIR: every path pair shares a POP or link")
        return "\n".join(out)
    out += [f"  Path A  {r['ms_a']:6.2f} ms  {fmt_path(r['path_a'])}",
            f"  Path B  {r['ms_b']:6.2f} ms  {fmt_path(r['path_b'])}",
            f"  Skew    {r['skew']:6.2f} ms  {r['status']}",
            f"  (single shortest path {r['shortest_ms']:.2f} ms: {fmt_path(r['shortest'])})"]
    if "fastest_a" in r:
        out.append(f"  (fastest diverse pair ignoring skew: A {r['fastest_a']:.2f} ms, "
                   f"B {r['fastest_b']:.2f} ms, skew {r['fastest_skew']:.2f} ms)")
    return "\n".join(out)


# ---------------------------------------------------------------- drawing

def draw(G, pos, r, out):
    fig, ax = plt.subplots(figsize=(18, 11))
    fig.patch.set_facecolor("white")
    on = {}
    for key, col in (("path_a", A_COLOR), ("path_b", B_COLOR)):
        p = r.get(key) or []
        for u, v in zip(p, p[1:]):
            on[frozenset((u, v))] = col

    for u, v in G.edges():
        col = on.get(frozenset((u, v)))
        (x1, y1), (x2, y2) = pos[u], pos[v]
        ax.plot([x1, x2], [y1, y2], color=col or GREY, lw=5 if col else 1.6,
                solid_capstyle="round", zorder=2 if col else 1)
        if col:
            t = bm.LABEL_POS.get((u, v), bm.LABEL_POS.get((v, u), 0.5))
            if (v, u) in bm.LABEL_POS:
                t = 1 - t
            ax.text(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
                    bm.fmt_ms(G.edges[u, v]["latency_ms"]), fontsize=9,
                    ha="center", va="center", zorder=5, color=INK,
                    bbox=dict(boxstyle="round,pad=0.22", fc="white", ec=col, lw=1.2))

    ends = {r["a"]: "A-end", r["z"]: "Z-end"}
    for n in G.nodes:
        x, y = pos[n]
        end = n in ends
        ax.scatter([x], [y], s=1300 if end else 950, c="white", zorder=6,
                   edgecolors=INK, linewidths=3.4 if end else 2.0)
        ax.text(x, y, bm.node_label(n), ha="center", va="center", fontsize=11,
                fontweight="bold", color=INK, zorder=7)
        if end:
            ax.text(x, y - 0.62, f"{ends[n]} ({n})", ha="center", va="top",
                    fontsize=10, fontweight="bold", color=INK, zorder=7,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none"))

    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 0.8, max(xs) + 0.8)
    ax.set_ylim(min(ys) - 1.0, max(ys) + 0.8)
    ax.axis("off")
    ax.set_title(f"2022-7-TE — diverse TE paths {r['a']} → {r['z']}",
                 fontsize=16, pad=14, fontweight="bold")

    if r["path_a"]:
        handles = [
            Line2D([0], [0], color=A_COLOR, lw=5,
                   label=f"Path A  {r['ms_a']:.2f} ms   {fmt_path(r['path_a'])}"),
            Line2D([0], [0], color=B_COLOR, lw=5,
                   label=f"Path B  {r['ms_b']:.2f} ms   {fmt_path(r['path_b'])}"),
            Line2D([0], [0], color="none",
                   label=f"Skew {r['skew']:.2f} ms  (budget {r['skew_budget']:g} ms)  —  {r['status']}"),
        ]
    else:
        handles = [Line2D([0], [0], color="none", label="No diverse A/B pair")]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.0),
              ncol=1, frameon=False, fontsize=11, handlelength=3)
    fig.text(0.5, 0.015,
             "A and B share no POP or link between the ends. Latency = great-circle × "
             f"{bm.ROUTE_FACTOR} × {bm.FIBER_US_PER_KM} µs/km, propagation only.",
             ha="center", fontsize=9.5, color="#555555")
    plt.tight_layout(rect=(0, 0.1, 1, 1))
    plt.savefig(out, dpi=160, facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- all pairs

def all_pairs(G, skew_ms, metro_km, out_csv):
    rows = []
    for a, z in itertools.combinations(sorted(G.nodes), 2):
        r = diverse_pair(G, a, z, skew_ms, metro_km)
        rows.append(r)
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["A-end", "Z-end", "Path A", "A ms", "Path B", "B ms",
                    "Skew ms", "Status", "Disjoint pairs"])
        for r in rows:
            w.writerow([r["a"], r["z"], fmt_path(r["path_a"]),
                        f"{r['ms_a']:.2f}" if r["path_a"] else "",
                        fmt_path(r["path_b"]),
                        f"{r['ms_b']:.2f}" if r["path_a"] else "",
                        f"{r['skew']:.2f}" if r["path_a"] else "",
                        r["status"], r["disjoint_pairs"]])
    return rows


# ---------------------------------------------------------------- html

def export_html(G, pos, template, out):
    data = dict(
        nodes=[dict(id=n, label=bm.node_label(n), x=pos[n][0], y=pos[n][1],
                    loc=G.nodes[n].get("location") or "",
                    lat=G.nodes[n]["coords"][0], lon=G.nodes[n]["coords"][1])
               for n in G.nodes],
        links=[dict(a=u, b=v, ms=d["latency_ms"], km=round(d["km"], 1))
               for u, v, d in G.edges(data=True)],
        skew_ms=SKEW_MS, route_factor=bm.ROUTE_FACTOR, fiber=bm.FIBER_US_PER_KM)
    html = open(template).read().replace("/*__DATA__*/null", json.dumps(data))
    open(out, "w").write(html)


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Diverse ST 2022-7 TE paths A and B")
    ap.add_argument("--a", help="A-end POP (code or 2-letter label)")
    ap.add_argument("--z", help="Z-end POP (code or 2-letter label)")
    ap.add_argument("--skew-ms", type=float, default=SKEW_MS,
                    help=f"max A/B delay difference (default {SKEW_MS:g})")
    ap.add_argument("--metro-km", type=float, default=METRO_KM,
                    help="treat POPs within this distance as one site (0 = off)")
    ap.add_argument("--all", action="store_true", help="report every A/Z pair")
    ap.add_argument("--html", help="write the interactive A/Z picker page")
    ap.add_argument("--out", default=os.path.join(HERE, "output"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    G = load_graph()
    pos = bm.layout(G)

    if args.html:
        export_html(G, pos, os.path.join(HERE, "te_paths_template.html"), args.html)
        print(f"Wrote {args.html}")

    if args.all:
        csv_path = os.path.join(args.out, "te_paths_all_pairs.csv")
        rows = all_pairs(G, args.skew_ms, args.metro_km, csv_path)
        bad = [r for r in rows if r["status"] != "OK"]
        print(f"All {len(rows)} A/Z pairs (skew budget {args.skew_ms:g} ms"
              + (f", metro {args.metro_km:g} km" if args.metro_km else "") + ")")
        print(f"  OK: {len(rows) - len(bad)}   over skew budget: "
              f"{sum(r['status'] == 'OVER SKEW BUDGET' for r in rows)}   "
              f"no diverse pair: {sum(r['status'] == 'NO DIVERSE PAIR' for r in rows)}")
        worst = sorted((r for r in rows if r["path_a"]), key=lambda r: -r["skew"])[:8]
        print("  Highest skew pairs:")
        for r in worst:
            print(f"    {r['a']:>4}-{r['z']:<4} A {r['ms_a']:5.2f}  B {r['ms_b']:5.2f}  "
                  f"skew {r['skew']:5.2f} ms  {r['status']}")
        print(f"Wrote {csv_path}")

    if args.a or args.z:
        if not (args.a and args.z):
            ap.error("give both --a and --z")
        a, z = resolve(G, args.a), resolve(G, args.z)
        if a == z:
            ap.error("A-end and Z-end must be different POPs")
        r = diverse_pair(G, a, z, args.skew_ms, args.metro_km)
        print(describe(r))
        png = os.path.join(args.out, f"te_paths_{a}_{z}.png")
        draw(G, pos, r, png)
        print(f"Wrote {png}")
    elif not (args.all or args.html):
        ap.print_help()


if __name__ == "__main__":
    main()
