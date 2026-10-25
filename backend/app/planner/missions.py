"""Flight planning: greedy routes on the line graph, landing choice, hop after a dead landing.

A flight = a sequence of *runs* (fly along a line from one cell boundary to another, inspecting every cell
in between, at inspect speed), joined by straight transits (fast, inspect nothing), ending with a landing
on a cell of a line we believe is live. Greedy: always take the run with the best
expected-detections / time that still leaves enough battery to reach a landing spot.

All arrays are numpy and precomputed once per network, so one flight plan takes a few ms.
"""

from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import shape
from shapely.ops import substring

from app.geo import project, to_lonlat, to_xy
from app.models import Static, Waypoint

RUN_OVERHEAD_S = 60  # per-run overhead in the score (turning, re-acquiring the line): favours longer runs
TRANSIT_W = 4.0  # transit seconds count this much in the score (opportunity cost: nothing is inspected)
MIN_RUN_VALUE = 0.003  # expected detections; below this a run is not worth flying
FUTURE_RADIUS_M = 4000  # landing choice: value still uninspected within this radius (next flight's start)
# landing score = W_LAND·landed value + W_FUTURE·air value nearby (next flight) + W_STATION·station value + W_LIVE·p_live
W_LAND, W_FUTURE, W_STATION, W_LIVE = 3.0, 0.4, 1.0, 0.3
HOP_RADIUS_M = 3000
STATION_HOPS = 6  # ~ hops a drone can afford in 4 h between its long flights


class Network:
    """Static geometry of the grid, in projected metres. Cells are equal pieces of each segment."""

    def __init__(self, static: Static):
        self.seg_ids: list[str] = [f["properties"]["segment_id"] for f in static.segments["features"]]
        self.seg_line = {f["properties"]["segment_id"]: project(shape(f["geometry"]))
                         for f in static.segments["features"]}
        self.cell_ids: list[str] = []
        self.cell_seg: list[str] = []
        mids = []
        for c in static.cells["features"]:
            pr = c["properties"]
            self.cell_ids.append(pr["cell_id"])
            self.cell_seg.append(pr["segment_id"])
            m = shape(c["geometry"]).interpolate(0.5, normalized=True)
            mids.append(to_xy(m.x, m.y))
        self.cell_idx = {cid: i for i, cid in enumerate(self.cell_ids)}
        self.cell_mid = np.array(mids)
        self.seg_cells: dict[str, list[int]] = {}
        for i, sid in enumerate(self.cell_seg):
            self.seg_cells.setdefault(sid, []).append(i)
        self.cell_len = np.zeros(len(self.cell_ids))

        # boundaries (n+1 per segment) and all runs (ordered boundary pairs a != b on one segment)
        b_xy, r = [], {k: [] for k in ("seg", "a", "b", "entry", "exit", "lo", "hi", "len")}
        self.seg_b0: dict[str, int] = {}
        for sid, idx in self.seg_cells.items():
            line, n = self.seg_line[sid], len(idx)
            step = line.length / n
            self.cell_len[idx] = step
            b0 = len(b_xy)
            self.seg_b0[sid] = b0
            for k in range(n + 1):
                p = line.interpolate(k * step)
                b_xy.append((p.x, p.y))
            for a in range(n + 1):
                for b in range(n + 1):
                    if a == b:
                        continue
                    r["seg"].append(sid)
                    r["a"].append(a)
                    r["b"].append(b)
                    r["entry"].append(b0 + a)
                    r["exit"].append(b0 + b)
                    r["lo"].append(idx[0] + min(a, b))
                    r["hi"].append(idx[0] + max(a, b))
                    r["len"].append(abs(b - a) * step)
        self.b_xy = np.array(b_xy)
        self.r_seg = r["seg"]
        self.r_a, self.r_b = np.array(r["a"]), np.array(r["b"])
        self.r_entry, self.r_exit = np.array(r["entry"]), np.array(r["exit"])
        self.r_lo, self.r_hi = np.array(r["lo"]), np.array(r["hi"])
        self.r_len = np.array(r["len"])
        # cell-to-cell distances (for "value nearby") and boundary-to-cell distances (for "can I still land?")
        d = np.linalg.norm(self.cell_mid[:, None, :] - self.cell_mid[None, :, :], axis=2)
        self.near_kernel = np.clip(1 - d / FUTURE_RADIUS_M, 0, None)
        self.cell_dist = d
        self.b_cell_dist = np.linalg.norm(self.b_xy[:, None, :] - self.cell_mid[None, :, :], axis=2)

    def dist_to_cells(self, xy) -> np.ndarray:
        return np.linalg.norm(self.cell_mid - np.asarray(xy), axis=1)

    def run_coords(self, sid: str, a: int, b: int) -> list[tuple[float, float]]:
        line = self.seg_line[sid]
        step = line.length / len(self.seg_cells[sid])
        return list(substring(line, a * step, b * step).coords)


