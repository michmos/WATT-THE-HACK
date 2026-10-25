"""Risk prior + pure belief-update functions.

The smart planner keeps one probability per (cell, fault type); cell risk = 1 - prod(1 - p_t).
Weights and calibration are documented in ASSUMPTIONS.md ("Risk prior").
"""

from shapely import STRtree
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from app.geo import project
from app.models import FaultType, Report, Static

ALL_TYPES: list[FaultType] = ["vegetation", "sag", "ice", "galloping", "hotspot", "broken_conductor"]

# How strongly a report of a fault type points at that type actually being on a line.
W_TYPE: dict[str, float] = {"vegetation": 1.0, "sag": 0.7, "ice": 0.5, "galloping": 1.0, "hotspot": 0.9,
                            "broken_conductor": 0.6}
N_REF = 10  # a report "points at" ~N_REF cells; bigger polygons spread the same evidence thinner
W_VEG = 0.6  # p(vegetation fault) for a cell fully inside forest (30 m buffer 100 % covered)
VEG_BUFFER_M = 30
BASE = {"vegetation": 0.004, "broken_conductor": 0.004, "sag": 0.002, "hotspot": 0.002, "galloping": 0.001,
        "ice": 0.0}  # background rate per cell after a storm, so no cell is exactly 0
STORM_IN, STORM_OUT = 1.0, 0.4  # multiplier for wind-driven types inside / outside the storm footprint
WIND_TYPES = {"vegetation", "galloping", "broken_conductor", "sag"}
DEAD_LINE_P = 0.3  # P(the fault that tripped a dead line sits on that line), spread over its cells
DEAD_LINE_MIN = 0.02  # ...but at least this per cell (short lines)
NEIGHBOUR_P = 0.03  # broken_conductor bump for cells of lines sharing a node with a dead line


def combine(ps) -> float:
    """risk = 1 - prod(1 - p_i) over independent contributions."""
    ok = 1.0
    for p in ps:
        ok *= 1 - p
    return 1 - ok


def update_no_finding(p: float, pd: float) -> float:
    """Inspection without a finding: p <- p(1-pd) / (p(1-pd) + (1-p))."""
    num = p * (1 - pd)
    den = num + (1 - p)
    return num / den if den > 0 else p


def update_finding(p: float, pd: float, pfp: float, confidence: float = 1.0) -> float:
    """Finding: p <- p·pd / (p·pd + (1-p)·pfp).

    `confidence` (the sensor's own certainty, 0..1) softens the evidence: we mix the Bayes posterior
    with the prior, so a shaky detection lands in the "needs confirmation" band instead of > 0.8.
    """
    num = p * pd
    den = num + (1 - p) * pfp
    post = num / den if den > 0 else p
    return confidence * post + (1 - confidence) * p


def update_live(p_live: float, reads_live: bool, accuracy: float) -> float:
    """Live/dead reading with symmetric accuracy (magnetometer ~0.9, current sensor when landed = 1)."""
    like_live = accuracy if reads_live else 1 - accuracy
    like_dead = 1 - accuracy if reads_live else accuracy
    den = p_live * like_live + (1 - p_live) * like_dead
    return p_live * like_live / den if den > 0 else p_live


# ---------- prior ----------

_VEG_CACHE: dict[int, dict[str, float]] = {}


def vegetation_shares(static: Static) -> dict[str, float]:
    """Share (0..1) of each cell's 30 m buffer covered by OSM forest / wood / tree rows. Cached per Static."""
    key = id(static)
    if key not in _VEG_CACHE:
        polys = [project(shape(f["geometry"])).buffer(0) for f in static.vegetation["features"]]
        tree = STRtree(polys)
        out = {}
        for c in static.cells["features"]:
            buf = project(shape(c["geometry"])).buffer(VEG_BUFFER_M)
            hit = sum(polys[i].intersection(buf).area for i in tree.query(buf, predicate="intersects"))
            out[c["properties"]["cell_id"]] = min(1.0, hit / buf.area)
        _VEG_CACHE[key] = out
    return _VEG_CACHE[key]


def report_cells(cell_geoms: dict[str, BaseGeometry], reports: list[Report]) -> dict[str, list[str]]:
    """report_id -> cell ids touching the report polygon (a fault near the edge may sit in a boundary cell)."""
    out = {}
    for r in reports:
        poly = shape(r.polygon)
        out[r.report_id] = [cid for cid, g in cell_geoms.items() if poly.intersects(g)]
    return out


def report_contributions(reports: list[Report], cells_in: dict[str, list[str]]) -> dict[str, dict[str, list[float]]]:
    """cell -> type -> list of report probabilities: confidence × w(type) × min(1, N_REF / n_cells_in_polygon)."""
    out: dict[str, dict[str, list[float]]] = {}
    for r in reports:
        ids = cells_in.get(r.report_id, [])
        if not ids:
            continue
        spread = min(1.0, N_REF / len(ids))
        for cid in ids:
            for t in r.fault_types:
                out.setdefault(cid, {}).setdefault(t, []).append(r.confidence * W_TYPE.get(t, 0.5) * spread)
    return out


def prior(cell_ids: list[str], cell_seg: dict[str, str], cells_in: dict[str, list[str]], reports: list[Report],
          veg_share: dict[str, float], in_storm: dict[str, bool], dead: set[str],
          neighbours: dict[str, set[str]]) -> dict[str, dict[str, float]]:
    """cell -> fault type -> prior probability. Pure function; see ASSUMPTIONS.md for the weights."""
    rep = report_contributions(reports, cells_in)
    n_cells: dict[str, int] = {}
    for cid in cell_ids:
        n_cells[cell_seg[cid]] = n_cells.get(cell_seg[cid], 0) + 1
    near_dead = set().union(*(neighbours.get(s, set()) for s in dead)) - dead if dead else set()
    out = {}
    for cid in cell_ids:
        sid = cell_seg[cid]
        storm = STORM_IN if in_storm[cid] else STORM_OUT
        pt = {}
        for t in ALL_TYPES:
            # generic exposure (background + forest) is scaled by the storm footprint; reports and operator
            # knowledge are direct observations and are not damped by it
            generic = combine([BASE[t], W_VEG * veg_share.get(cid, 0.0) if t == "vegetation" else 0.0])
            p = combine([generic * (storm if t in WIND_TYPES else 1.0)] + rep.get(cid, {}).get(t, []))
            if t == "broken_conductor":
                if sid in dead:
                    p = combine([p, max(DEAD_LINE_MIN, DEAD_LINE_P / n_cells[sid])])
                elif sid in near_dead:
                    p = combine([p, NEIGHBOUR_P])
            pt[t] = p
        out[cid] = pt
    return out
