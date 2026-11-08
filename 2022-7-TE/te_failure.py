"""
2022-7-TE — double link failure analysis for ST 2022-7 TE-LSP traffic

Question: if every Backbone1 link is 100G and all traffic rides TE-LSPs, how
much traffic can the network carry without any link saturating when any two
links fail at once?

Traffic model
  * Every POP pair (78 A/Z pairs) carries one ST 2022-7 video flow of X Gbps,
    or the matrix in --demands (CSV: A,Z,Gbps) scaled by a factor X.
  * Each flow is two TE-LSPs, A and B, each carrying the full X Gbps, on the
    diverse paths te_paths.py picks (no shared POP or link, skew budget).
  * Steady state: both LSPs up on their planned paths.

Failure behaviour (--policy)
  reroute    (default) an LSP that loses a link re-signals on the
             lowest-latency path left in the surviving network (CSPF with
             no bandwidth constraint). This is the normal TE-LSP behaviour
             and the case that can push links over capacity.
  no-reroute the failed leg stays down; the stream survives on the other leg
             (2022-7 still delivers it if one leg is up). Load only drops.

Because load is linear in X, the program sweeps every failure case once with
1 Gbps per flow, finds the most loaded link in each case, and

    X_max = link capacity / worst per-flow load on any link in any case

It reports X_max for no failure, every single failure and every double
failure, which pair of failures is binding, and which flows would lose both
legs at once (a hit until the LSPs re-signal).

Usage
  python te_failure.py                        # uniform full mesh, reroute
  python te_failure.py --policy no-reroute
  python te_failure.py --demands my.csv       # custom A,Z,Gbps matrix
  python te_failure.py --skew-ms 5 --metro-km 25
"""

import argparse
import csv
import itertools
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.lines import Line2D

import backbone5_model as bm
import te_paths as tp

HERE = os.path.dirname(os.path.abspath(__file__))
CAP_GBPS = 100.0
INK = "#1f1f1f"
UTIL_COLORS = [(0.70, "#2e7d32"), (0.85, "#e08a00"), (1.0001, "#c62828")]   # <70, <85, <=100
FAILED = "#9a9a9a"


def ekey(u, v):
    return (u, v) if u <= v else (v, u)


def load_demands(G, path):
    if not path:
        return {ekey(a, z): 1.0 for a, z in itertools.combinations(sorted(G.nodes), 2)}
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            a, z = tp.resolve(G, r["A"].strip()), tp.resolve(G, r["Z"].strip())
            out[ekey(a, z)] = out.get(ekey(a, z), 0.0) + float(r["Gbps"])
    return out


def plan_lsps(G, demands, skew_ms, metro_km):
    """One A and one B LSP per flow, on te_paths' diverse pair."""
    lsps = []
    for (a, z), gbps in demands.items():
        r = tp.diverse_pair(G, a, z, skew_ms, metro_km)
        for leg in ("a", "b"):
            p = r[f"path_{leg}"]
            lsps.append(dict(flow=(a, z), leg=leg.upper(), gbps=gbps, path=p,
                             links=[ekey(u, v) for u, v in zip(p, p[1:])]))
    return lsps


def run_case(G, lsps, failed, policy, sp_cache):
    """Per-link load (Gbps) with `failed` links down."""
    load = {ekey(u, v): 0.0 for u, v in G.edges()}
    down_legs, dropped, rerouted = {}, 0.0, 0
    H = None
    for l in lsps:
        if not failed.intersection(l["links"]):
            for e in l["links"]:
                load[e] += l["gbps"]
            continue
        down_legs.setdefault(l["flow"], set()).add(l["leg"])
        if policy == "no-reroute":
            continue
        key = (failed, l["flow"])
        if key not in sp_cache:
            if H is None:
                H = G.copy()
                H.remove_edges_from(failed)
            try:
                p = nx.shortest_path(H, *l["flow"], weight="latency_ms")
                sp_cache[key] = [ekey(u, v) for u, v in zip(p, p[1:])]
            except nx.NetworkXNoPath:
                sp_cache[key] = None
        new = sp_cache[key]
        if new is None:
            dropped += l["gbps"]
            continue
        rerouted += 1
        for e in new:
            load[e] += l["gbps"]
    both = sorted(f for f, legs in down_legs.items() if len(legs) == 2)
    peak_link = max(load, key=load.get)
    return dict(failed=failed, load=load, peak=load[peak_link], peak_link=peak_link,
                both_down=both, rerouted=rerouted, dropped=dropped)


