"""SmartPlanner (M2): risk prior per fault type, Bayes updates, greedy start positions and risk-driven routes.

Belief = P[cell, fault type]. The planner values a cell by the expected number of detections a pass would
give (sum over types of p_t · p_detect_t, using the published sensor specs), so it automatically prefers
- cells with high prior (reports, forest, storm, dead lines),
- uncertain findings (0.4-0.8: a second look is worth a lot),
- LANDINGS where landed-only sensors matter (galloping, sag; or hotspots when the air sensor is weak).
Cells already above the crew threshold are worth nothing more (we know enough there).
"""

import os

import numpy as np
from shapely.geometry import shape

from app.geo import to_lonlat, to_xy
from app.models import (CellState, DroneParams, DroneState, Event, FaultType, Observation, OperatorInputs,
                        SegmentState, SensorParams, StartPosition, Static, Waypoint)
from app.planner import Planner
from app.planner import missions, start_positions
from app.planner.missions import Network
from app.planner.recommend import CONFIRM, CONFIRM_CONF
from app.planner.risk import (ALL_TYPES, DEAD_LINE_MIN, DEAD_LINE_P, NEIGHBOUR_P, prior, report_cells,
                              update_finding, update_live, update_no_finding, vegetation_shares)

P_LIVE_PRIOR = 0.9
P_LIVE_NEAR_DEAD = 0.8  # a line sharing a node with a dead line is a bit more likely dead too
LANDABLE_P_LIVE = 0.6  # one "dead" magnetometer reading (acc. 0.9) drops 0.9 -> 0.5: not landable any more
RESERVE_MIN = 5
TAKEOFF_MIN = 20  # charge to this before a normal flight ...
CONFIRM_TAKEOFF_MIN = 10  # ... but go earlier for a landing that confirms / checks a landed-only fault
# landing targets, in landed-only expected detections per battery minute (an air flight makes ~0.03-0.2/min)
TARGET_RATE = 0.02  # while charging: worth a short hop (e.g. galloping / sag check in a report area)
CONFIRM_RATE = 0.04  # when charged: worth forcing the flight to end there (uncertain finding to confirm)
MIN_FLIGHT_VALUE = 0.03
CREW = 0.8  # above this (and confirmed) a fault is known: no more value in looking again
CLEAR_TOP = 0.1  # a finding whose belief dropped below this is no longer shown as top fault
INSPECTED_RANK = {"none": 0, "air": 1, "landed": 2}
# Land on a finding (confirm_route): a flying drone whose air finding needs confirmation lands on that cell
# right away (landed sensors confirm with one confident detection). LAND_ON_FINDING=0 = old behaviour.
LAND_ON_FINDING = os.environ.get("LAND_ON_FINDING", "1") != "0"
MIN_CONFIRM_VALUE = 0.1  # P(a landing confirms the finding) below this: not worth breaking off the flight
HANDOFF_RADIUS_M = 2000  # a drone parked this close to a finding hops over to confirm it (instead of the flyer)
N_NET: dict[int, Network] = {}  # Network is pure geometry: build once per Static


