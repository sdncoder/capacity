"""
2022-7-TE — ST 2022-7 TE-LSP plan for Backbone1

Builds one LSP plan for every POP pair (78 flows, 156 LSPs):

  * LSP A and LSP B carry the same video. Their primary paths are diverse from
    source to destination: no shared link, no shared transit POP, and no two
    transit POPs in the same metro (DRT/ASH, HAL1/SEC1, DEN/DRY within 25 km).
  * A/B skew on the primary paths stays within the skew budget (10 ms).
  * Each flow also gets a standby path C that shares no link with A or B
    (and no transit POP where possible). Both LSPs carry C as a pre-signalled
    secondary, so a failure on A or B switches to C without a CSPF run.
  * If C is also down, the failed LSP re-signals on the shortest surviving
    path that avoids the other leg's links, so the two legs stay apart.

Optimization: for each flow it keeps the diverse A/B pairs within the skew
budget whose slower path is no more than --slack-ms behind the fastest
option, then searches over all flows' choices to minimize the worst link
load in any normal, single or double link failure case. Lower worst-case
load = more video per LSP before any 100G link saturates.

It compares the result with the original plan (lowest-latency pair from
te_paths.py, plain shortest-path reroute, no standby).

Usage
  python lsp_plan.py                    # plan, tables, report and maps in lsp-plan/
  python lsp_plan.py --slack-ms 5       # allow more latency for better load
  python lsp_plan.py --metro-km 0       # turn the metro rule off
"""

import argparse
import csv
import itertools
import os

import networkx as nx
import numpy as np

import backbone5_model as bm
import te_failure as tf
import te_paths as tp

HERE = os.path.dirname(os.path.abspath(__file__))
CAP = 100.0
TOP_K = 12


def ek(u, v):
    return (u, v) if u <= v else (v, u)


def plinks(p):
    return frozenset(ek(u, v) for u, v in zip(p, p[1:]))


def pms(G, p):
    return sum(G.edges[u, v]["latency_ms"] for u, v in zip(p, p[1:]))


# ---------------------------------------------------------------- candidates

def candidates(G, a, z, skew, metro_km, slack):
    grp = tp.metro_groups(G, metro_km)
    ends = {grp[a], grp[z]}
    paths = []
    for p in nx.all_simple_paths(G, a, z):
        inner = frozenset(p[1:-1])
        paths.append(dict(p=p, ms=pms(G, p), links=plinks(p), inner=inner,
                          risk=frozenset(grp[n] for n in inner) - ends))
    paths.sort(key=lambda d: d["ms"])

    pairs = []
    for P, Q in itertools.combinations(paths, 2):
        if P["inner"] & Q["inner"] or P["risk"] & Q["risk"]:
            continue
        if len(P["p"]) == 2 and len(Q["p"]) == 2:
            continue
        pairs.append((P, Q))
    ok = [(P, Q) for P, Q in pairs if abs(P["ms"] - Q["ms"]) <= skew + 1e-9]
    over = not ok
    if over:   # nothing fits the budget: keep the lowest-skew pairs
        ok = sorted(pairs, key=lambda t: (round(abs(t[0]["ms"] - t[1]["ms"]), 3),
                                          max(t[0]["ms"], t[1]["ms"])))[:1]
    best = min(max(P["ms"], Q["ms"]) for P, Q in ok)
    ok = [t for t in ok if max(t[0]["ms"], t[1]["ms"]) <= best + slack + 1e-9]
    ok.sort(key=lambda t: (round(max(t[0]["ms"], t[1]["ms"]), 3),
                           round(t[0]["ms"] + t[1]["ms"], 3)))
    out = []
    for P, Q in ok[:TOP_K]:
        A, B = (P, Q) if P["ms"] <= Q["ms"] else (Q, P)
        used = A["links"] | B["links"]
        # standby C: no shared link with A or B and within the skew budget of
        # both legs (so 2022-7 still merges while a leg runs on it); prefer no
        # shared transit POP, then lowest delay. None -> dynamic diverse reroute.
        def c_key(c):
            return (bool(c["inner"] & (A["inner"] | B["inner"])), round(c["ms"], 3))
        free = sorted((c for c in paths if not c["links"] & used
                       and max(abs(c["ms"] - A["ms"]), abs(c["ms"] - B["ms"])) <= skew + 1e-9),
                      key=c_key)
        C = free[0] if free else None
        out.append(dict(A=A, B=B, C=C, over=over,
                        c_shares_pop=bool(C and C["inner"] & (A["inner"] | B["inner"]))))
    return out