def sweep(G, lsps, policy):
    edges = sorted(ekey(u, v) for u, v in G.edges())
    cache = {}
    base = run_case(G, lsps, frozenset(), policy, cache)
    singles = [run_case(G, lsps, frozenset([e]), policy, cache) for e in edges]
    doubles = [run_case(G, lsps, frozenset(c), policy, cache)
               for c in itertools.combinations(edges, 2)]
    return base, singles, doubles


def x_max(cases):
    worst = max(cases, key=lambda c: c["peak"])
    return CAP_GBPS / worst["peak"] if worst["peak"] else math.inf, worst


def worst_per_link(G, cases):
    out = {ekey(u, v): (0.0, None) for u, v in G.edges()}
    for c in cases:
        for e, v in c["load"].items():
            if e not in c["failed"] and v > out[e][0]:
                out[e] = (v, c["failed"])
    return out


# ---------------------------------------------------------------- drawing

def util_color(u):
    for lim, col in UTIL_COLORS:
        if u < lim:
            return col
    return UTIL_COLORS[-1][1]


def draw(G, pos, loads, failed, title, subtitle, out, label_fn):
    fig, ax = plt.subplots(figsize=(18, 11.5))
    fig.patch.set_facecolor("white")
    for u, v in G.edges():
        e = ekey(u, v)
        (x1, y1), (x2, y2) = pos[u], pos[v]
        down = e in failed
        u_ = loads.get(e, 0) / CAP_GBPS
        col = FAILED if down else util_color(u_)
        ax.plot([x1, x2], [y1, y2], color=col, lw=2.2 if down else 2.5 + 4.5 * min(u_, 1),
                ls=(0, (4, 3)) if down else "-", solid_capstyle="round", zorder=1)
        t = bm.LABEL_POS.get((u, v), bm.LABEL_POS.get((v, u), 0.5))
        if (v, u) in bm.LABEL_POS:
            t = 1 - t
        ax.text(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, "FAILED" if down else label_fn(e),
                fontsize=9, ha="center", va="center", zorder=5, color=INK,
                fontweight="bold" if down else "normal",
                bbox=dict(boxstyle="round,pad=0.22", fc="white", ec=col, lw=1.2))
    for n in G.nodes:
        x, y = pos[n]
        ax.scatter([x], [y], s=950, c="white", zorder=6, edgecolors=INK, linewidths=2.0)
        ax.text(x, y, bm.node_label(n), ha="center", va="center", fontsize=11,
                fontweight="bold", color=INK, zorder=7)
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 0.8, max(xs) + 0.8)
    ax.set_ylim(min(ys) - 0.8, max(ys) + 0.8)
    ax.axis("off")
    ax.set_title(title, fontsize=16, fontweight="bold", loc="left", pad=26)
    ax.text(0, 1.015, subtitle, transform=ax.transAxes, fontsize=11, color="#444444")
    handles = [Line2D([0], [0], color=UTIL_COLORS[0][1], lw=4, label="< 70% of 100G"),
               Line2D([0], [0], color=UTIL_COLORS[1][1], lw=4, label="70–85%"),
               Line2D([0], [0], color=UTIL_COLORS[2][1], lw=4, label="85–100%"),
               Line2D([0], [0], color=FAILED, lw=2.2, ls=(0, (4, 3)), label="Failed link")]
    ax.legend(handles=handles, loc="lower left", ncol=4, frameon=False, fontsize=10.5,
              bbox_to_anchor=(0.0, -0.05))
    plt.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- report

