"""Start positions: greedy max-coverage with an overlap penalty.

For each drone in turn: rank candidate cells (on lines with high p_live, never known-dead) by a cheap proxy
(value reachable nearby), run the real flight planner from the best few, keep the one whose first flight
collects the most expected detections. Cells that flight covers are discounted for the next drones.
"""

import numpy as np

from app.geo import to_lonlat
from app.planner.missions import HOP_RADIUS_M, Network, plan_flight, station

N_EXACT = 40  # candidates per drone evaluated with the real flight planner
OVERLAP = 0.15  # value left in a cell another drone's first flight already covers
MIN_SEPARATION_M = 3000
P_LIVE_START = 0.85
W_STATION = 3.0  # weight of the landed-only value around the first landing (see missions.station): a station serves ~6 hops


def choose(net: Network, n: int, budget_s: float, air_value: np.ndarray, land_value: np.ndarray,
           land_value_after_air: np.ndarray, p_live_cell: np.ndarray, start_ok: np.ndarray,
           speed_inspect: float, speed_transit: float) -> list[tuple[int, object]]:
    """Returns [(cell index, FlightPlan of its first flight)] for n drones."""
    v = air_value.copy()
    reach_m = budget_s * speed_transit / 3
    kernel = np.clip(1 - net.cell_dist / reach_m, 0, None)
    chosen: list[tuple[int, object]] = []
    taken_land = np.zeros(len(v), bool)
    la = land_value_after_air * (p_live_cell >= 0.6)  # what only landed sensors can still find
    for _ in range(n):
        ok = start_ok.copy()
        for c, _plan in chosen:  # spread out: one drone per line, a few km apart
            ok &= (net.cell_dist[c] >= MIN_SEPARATION_M) & (np.array(net.cell_seg) != net.cell_seg[c])
        if not ok.any():
            ok = start_ok.copy()
        st = station(net, la)
        proxy = np.where(ok, kernel @ v + W_STATION * st, -1)
        best = None
        for c in np.argsort(-proxy)[:N_EXACT]:
            if proxy[c] < 0:
                break
            lon_lat = list(to_lonlat(*net.cell_mid[c]))
            plan = plan_flight(net, lon_lat, net.cell_seg[c], budget_s, v, land_value, land_value_after_air,
                               (p_live_cell >= 0.6) & ~taken_land, p_live_cell, speed_inspect, speed_transit)
            val = plan.value + W_STATION * st[plan.land_cell] if plan else 0.0
            if best is None or val > best[2]:
                best = (int(c), plan, val)
        c, plan, _ = best
        chosen.append((c, plan))
        if plan:
            idx = list(plan.cells)
            v[idx] *= OVERLAP
            # one drone per landing line: if that line turns out dead, only one drone has to hop
            taken_land[net.seg_cells[net.cell_seg[plan.land_cell]]] = True
            la[net.cell_dist[plan.land_cell] <= HOP_RADIUS_M] *= OVERLAP
    return chosen