# ---------------------------------------------------------------- failure model

class Sweep:
    def __init__(self, G):
        self.G = G
        self.edges = sorted(ek(u, v) for u, v in G.edges())
        self.idx = {e: i for i, e in enumerate(self.edges)}
        self.cases = ([frozenset()] + [frozenset([e]) for e in self.edges]
                      + [frozenset(c) for c in itertools.combinations(self.edges, 2)])
        self._sp = {}

    def shortest(self, F, a, z, avoid=frozenset()):
        key = (F, a, z, avoid)
        if key not in self._sp:
            H = self.G.copy()
            H.remove_edges_from(F)
            H.remove_edges_from(avoid)
            try:
                p = nx.shortest_path(H, a, z, weight="latency_ms")
                self._sp[key] = (p, plinks(p))
            except nx.NetworkXNoPath:
                self._sp[key] = None
        return self._sp[key]

    def legs(self, a, z, A, B, C, F, policy):
        """Where legs A and B run with links F down. Returns
        ((A links, how), (B links, how))."""
        def up(path):
            return path is not None and not (path["links"] & F)
        if policy == "base":
            res = []
            for P in (A, B):
                if up(P):
                    res.append((P["links"], "primary"))
                else:
                    sp = self.shortest(F, a, z)
                    res.append((sp[1], "reroute"))
            return res
        cur = {}
        for name, P in (("A", A), ("B", B)):
            if up(P):
                cur[name] = (P["links"], "primary")
            elif up(C):
                cur[name] = (C["links"], "standby")
        for name, other in (("A", "B"), ("B", "A")):
            if name in cur:
                continue
            avoid = cur[other][0] if other in cur else frozenset()
            sp = self.shortest(F, a, z, frozenset(avoid)) or self.shortest(F, a, z)
            cur[name] = (sp[1], "reroute")
        return cur["A"], cur["B"]

    def usage(self, a, z, cand, policy):
        U = np.zeros((len(self.cases) + 1, len(self.edges)))   # last row: reserved
        hits = np.zeros(len(self.cases), dtype=np.int8)   # 1 both legs hit, 2 both re-signal
        for ci, F in enumerate(self.cases):
            (la, ha), (lb, hb) = self.legs(a, z, cand["A"], cand["B"], cand["C"], F, policy)
            for e in la:
                U[ci, self.idx[e]] += 1
            for e in lb:
                U[ci, self.idx[e]] += 1
            if ha != "primary" and hb != "primary":
                hits[ci] = 2 if (ha == "reroute" and hb == "reroute") else 1
        # reserved bandwidth: both primaries plus the standby (shared by A and B)
        for P in (cand["A"], cand["B"], cand["C"]):
            if P:
                for e in P["links"]:
                    U[-1, self.idx[e]] += 1
        return U, hits


def objective(L, hits):
    per_case = L.max(axis=1)
    return (round(per_case.max(), 6), hits[0], hits[1], round(per_case.sum(), 6))


def optimize(flows, cands, U, HE, start):
    """U[f][k]: per-case link usage (last row = reserved bandwidth);
    HE[f][k]: double failures that hit both legs of flow f."""
    choice = dict(start)
    L = sum(U[f][choice[f]] for f in flows)
    he = tuple(map(sum, zip(*(HE[f][choice[f]] for f in flows))))
    best = objective(L, he)
    improved = True
    passes = 0
    while improved:
        improved = False
        passes += 1
        for f in flows:
            cur = choice[f]
            sub = lambda x, y: (x[0] - y[0], x[1] - y[1])
            add = lambda x, y: (x[0] + y[0], x[1] + y[1])
            base, hbase = L - U[f][cur], sub(he, HE[f][cur])
            for k in range(len(cands[f])):
                if k == cur:
                    continue
                trial, th = base + U[f][k], add(hbase, HE[f][k])
                obj = objective(trial, th)
                if obj < best:
                    best, cur, L, he, improved = obj, k, trial, th, True
                    sub = lambda x, y: (x[0] - y[0], x[1] - y[1])
            add = lambda x, y: (x[0] + y[0], x[1] + y[1])
            base, hbase = L - U[f][cur], sub(he, HE[f][cur])
            choice[f] = cur
    return choice, L, passes