def name(e):
    return f"{e[0]}–{e[1]}"


def fails(f):
    return " + ".join(name(e) for e in sorted(f)) if f else "none"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Double link failure analysis, 2022-7 TE-LSPs")
    ap.add_argument("--policy", choices=["reroute", "no-reroute"], default="reroute")
    ap.add_argument("--demands", help="CSV with columns A,Z,Gbps (default: 1 flow per POP pair)")
    ap.add_argument("--skew-ms", type=float, default=tp.SKEW_MS)
    ap.add_argument("--metro-km", type=float, default=tp.METRO_KM)
    ap.add_argument("--out", default=os.path.join(HERE, "output"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    G = tp.load_graph()
    pos = bm.layout(G)
    demands = load_demands(G, args.demands)
    lsps = plan_lsps(G, demands, args.skew_ms, args.metro_km)
    base, singles, doubles = sweep(G, lsps, args.policy)

    x0, _ = x_max([base])
    x1, w1 = x_max([base] + singles)
    x2, w2 = x_max([base] + singles + doubles)
    unit = "Gbps per flow" if not args.demands else "x the demand matrix"
    flows = len(demands)
    offered = sum(demands.values())

    R = []
    w = R.append
    w("2022-7-TE — double link failure analysis")
    w(f"Links: {G.number_of_edges()} x {CAP_GBPS:.0f}G   POPs: {G.number_of_nodes()}   "
      f"edge connectivity {nx.edge_connectivity(G)} (no double failure splits the network)")
    w(f"Traffic: {flows} ST 2022-7 flows "
      + ("(one per POP pair, equal size)" if not args.demands else f"from {args.demands}")
      + f", each as TE-LSP A + TE-LSP B on diverse paths")
    w(f"Paths: te_paths.py, skew budget {args.skew_ms:g} ms"
      + (f", metro {args.metro_km:g} km" if args.metro_km else ""))
    w(f"Failure policy: {args.policy} "
      + ("(failed LSP re-signals on the lowest-latency surviving path)" if args.policy == "reroute"
         else "(failed leg stays down; stream rides the other leg)"))
    w(f"Cases: 1 normal, {len(singles)} single failures, {len(doubles)} double failures")
    w("")
    w("Highest traffic with no link over 100G")
    w(f"  {'Condition':22} {'Max per flow':>14} {'Video total':>12} {'Carried':>10}  Binding link / failure")
    for label, x, c in (("No failure", x0, base), ("Any single failure", x1, w1),
                        ("Any double failure", x2, w2)):
        w(f"  {label:22} {x:10.2f} {'G' if not args.demands else 'x':<3} {x * offered:9.0f} G "
          f"{2 * x * offered:8.0f} G  {name(c['peak_link'])} with {fails(c['failed'])}")
    w(f"  (Video total = unique video across all flows; Carried = A + B copies on the backbone.)")
    w("")
    w(f"Answer: keep each flow at or below {x2:.2f} {unit} "
      f"({x2 * offered:.0f} G of video, {2 * x2 * offered:.0f} G carried) and no link "
      f"saturates under any double link failure.")
    w(f"  Binding case: {fails(w2['failed'])} down puts {name(w2['peak_link'])} at 100%.")
    w(f"  Double-failure headroom costs {(1 - x2 / x0) * 100:.0f}% of the no-failure limit "
      f"({x0:.2f} -> {x2:.2f}).")
    w("")

    # at X_max, normal-state and per-link worst
    wl = worst_per_link(G, [base] + singles + doubles)
    w(f"Per-link load at {x2:.2f} {unit}: normal and worst case under any double failure")
    w(f"  {'Link':12} {'Normal':>8} {'Worst':>8}  Worst when down")
    for e, (v, f) in sorted(wl.items(), key=lambda kv: -kv[1][0]):
        w(f"  {name(e):12} {base['load'][e] * x2:7.1f}G {v * x2:7.1f}G  {fails(f) if f else 'normal'}")
    w("")

    w("Worst double failures (highest peak link)")
    for c in sorted(doubles, key=lambda c: -c["peak"])[:10]:
        w(f"  {fails(c['failed']):28} -> {name(c['peak_link']):10} {c['peak'] * x2:6.1f}G "
          f"at limit, {c['rerouted']} LSPs rerouted, {len(c['both_down'])} flows lost both legs")
    w("")

    hit = [c for c in doubles if c["both_down"]]
    w(f"Flows losing both A and B at once: in {len(hit)} of {len(doubles)} double failures")
    if args.policy == "reroute":
        w("  (a video hit until the LSPs re-signal; one leg down alone is hitless under 2022-7)")
    else:
        w("  (with no reroute these flows stay down for the duration of the failure)")
    worst_hit = sorted(hit, key=lambda c: -len(c["both_down"]))[:6]
    for c in worst_hit:
        w(f"  {fails(c['failed']):28} {len(c['both_down']):3} flows: "
          + ", ".join(f"{a}–{z}" for a, z in c["both_down"][:6])
          + (" ..." if len(c["both_down"]) > 6 else ""))
    nohit = [c for c in doubles if not c["both_down"]]
    w(f"  {len(nohit)} double failures hit no flow on both legs.")
    any_single_hit = [c for c in singles if c["both_down"]]
    w(f"  Single failures that hit both legs of a flow: {len(any_single_hit)} (A and B are link-disjoint)")
    w("")
    w("Assumptions: estimated propagation latency drives path choice; reroute ignores bandwidth "
      "(no CSPF bandwidth reservation), so this is the load the network would see if every LSP "
      "takes its shortest surviving path. All links are 100G.")
    text = "\n".join(R) + "\n"
    print(text, end="")
    tag = "" if args.policy == "reroute" else "_noreroute"
    with open(os.path.join(args.out, f"failure_report{tag}.txt"), "w") as f:
        f.write(text)

    with open(os.path.join(args.out, f"failure_cases{tag}.csv"), "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["Failed links", "Peak link", "Peak load per 1G flow (G)",
                     f"Peak load at {x2:.2f} (G)", "LSPs rerouted", "Flows with both legs down"])
        for c in [base] + singles + doubles:
            cw.writerow([fails(c["failed"]), name(c["peak_link"]), round(c["peak"], 3),
                         round(c["peak"] * x2, 1), c["rerouted"], len(c["both_down"])])

    sub = (f"{flows} ST 2022-7 flows as TE-LSP A + B · {x2:.2f} {unit} "
           f"(the double-failure limit) · 100G links · policy: {args.policy}")
    draw(G, pos, {e: v * x2 for e, v in base["load"].items()}, frozenset(),
         "2022-7-TE — normal load at the double-failure limit", sub,
         os.path.join(args.out, f"failure_normal{tag}.png"),
         lambda e: f"{base['load'][e] * x2:.0f}G")
    draw(G, pos, {e: v * x2 for e, v in w2["load"].items()}, w2["failed"],
         f"2022-7-TE — worst double failure: {fails(w2['failed'])}", sub,
         os.path.join(args.out, f"failure_worst_double{tag}.png"),
         lambda e: f"{w2['load'][e] * x2:.0f}G")
    draw(G, pos, {e: wl[e][0] * x2 for e in wl}, frozenset(),
         "2022-7-TE — worst load per link under any double failure", sub
         + " · label = worst load, colour = worst utilization",
         os.path.join(args.out, f"failure_worst_per_link{tag}.png"),
         lambda e: f"{wl[e][0] * x2:.0f}G")
    print(f"Wrote report, cases CSV and 3 maps to {args.out}")


if __name__ == "__main__":
    main()
