# Network Capacity Planning Model

Traffic-engineered LSP and IGP capacity model with what-if scenario control.

## How the model works

| Sheet | Description |
|-------|-------------|
| **Scenario** | Global levers: per-class growth rates, a global uplift, and the utilization thresholds that drive OK / WARNING / CRITICAL status. |
| **Links** | Circuit inventory: capacity, one-way latency, IGP metric, and a Failed? flag used to simulate outages. |
| **Demands** | Every traffic demand in one table — TE-LSPs and IGP demands. Enter current and new/incremental load. The model grows it, picks primary or protect path, and computes end-to-end latency. |
| **PathPrimary** | Incidence matrix: which links each demand's PRIMARY path traverses. |
| **PathProtect** | Incidence matrix: which links each demand's PROTECT / backup path traverses. |
| **PathEffective** | Calculated. Resolves to primary or protect per demand based on link failures. Do not edit. |
| **LinkLoading** | Calculated. Per-circuit load broken out by TE-LSP, Internet, Cloud and Corporate, with utilization, headroom and status. |
| **Dashboard** | Calculated. Scenario roll-up: totals by class, hotspot count, worst circuits, protect-path exposure, latency SLA breaches. |

## Colour legend

| Colour | Meaning |
|--------|---------|
| Blue text | Hardcoded input — you edit these. |
| Yellow fill | Key assumption / scenario lever — the cells that drive every what-if. |
| Black text | Formula. Do not overwrite. |
| Green text | Pull-through from another sheet. Do not overwrite. |

## Running a what-if

1. **Growth** — Change the per-class growth rates on Scenario (B13:B16), or the global uplift (B6). Every downstream number recalculates.
2. **New services** — Add Gbps to the `New / Incremental` column on Demands (column F) for a known upcoming service.
3. **New demand** — Insert a row inside the demand block on Demands, PathPrimary, PathProtect and PathEffective at the same row number on all four, then extend the SUMPRODUCT ranges on LinkLoading.
4. **New circuit** — Insert a column inside the matrix block (D:O) on all three Path sheets and a row on Links and LinkLoading.
5. **Failure test** — Set `Failed? = Y` on any circuit in Links (column G). Demands whose primary path uses it reroute to protect automatically; demands whose protect path is also down are reported as NO PATH and their traffic is shown as dropped on the Dashboard.

## Assumptions

| Topic | Notes |
|-------|-------|
| **Directionality** | Circuits are modelled as undirected aggregates. For directional planning, duplicate each circuit as two rows (A>Z and Z>A) and split the demands. |
| **Path values** | A `1` in the path matrix means the demand fully traverses that link. Enter a fraction (e.g. `0.5`) to model an ECMP or load-shared split. Latency then becomes a share-weighted average. |
| **Rerouting** | Failure handling is deterministic primary-to-protect switching using the paths you supply. This is NOT an IGP/CSPF solver — it will not compute a new shortest path or model make-before-break, priority preemption, or auto-bandwidth. |
| **IGP metric** | The IGP metric column is documentation only; it does not drive path selection in this model. |
| **Protection** | Only one protect path per demand is modelled. Multi-failure scenarios beyond primary+protect are not resolved. |
| **Latency** | One-way circuit latency, summed along the path. Node/transponder and serialization delay are excluded — add them to the circuit latency if material. |
| **Sample data** | Topology, capacities, latencies and volumes are fabricated for illustration. Not measured data. |