class SmartPlanner(Planner):
    def __init__(self, static: Static):
        self.static = static
        if id(static) not in N_NET:
            N_NET[id(static)] = Network(static)
        self.net = net = N_NET[id(static)]
        self.n = len(net.cell_ids)
        self.cell_seg_of = dict(zip(net.cell_ids, net.cell_seg))
        self.segs = net.seg_ids
        self.veg = vegetation_shares(static)
        storm = shape(static.storm["geometry"])
        self.cell_geoms = {c["properties"]["cell_id"]: shape(c["geometry"]) for c in static.cells["features"]}
        self.in_storm = {cid: storm.contains(g.interpolate(0.5, normalized=True)) for cid, g in self.cell_geoms.items()}
        node_segs: dict[str, set[str]] = {}
        for f in static.segments["features"]:
            pr = f["properties"]
            for node in (pr["from_node"], pr["to_node"]):
                node_segs.setdefault(node, set()).add(pr["segment_id"])
        self.neighbours: dict[str, set[str]] = {s: set() for s in self.segs}
        for group in node_segs.values():
            for s in group:
                self.neighbours[s] |= group - {s}
        self.cell_seg_idx = np.array([self.segs.index(s) for s in net.cell_seg])
        self.sensors: dict[str, SensorParams] = {}
        self.drone = DroneParams()
        self.land_on_finding = LAND_ON_FINDING
        self._pd()
        self.set_inputs(static.default_inputs)

    # ---------- belief ----------

    def set_params(self, sensors: dict[FaultType, SensorParams], drone: DroneParams) -> None:
        self.sensors, self.drone = sensors, drone
        self._pd()

    def _pd(self) -> None:
        def arr(key):
            return np.array([getattr(self.sensors[t], key) if t in self.sensors else 0.0 for t in ALL_TYPES])
        self.pd_air, self.pd_land, self.pfp = arr("p_detect_air"), arr("p_detect_landed"), arr("p_false_positive")

    def set_inputs(self, inputs: OperatorInputs) -> None:
        self.inputs = inputs
        self.dead = set(inputs.known_dead_segments)
        near = set().union(*(self.neighbours.get(s, set()) for s in self.dead)) if self.dead else set()
        self.report_cells = report_cells(self.cell_geoms, inputs.reports)
        # a protection event (trip / reclose) in a sector: some line there may be de-energised
        tripped = {self.cell_seg_of[c] for r in inputs.reports if "protection" in r.text.lower()
                   for c in self.report_cells[r.report_id]}
        self.p_live = {s: 0.0 if s in self.dead else P_LIVE_NEAR_DEAD if s in near | tripped else P_LIVE_PRIOR
                       for s in self.segs}
        self.certain_live: set[str] = set(self.dead)
        pr = prior(self.net.cell_ids, self.cell_seg_of, self.report_cells, inputs.reports, self.veg,
                   self.in_storm, self.dead, self.neighbours)
        self.P = np.array([[pr[cid][t] for t in ALL_TYPES] for cid in self.net.cell_ids])
        self.inspected = ["none"] * self.n
        self.detected: list[set[int]] = [set() for _ in range(self.n)]
        # confirmed = two detections of the type, or one confident landed detection (crew only after a confirmed
        # finding). Unconfirmed detections keep their value, so a second look is scheduled.
        self.n_det = np.zeros((self.n, len(ALL_TYPES)), int)
        self.confirmed = np.zeros((self.n, len(ALL_TYPES)), bool)
        self.claim_cells: dict[str, set[int]] = {}
        self.claim_land: dict[str, int] = {}
        self.home: dict[str, int] = {}  # drone_id -> cell of its last / planned landing (where it charges)
        self.first_plans: dict = {}  # drone_id -> FlightPlan from start_positions
        self.parked: dict[str, DroneState] = {}  # charging drones, as of their last next_route call (<= 60 s old)
        self._cache = None

    def on_event(self, event: Event) -> None:
        if event.type in ("landing", "stranded") and event.drone_id:
            self.claim_cells.pop(event.drone_id, None)
            self.claim_land.pop(event.drone_id, None)
            if event.type == "stranded":
                self.home.pop(event.drone_id, None)
                self.parked.pop(event.drone_id, None)
        if event.type == "line_dead" and event.segment_id and event.segment_id not in self.dead:
            self._mark_dead(event.segment_id)
        if event.type == "recommendation" and event.message.startswith("Area of "):
            self._explain_away(event.message.split()[2])

    def _explain_away(self, rid: str) -> None:
        """A report area was searched without finding anything: the report is most likely a false alarm.
        Cell-wise Bayes treats the cells independently, which keeps ~1/(1-pd) of the report's evidence in
        every cell; instead drop that report's contribution (inspected cells: prior without the report,
        after one air pass). Stops drones re-flying a cleared area just because its prior was high."""
        reports = [r for r in self.inputs.reports if r.report_id != rid]
        if len(reports) == len(self.inputs.reports):
            return
        pr = prior(self.net.cell_ids, self.cell_seg_of, self.report_cells, reports, self.veg, self.in_storm,
                   set(self.inputs.known_dead_segments), self.neighbours)
        for cid in self.report_cells.get(rid, []):
            i = self.net.cell_idx[cid]
            if self.inspected[i] != "none":
                p0 = np.array([pr[cid][t] for t in ALL_TYPES]) * (1 - self.pd_air)
                self.P[i] = np.minimum(self.P[i], p0)
        self._cache = None

    def _mark_dead(self, sid: str) -> None:
        """Dead line: no charging there for anyone; its cells and (modestly) its neighbours get riskier."""
        self.dead.add(sid)
        self.p_live[sid] = 0.0
        self.certain_live.add(sid)
        bc = ALL_TYPES.index("broken_conductor")
        idx = self.net.seg_cells.get(sid, [])
        bump = max(DEAD_LINE_MIN, DEAD_LINE_P / max(1, len(idx)))
        self.P[idx, bc] = 1 - (1 - self.P[idx, bc]) * (1 - bump)
        for nb in self.neighbours.get(sid, set()) - self.dead:
            nidx = self.net.seg_cells.get(nb, [])
            self.P[nidx, bc] = 1 - (1 - self.P[nidx, bc]) * (1 - NEIGHBOUR_P)
            if nb not in self.certain_live:
                self.p_live[nb] = min(self.p_live[nb], P_LIVE_NEAR_DEAD)
        self._cache = None

    def on_observation(self, obs: Observation) -> None:
        i = self.net.cell_idx.get(obs.cell_id)
        if i is None:
            return
        if INSPECTED_RANK[obs.mode] > INSPECTED_RANK[self.inspected[i]]:
            self.inspected[i] = obs.mode
        pd = self.pd_air if obs.mode == "air" else self.pd_land
        det = {d.fault_type: d.confidence for d in obs.detections}
        for k, t in enumerate(ALL_TYPES):
            if t in det:
                self.P[i, k] = update_finding(self.P[i, k], pd[k], self.pfp[k], det[t])
                self.detected[i].add(k)
                self.n_det[i, k] += 1
                if self.n_det[i, k] >= 2 or (obs.mode == "landed" and det[t] >= CONFIRM_CONF):
                    self.confirmed[i, k] = True
            elif pd[k] > 0:
                self.P[i, k] = update_no_finding(self.P[i, k], pd[k])
        sid = obs.segment_id
        if obs.reads_live is not None and sid not in self.certain_live:
            if obs.live_accuracy >= 1:
                self.certain_live.add(sid)
                if obs.reads_live:
                    self.p_live[sid] = 1.0
                else:
                    self._mark_dead(sid)
            else:
                self.p_live[sid] = update_live(self.p_live[sid], obs.reads_live, obs.live_accuracy)
        self._cache = None

    # ---------- values (expected detections) ----------

    def _values(self):
        P = self.P
        open_ = (P < CREW) | ((self.n_det > 0) & ~self.confirmed)
        air = (P * self.pd_air * open_).sum(axis=1)
        land = (P * self.pd_land * open_).sum(axis=1)
        after = P * (1 - self.pd_air) / (1 - P * self.pd_air)  # belief after an air pass without finding
        land_after = (after * self.pd_land * open_).sum(axis=1)
        p_live_cell = np.array([self.p_live[s] for s in self.segs])[self.cell_seg_idx]
        landable = (p_live_cell >= LANDABLE_P_LIVE) & np.array([s not in self.dead for s in self.net.cell_seg])
        return air, land, land_after, p_live_cell, landable

    def _landable(self, sid: str | None) -> bool:
        return sid in self.p_live and sid not in self.dead and self.p_live[sid] >= LANDABLE_P_LIVE

    # ---------- start positions ----------

    def start_positions(self, n: int = 5) -> list[StartPosition]:
        p = self.drone
        air, land, land_after, p_live_cell, landable = self._values()
        budget = (p.max_flight_min - p.landing_min - RESERVE_MIN) * 60
        ok = landable & (p_live_cell >= start_positions.P_LIVE_START)
        chosen = start_positions.choose(self.net, n, budget, air, land, land_after, p_live_cell, ok,
                                        p.speed_inspect_mps, p.speed_transit_mps)
        risk = 1 - np.prod(1 - self.P, axis=1)
        big = len(self.net.cell_ids) / 2  # reports covering half the grid say nothing about where to go
        out = []
        for k, (c, plan) in enumerate(chosen):
            cells = plan.cells if plan else set()
            ids = {self.net.cell_ids[i] for i in cells}
            reps = sorted(((len(ids & set(cs)), rid) for rid, cs in self.report_cells.items()
                           if len(cs) < big and ids & set(cs)), reverse=True)
            forest_km = sum(self.veg[self.net.cell_ids[i]] * self.net.cell_len[i] for i in cells) / 1000
            parts = [f"covers {', '.join(r.replace('SYN_REPORT_', 'report ') for _, r in reps[:2])} area"] if reps else []
            if forest_km >= 0.1:
                parts.append(f"{forest_km:.1f} km forest")
            parts.append(f"{len(cells) * 0.5:.0f} km line in first flight")
            reason = " + ".join(parts) + f", exp. risk {risk[list(cells)].sum():.2f}" if cells else "fallback"
            sid = self.net.cell_seg[c]
            out.append(StartPosition(drone_id=f"D{k + 1}", segment_id=sid,
                                     position=list(to_lonlat(*self.net.cell_mid[c])), reason=reason))
            if plan:  # fly exactly the coordinated first flight the positions were chosen for
                self.first_plans[f"D{k + 1}"] = plan
        return out

    # ---------- routes ----------

    def next_route(self, drone: DroneState, speed_mps: float = 10, landing_min: float = 3) -> list[Waypoint] | None:
        p = self.drone
        first = self.first_plans.pop(drone.drone_id, None)
        if first and drone.position == first.route[0].position and drone.battery_min >= p.max_flight_min - 0.1:
            return self._take(drone, first)
        if drone.status == "charging":
            self.parked[drone.drone_id] = drone
        air, land, land_after, p_live_cell, landable = self._values()
        hop_ok = landable.copy()  # emergency hop off a dead line: any free landable cell
        hop_ok[[c for d, c in self.claim_land.items() if d != drone.drone_id]] = False
        # one drone per landing line (spreads the risk of landing on a line that turns out dead)
        for d, c in list(self.claim_land.items()) + list(self.home.items()):
            if d != drone.drone_id:
                landable[self.net.seg_cells[self.net.cell_seg[c]]] = False
        if self.land_on_finding:
            # the reserve only helps if a hop is possible: land on a line not known live only with another free
            # landable line within hop range ((reserve - landing) min of transit). Part of the flag: early
            # confirmation landings move the drones' homes, and without this D1 got stranded on the hidden dead
            # line in 7 of 10 default-scenario seeds.
            reach = (RESERVE_MIN - p.landing_min) * 60 * p.speed_transit_mps
            other = self.cell_seg_idx[:, None] != self.cell_seg_idx[None, :]
            landable &= (p_live_cell >= 1) | ((self.net.cell_dist <= reach) & hop_ok[None, :] & other).any(axis=1)
        if drone.status != "charging" and not self._landable(drone.segment_id):
            # landed on a dead line: hop to a live one using the reserve
            plan = missions.hop(self.net, drone.position, (drone.battery_min - p.landing_min) * 60 - 5, hop_ok,
                                land, p_live_cell, p.speed_transit_mps)
            return self._take(drone, plan)
        if drone.status == "charging" and self.land_on_finding:
            # parked near a finding that needs confirmation: hop over as soon as the battery allows
            for i in self._pending_findings(drone):
                plan = self._confirm_hop(drone, i)
                if plan:
                    return self._take(drone, plan)
        if drone.battery_min < p.landing_min + RESERVE_MIN + 2:
            return None
        budget = (drone.battery_min - p.landing_min - RESERVE_MIN) * 60
        for d, cells in self.claim_cells.items():  # don't fly what another drone is flying right now
            if d != drone.drone_id:
                air[list(cells)] = 0
        here = to_xy(*drone.position)
        d_cells = self.net.dist_to_cells(here)
        full = drone.battery_min >= min(TAKEOFF_MIN, p.max_flight_min)
        # charged: a normal inspection flight, forced to land on a target only for a strong confirmation.
        # still charging: a short hop to a landing target (confirmation / landed-only check) uses the wait.
        target = self._target(land_after, landable, d_cells, budget, CONFIRM_RATE if full else TARGET_RATE)
        if not full and (target is None or drone.battery_min < CONFIRM_TAKEOFF_MIN):
            return None
        plan = missions.plan_flight(self.net, drone.position, drone.segment_id, budget, air, land, land_after,
                                    landable, p_live_cell, p.speed_inspect_mps, p.speed_transit_mps, dest=target)
        if not plan or plan.value < MIN_FLIGHT_VALUE:
            return None
        return self._take(drone, plan)

    def _target(self, land: np.ndarray, landable: np.ndarray, d_cells: np.ndarray, budget: float,
                min_rate: float) -> int | None:
        """Best landing for confirmation / landed-only sensors, if one is worth it and reachable.
        `land` = what a landing would still find after an air pass, i.e. what only landed sensors can add."""
        cost_min = d_cells / self.drone.speed_transit_mps / 60 + self.drone.landing_min
        rate = land / cost_min  # expected detections per battery minute spent on this landing
        ok = landable & (rate >= min_rate) & (d_cells / self.drone.speed_transit_mps <= budget) & (d_cells > 100)
        if not ok.any():
            return None
        return int(np.where(ok, rate, -1).argmax())

    # ---------- land on a finding ----------

    def confirm_route(self, drone: DroneState, cell_id: str) -> list[Waypoint] | None:
        """Called while `drone` flies, right after an air detection on `cell_id`. If the finding needs
        confirmation and landed sensors can confirm it, return a new route that lands on that cell now
        (or hand it to a drone parked nearby and keep flying). None = keep the current plan.
        Only the cell under a landed drone is sensed, so the landing must be on the finding's own cell."""
        i = self.net.cell_idx.get(cell_id)
        if not self.land_on_finding or i is None or drone.status != "flying" or not self._needs_landing(i):
            return None
        if self.claim_land.get(drone.drone_id) == i:
            return None  # already landing there
        # a drone parked close by is the cheaper confirmer: it hops over on its next call (<= 60 s), this one
        # keeps its flight plan
        if any(i in self._pending_findings(d) and self._confirm_hop(d, i) for d in self.parked.values()
               if d.drone_id != drone.drone_id):
            return None
        return self._take(drone, self._confirm_hop(drone, i))

    def _needs_landing(self, i: int) -> bool:
        """An unconfirmed detection that makes the cell "needs confirmation", and a landing would likely confirm it."""
        risk = 1 - np.prod(1 - self.P[i])
        todo = [k for k in self.detected[i] if not self.confirmed[i, k]]
        return risk >= CONFIRM and any(self.P[i, k] * self.pd_land[k] >= MIN_CONFIRM_VALUE for k in todo)

    def _pending_findings(self, drone: DroneState) -> list[int]:
        """Cells with an unconfirmed detection within hop range of a parked drone, nearest first (not the cell it
        sits on: its landing already sensed that one)."""
        d = self.net.dist_to_cells(to_xy(*drone.position))
        cells = np.where(((self.n_det > 0) & ~self.confirmed).any(axis=1) & (d <= HANDOFF_RADIUS_M) & (d > 100))[0]
        return [int(i) for i in cells[np.argsort(d[cells])]
                if not any(c == i for o, c in self.claim_land.items() if o != drone.drone_id)]

    def _line_taken(self, i: int, me: str) -> bool:
        sid = self.net.cell_seg[i]
        return any(self.net.cell_seg[c] == sid for d, c in list(self.claim_land.items()) + list(self.home.items())
                   if d != me)

    def _confirm_hop(self, drone: DroneState, i: int):
        """Straight transit + landing on cell i (missions.hop), keeping landing time and the 5 min reserve.
        None if the finding no longer needs it, the line is dead / doubtful or another drone lands or charges on
        it (one drone per landing line), or the battery is short."""
        if not self._needs_landing(i) or not self._landable(self.net.cell_seg[i]) or self._line_taken(i, drone.drone_id):
            return None
        _, land, _, p_live_cell, _ = self._values()
        only = np.zeros(self.n, bool)
        only[i] = True
        budget = (drone.battery_min - self.drone.landing_min - RESERVE_MIN) * 60
        return missions.hop(self.net, drone.position, budget, only, land, p_live_cell, self.drone.speed_transit_mps)

    def _take(self, drone: DroneState, plan) -> list[Waypoint] | None:
        if not plan:
            return None
        self.parked.pop(drone.drone_id, None)
        self.claim_cells[drone.drone_id] = plan.cells
        self.claim_land[drone.drone_id] = plan.land_cell
        self.home[drone.drone_id] = plan.land_cell
        return plan.route

    # ---------- output ----------

    def cell_states(self) -> list[CellState]:
        if self._cache is None:
            risk = 1 - np.prod(1 - self.P, axis=1)
            out = []
            for i, cid in enumerate(self.net.cell_ids):
                top = max(self.detected[i], key=lambda k: self.P[i, k], default=None)
                if top is not None and self.P[i, top] < CLEAR_TOP:
                    top = None
                out.append(CellState(cell_id=cid, risk=round(float(min(1.0, max(0.0, risk[i]))), 3),
                                     inspected=self.inspected[i], top_fault=ALL_TYPES[top] if top is not None else None))
            self._cache = out
        return self._cache

    def segment_states(self) -> list[SegmentState]:
        return [SegmentState(segment_id=s, p_live=round(self.p_live[s], 3)) for s in self.segs]