# ---------------------------------------------------------------- outputs

def fmt(p):
    return " - ".join(p)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ST 2022-7 TE-LSP plan")
    ap.add_argument("--skew-ms", type=float, default=tp.SKEW_MS)
    ap.add_argument("--metro-km", type=float, default=25.0)
    ap.add_argument("--slack-ms", type=float, default=3.0,
                    help="latency the slower leg may give up for better load")
    ap.add_argument("--out", default=os.path.join(HERE, "lsp-plan"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    G = tp.load_graph()
    S = Sweep(G)
    flows = [ek(a, z) for a, z in itertools.combinations(sorted(G.nodes), 2)]

    # original plan: te_paths pair, plain shortest-path reroute, no standby
    base_U, base_hits = {}, {}
    for a, z in flows:
        r = tp.diverse_pair(G, a, z, args.skew_ms, 0)
        cand = dict(A=dict(p=r["path_a"], links=plinks(r["path_a"])),
                    B=dict(p=r["path_b"], links=plinks(r["path_b"])), C=None)
        base_U[(a, z)], base_hits[(a, z)] = S.usage(a, z, cand, "base")
    nfail = len(S.cases)
    dbl = slice(1 + len(S.edges), nfail)
    base_L = sum(base_U.values())[:nfail]
    base_peak = base_L.max()

    # new plan
    cands, U, H = {}, {}, {}
    for a, z in flows:
        cs = candidates(G, a, z, args.skew_ms, args.metro_km, args.slack_ms)
        cands[(a, z)] = cs
        U[(a, z)], H[(a, z)] = zip(*(S.usage(a, z, c, "plan") for c in cs))
    # per candidate: (double failures needing a CSPF re-signal on both legs,
    #                 double failures hitting both legs)
    HE = {f: [(int((h[dbl] == 2).sum()), int((h[dbl] > 0).sum())) for h in H[f]] for f in flows}
    start = {f: 0 for f in flows}
    L0 = sum(U[f][0] for f in flows)
    choice, LR, passes = optimize(flows, cands, U, HE, start)
    L, res = LR[:nfail], LR[nfail]

    peak, peak0 = LR.max(), L0.max()
    x_base, x_lat, x_plan = CAP / base_peak, CAP / peak0, CAP / peak
    ci, ei = np.unravel_index(L.argmax(), L.shape)
    bind_case, bind_link = S.cases[ci], S.edges[ei]
    res_binds = res.max() >= L.max()
    bci, bei = np.unravel_index(base_L.argmax(), base_L.shape)

    nd = nfail - 1 - len(S.edges)
    hits = sum((H[f][choice[f]] > 0).astype(int) for f in flows)
    resig = sum((H[f][choice[f]] == 2).astype(int) for f in flows)
    base_hit = sum((base_hits[f] > 0).astype(int) for f in flows)

    # ---- per-LSP rows
    rows, flow_rows = [], []
    for n, f in enumerate(flows, 1):
        a, z = f
        c = cands[f][choice[f]]
        A, B, C = c["A"], c["B"], c["C"]
        changed = choice[f] != 0
        skew = B["ms"] - A["ms"]
        c_skew = max(abs(C["ms"] - B["ms"]), abs(C["ms"] - A["ms"])) if C else None
        flow_rows.append(dict(n=n, a=a, z=z, A=A, B=B, C=C, skew=skew, c_skew=c_skew,
                              over=c["over"], c_shares_pop=c["c_shares_pop"], changed=changed))
        for leg, P, other in (("A", A, "B"), ("B", B, "A")):
            rows.append({
                "LSP": f"V27-{a}-{z}-{leg}",
                "Flow": f"{a}–{z}",
                "Leg": leg,
                "Head-end": a,
                "Tail-end": z,
                "Primary path (strict ERO)": fmt(P["p"]),
                "Hops": len(P["p"]) - 1,
                "Primary ms": round(P["ms"], 2),
                "Skew vs other leg ms": round(abs(skew), 2),
                "Standby path (strict ERO)": fmt(C["p"]) if C else "dynamic: CSPF excluding other leg",
                "Standby ms": round(C["ms"], 2) if C else "",
                "Bandwidth Gbps": round(x_plan, 2),
                "Setup/Hold priority": "0/0",
                "FRR": "link-protect bypass",
                "Diversity": f"exclude links and transit POPs of V27-{a}-{z}-{other}; metro SRLG",
            })

    with open(os.path.join(args.out, "lsp_plan.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    res_peak = res.max() * x_plan
    flow_hit_events = sum(HE[f][choice[f]][1] for f in flows)
    base_hit_events = sum(int((base_hits[f][dbl] > 0).sum()) for f in flows)

    # ---- per-link worst case under the plan
    worst = {}
    for i, e in enumerate(S.edges):
        col = L[:, i].copy()
        for ci2, F in enumerate(S.cases):
            if e in F:
                col[ci2] = 0
        k = int(col.argmax())
        worst[e] = (L[0, i] * x_plan, col[k] * x_plan, S.cases[k])

    # ---- report
    R = []
    w = R.append
    nm = lambda e: f"{e[0]}–{e[1]}"
    fl = lambda F: " + ".join(nm(e) for e in sorted(F)) or "none"
    changed = [r for r in flow_rows if r["changed"]]
    w("2022-7-TE — TE-LSP plan")
    w(f"{len(flows)} flows, {2 * len(flows)} LSPs. Skew budget {args.skew_ms:g} ms, "
      f"metro SRLG {args.metro_km:g} km, latency slack {args.slack_ms:g} ms.")
    w(f"Optimizer: {passes} passes over {sum(len(c) for c in cands.values())} candidate A/B pairs.")
    w("")
    w("Max video per LSP before any 100G link saturates (normal + all single + all double failures)")
    w(f"  Original plan (lowest latency, shortest-path reroute): {x_base:5.2f} G   "
      f"bind {nm(S.edges[bei])} with {fl(S.cases[bci])}")
    w(f"  New rules, lowest-latency choice:                      {x_lat:5.2f} G")
    w(f"  New plan, load-optimized:                              {x_plan:5.2f} G   "
      + (f"bind reserved bandwidth on {nm(S.edges[int(res.argmax())])}" if res_binds else
         f"bind {nm(bind_link)} with {fl(bind_case)}"))
    w(f"  => {x_plan * len(flows):.0f} G of video, {2 * x_plan * len(flows):.0f} G carried "
      f"(+{(x_plan / x_base - 1) * 100:.0f}% vs original)")
    w(f"  Reserved bandwidth (primaries + shared standby) peaks at {res_peak:.0f} G "
      f"on {nm(S.edges[int(res.argmax())])}")
    w(f"  Worst actual load under any double failure peaks at {L.max() * x_plan:.0f} G "
      f"on {nm(bind_link)} with {fl(bind_case)}")
    w("")
    w(f"Paths changed from lowest-latency: {len(changed)} of {len(flows)} flows")
    lat_cost = [max(r["A"]["ms"], r["B"]["ms"]) for r in flow_rows]
    w(f"Double failures with both legs of some flow down at once: {int((hits[dbl] > 0).sum())} of {nd} "
      f"(original {int((base_hit[dbl] > 0).sum())})")
    w(f"  Flow-hit events (flow x double failure, both legs down): {flow_hit_events} "
      f"(original {base_hit_events})")
    w(f"  ... of which both legs of some flow wait for a CSPF re-signal: {int((resig[dbl] > 0).sum())} "
      f"({sum(HE[f][choice[f]][0] for f in flows)} flow events); "
      f"the rest switch to the pre-signalled standby")
    over = [r for r in flow_rows if r["over"]]
    w(f"Flows over the skew budget on primaries: {', '.join(r['a'] + '–' + r['z'] for r in over) or 'none'}")
    # skew after any single failure (2022-7 must still merge)
    lms = lambda links: sum(G.edges[e]["latency_ms"] for e in links)
    sk_bad, sk_worst = [], (0, None, None)
    for f in flows:
        c = cands[f][choice[f]]
        for F in S.cases[1:1 + len(S.edges)]:
            (la, ha), (lb, hb) = S.legs(*f, c["A"], c["B"], c["C"], F, "plan")
            if ha == hb == "primary":
                continue
            sk = abs(lms(la) - lms(lb))
            if sk > args.skew_ms + 1e-9:
                sk_bad.append((f, F))
            if sk > sk_worst[0]:
                sk_worst = (sk, f, F)
    w(f"Single failures that push a flow's A/B skew over {args.skew_ms:g} ms: {len(sk_bad)} "
      f"(flow x failure) across {len({f for f, _ in sk_bad})} flows; worst {sk_worst[0]:.1f} ms "
      f"on {sk_worst[1][0]}–{sk_worst[1][1]} with {fl(sk_worst[2])} down")
    nos = [r for r in flow_rows if r["C"] is None]
    w(f"Flows with a pre-signalled standby C (link-disjoint, within skew): {len(flows) - len(nos)}")
    w(f"Flows on dynamic diverse reroute (no standby within skew): {len(nos)}")
    sp = [r for r in flow_rows if r["c_shares_pop"]]
    w(f"Flows whose standby shares a transit POP with A or B: {len(sp)}")
    w("")
    w(f"Worst load per link at {x_plan:.2f} G per LSP")
    w(f"  {'Link':12} {'Normal':>8} {'Worst':>8}  Worst when down")
    for e, (n0, wv, F) in sorted(worst.items(), key=lambda kv: -kv[1][1]):
        w(f"  {nm(e):12} {n0:7.1f}G {wv:7.1f}G  {fl(F)}")
    text = "\n".join(R) + "\n"
    print(text, end="")
    open(os.path.join(args.out, "lsp_plan_report.txt"), "w").write(text)

    # ---- markdown table
    md = ["| # | Flow | Path A (primary) | A ms | Path B (primary) | B ms | Skew ms | Standby C | C ms | Re-pathed |",
          "|---:|---|---|---:|---|---:|---:|---|---:|:---:|"]
    for r in flow_rows:
        md.append(f"| {r['n']} | {r['a']}–{r['z']} | {fmt(r['A']['p'])} | {r['A']['ms']:.2f} | "
                  f"{fmt(r['B']['p'])} | {r['B']['ms']:.2f} | {r['skew']:.2f}"
                  f"{' ⚠' if r['over'] else ''} | "
                  f"{fmt(r['C']['p']) if r['C'] else 'dynamic'} | "
                  f"{r['C']['ms']:.2f} | {'✓' if r['changed'] else ''} |" if r['C'] else
                  f"| {r['n']} | {r['a']}–{r['z']} | {fmt(r['A']['p'])} | {r['A']['ms']:.2f} | "
                  f"{fmt(r['B']['p'])} | {r['B']['ms']:.2f} | {r['skew']:.2f}{' ⚠' if r['over'] else ''} | dynamic, diverse from other leg |  | "
                  f"{'✓' if r['changed'] else ''} |")
    open(os.path.join(args.out, "lsp_table.md"), "w").write("\n".join(md) + "\n")

    # ---- maps
    pos = bm.layout(G)
    sub = (f"{len(flows)} flows · 156 LSPs at {x_plan:.2f} G each · A/B diverse + standby C · 100G links")
    norm = {e: L[0, i] * x_plan for i, e in enumerate(S.edges)}
    tf.draw(G, pos, norm, frozenset(), "2022-7-TE LSP plan — normal load", sub,
            os.path.join(args.out, "plan_normal.png"), lambda e: f"{norm[e]:.0f}G")
    wl = {e: worst[e][1] for e in S.edges}
    tf.draw(G, pos, wl, frozenset(), "2022-7-TE LSP plan — worst load per link, any double failure",
            sub + " · label = worst load", os.path.join(args.out, "plan_worst_per_link.png"),
            lambda e: f"{wl[e]:.0f}G")
    bl = {e: L[ci, i] * x_plan for i, e in enumerate(S.edges)}
    tf.draw(G, pos, bl, bind_case, f"2022-7-TE LSP plan — binding failure: {fl(bind_case)}", sub,
            os.path.join(args.out, "plan_binding_failure.png"), lambda e: f"{bl[e]:.0f}G")
    print(f"Wrote lsp_plan.csv, lsp_table.md, lsp_plan_report.txt and 3 maps to {args.out}")
    return dict(x_base=x_base, x_lat=x_lat, x_plan=x_plan, flow_rows=flow_rows)


if __name__ == "__main__":
    main()