@dataclass
class FlightPlan:
    route: list[Waypoint]
    cells: set[int] = field(default_factory=set)  # cells inspected from the air on the way
    land_cell: int = -1
    time_s: float = 0.0
    value: float = 0.0  # expected detections (air runs + landing)


def _wp(xy, sid: str | None) -> Waypoint:
    return Waypoint(position=list(to_lonlat(*xy)), segment_id=sid)


def plan_flight(net: Network, start_lonlat: list[float], start_seg: str | None, budget_s: float,
                air_value: np.ndarray, land_value: np.ndarray, land_value_after_air: np.ndarray,
                landable: np.ndarray, p_live_cell: np.ndarray, speed_inspect: float, speed_transit: float,
                dest: int | None = None) -> FlightPlan | None:
    """Greedy flight. `budget_s` = flight seconds available before landing (landing time and reserve already
    subtracted). `landable` = cells we may land on (live line, not claimed). `dest` = forced landing cell."""
    here = np.array(to_xy(*start_lonlat))
    route = [Waypoint(position=list(start_lonlat), segment_id=start_seg)]
    v = air_value.copy()
    covered: set[int] = set()
    t, value = 0.0, 0.0
    if dest is not None:
        end_time = net.b_cell_dist[:, dest] / speed_transit
    else:
        if not landable.any():
            return None
        end_time = net.b_cell_dist[:, landable].min(axis=1) / speed_transit
    t_run = net.r_len / speed_inspect
    t_end = end_time[net.r_exit]
    while True:
        csum = np.concatenate([[0.0], np.cumsum(v)])
        run_v = csum[net.r_hi] - csum[net.r_lo]
        t_tr = np.linalg.norm(net.b_xy - here, axis=1)[net.r_entry] / speed_transit
        ok = (t + t_tr + t_run + t_end <= budget_s) & (run_v > MIN_RUN_VALUE)
        if not ok.any():
            break
        score = np.where(ok, run_v / (TRANSIT_W * t_tr + t_run + RUN_OVERHEAD_S), -1)
        k = int(score.argmax())
        sid, a, b = net.r_seg[k], int(net.r_a[k]), int(net.r_b[k])
        coords = net.run_coords(sid, a, b)
        if np.linalg.norm(np.array(coords[0]) - here) > 1:
            route.append(_wp(coords[0], None))
        route += [_wp(c, sid) for c in coords[1:]]
        cells = range(int(net.r_lo[k]), int(net.r_hi[k]))
        covered.update(cells)
        value += float(run_v[k])
        v[list(cells)] = 0
        t += float(t_tr[k] + t_run[k])
        here = net.b_xy[net.r_exit[k]]

    # landing
    d_land = net.dist_to_cells(here) / speed_transit
    if dest is not None:
        land = dest
    else:
        reach = landable & (t + d_land <= budget_s)
        if not reach.any():
            reach = landable  # should not happen (the runs kept a landing reachable); take the nearest
            land = int(np.where(reach, d_land, np.inf).argmin())
        else:
            lv = land_value.copy()
            idx = list(covered)
            lv[idx] = land_value_after_air[idx]
            st = station(net, land_value_after_air * landable)
            score = W_LAND * lv + W_FUTURE * (net.near_kernel @ v) + W_STATION * st + W_LIVE * p_live_cell
            land = int(np.where(reach, score, -np.inf).argmax())
    lv_land = land_value_after_air[land] if land in covered else land_value[land]
    if len(route) == 1 and dest is None:
        return None  # nothing worth flying
    mid = net.cell_mid[land]
    sid = net.cell_seg[land]
    route += [_wp(mid, None), _wp(mid, sid)]
    t += float(d_land[land])
    return FlightPlan(route=route, cells=covered, land_cell=land, time_s=t, value=value + float(lv_land))


def station(net: Network, la: np.ndarray) -> np.ndarray:
    """Per cell: landed-only value of the best STATION_HOPS cells within hop range (`la` = what only landed
    sensors can still find). A drone charging there can check them with short take-off/land hops."""
    m = np.where(net.cell_dist <= HOP_RADIUS_M, la[None, :], 0.0)
    return -np.sort(-m, axis=1)[:, :STATION_HOPS].sum(axis=1)


def hop(net: Network, start_lonlat: list[float], budget_s: float, landable: np.ndarray, land_value: np.ndarray,
        p_live_cell: np.ndarray, speed_transit: float) -> FlightPlan | None:
    """Just landed on a dead line: transit to the best landable cell within the remaining battery."""
    d = net.dist_to_cells(to_xy(*start_lonlat)) / speed_transit
    reach = landable & (d <= budget_s)
    if not reach.any():
        return None
    score = W_LAND * land_value + W_LIVE * p_live_cell - d / 600  # prefer close: 10 min transit ~ 1 point
    land = int(np.where(reach, score, -np.inf).argmax())
    mid = net.cell_mid[land]
    route = [Waypoint(position=list(start_lonlat), segment_id=None), _wp(mid, None), _wp(mid, net.cell_seg[land])]
    return FlightPlan(route=route, land_cell=land, time_s=float(d[land]), value=float(land_value[land]))
