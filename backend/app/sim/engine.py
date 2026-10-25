"""On-demand simulation.

No background thread: every `advance()` (called on GET /api/state) steps the sim forward by
`wall time since last call × speed` in fixed 1 s sim steps. Seeded RNG => same config, same run.
The sim knows the ground truth (SimConfig); the planner only sees Events and Observations.
"""

import random
import time
from typing import Callable

from shapely.geometry import Point, shape

from app.geo import dist_m, heading_deg, lerp, project, to_xy
from app.models import (CellObservation, DroneState, Event, Fault, FaultFound, OperatorInputs, Progress, SimConfig, StartPosition, State,
                        Static, Waypoint)
from app.planner import Planner
from app.planner.recommend import CONFIRM_CONF, observations_for, recommendations
from app.sim.sensors import inspect

DT = 1.0  # sim seconds per step
MAX_STEPS_PER_CALL = 6 * 3600  # don't hang if the tab was asleep for a long time
N_DRONES = 5
LOG_MIN_RISK = 0.4  # unconfirmed detections are logged only if they push the cell to "needs confirmation"
ASK_PLANNER_EVERY_S = 60  # charging drones ask the planner for a route this often


class Drone:
    def __init__(self, sp: StartPosition, battery_min: float, max_flight_min: float = 30):
        self.id = sp.drone_id
        self.max_flight_min = max_flight_min
        self.position = list(sp.position)
        self.segment_id: str | None = sp.segment_id
        self.heading = 0.0
        self.status = "charging"
        self.battery_min = battery_min
        self.route: list[Waypoint] = []
        self.leg = 0  # index of the waypoint we are flying towards
        self.landing_left_s = 0.0
        self.last_cell: str | None = None  # cell under the drone in the previous step (air pass = entering a new cell)
        self.mag_read: set[str] = set()  # segments the magnetometer already read on this flight

    def state(self) -> DroneState:
        remaining = [self.position] + [w.position for w in self.route[self.leg:]] if self.route else []
        return DroneState(drone_id=self.id, position=self.position, heading=round(self.heading, 1),
                          status=self.status, battery_min=round(self.battery_min, 2),
                          max_flight_min=self.max_flight_min,
                          segment_id=self.segment_id, route=remaining)


class Engine:
    def __init__(self, static: Static, config: SimConfig, planner_factory: Callable[[Static], Planner]):
        self.static = static
        self.config = config
        self.planner_factory = planner_factory
        # no reports / dead lines until the operator loads or uploads them ("Load defaults" = static.default_inputs)
        self.inputs = OperatorInputs()
        self.speed = 60.0
        self.start_positions: list[StartPosition] = []
        # network geometry for cell lookup (projected metres); cells are equal pieces of each segment
        self.seg_m = {f["properties"]["segment_id"]: project(shape(f["geometry"])) for f in static.segments["features"]}
        self.n_cells: dict[str, int] = {}
        self.cell_mid: dict[str, list[float]] = {}
        for c in static.cells["features"]:
            pr = c["properties"]
            self.n_cells[pr["segment_id"]] = self.n_cells.get(pr["segment_id"], 0) + 1
            mid = shape(c["geometry"]).interpolate(0.5, normalized=True)
            self.cell_mid[pr["cell_id"]] = [mid.x, mid.y]
        self.cell_km = {}
        for cid in self.cell_mid:
            sid, i = cid.split("#")
            self.cell_km[cid] = round((int(i) + 0.5) * self.seg_m[sid].length / self.n_cells[sid] / 1000, 1)
        self.reset()

    # ---------- control ----------

    def reset(self) -> None:
        self.t = 0.0
        self.status = "setup"
        self.events: list[Event] = []
        self.rng = random.Random(self.config.seed)
        self.planner = self.planner_factory(self.static)
        self.planner.set_params(self.config.sensors, self.config.drone)
        self.planner.set_inputs(self.inputs)
        self.done_recs: set[str] = set()
        self.obs_log: list[CellObservation] = []  # every pass with risk before/after (report panel)
        self.n_det: dict[tuple[str, str], int] = {}  # (cell, fault type) -> detections so far
        self.confirmed: set[tuple[str, str]] = set()  # 2 detections, or 1 confident landed detection
        self.cell_faults: dict[str, list[Fault]] = {}
        for f in self.config.faults:
            self.cell_faults.setdefault(self._cell_of(f.segment_id, f.position), []).append(f)
        self.found: dict[str, FaultFound] = {}  # fault_id -> first finding (benchmark, see _track_found)
        self.all_found_t: float | None = None
        self.all_confirmed_t: float | None = None
        self._report_cells()
        self._place_drones()
        self._last_wall: float | None = None
        self._carry = 0.0

    def set_inputs(self, inputs: OperatorInputs) -> None:
        self.inputs = inputs
        self.planner.set_inputs(inputs)
        self._report_cells()
        if self.status != "running":  # start positions were planned for the old inputs
            self.start_positions = []
            if self.status == "setup":
                self._place_drones()

    def set_config(self, config: SimConfig) -> None:
        self.config = config
        self.reset()

    def plan_start_positions(self) -> list[StartPosition]:
        self.start_positions = self.planner.start_positions(N_DRONES)
        if self.status == "setup":
            self._place_drones()
        return self.start_positions

    def control(self, action: str, speed: float | None = None) -> None:
        if speed is not None:
            self.advance()  # bank time at the old speed first
            self.speed = speed
        if action == "start" and self.status == "setup":
            if not self.start_positions:
                self.plan_start_positions()
            self.status = "running"
            self._last_wall = time.monotonic()
        elif action == "pause" and self.status == "running":
            self.advance()
            self.status = "paused"
        elif action == "resume" and self.status == "paused":
            self.status = "running"
            self._last_wall = time.monotonic()
        elif action == "reset":  # back to a fresh start: operator inputs are cleared too
            self.inputs = OperatorInputs()
            self.start_positions = []
            self.reset()

    # ---------- stepping ----------

    def advance(self) -> None:
        """Advance by elapsed wall time × speed (only while running)."""
        if self.status != "running":
            return
        now = time.monotonic()
        self._carry += (now - (self._last_wall or now)) * self.speed
        self._last_wall = now
        n = min(int(self._carry / DT), MAX_STEPS_PER_CALL)
        self._carry -= n * DT
        self.step_n(n)
        if self.status == "paused":  # auto-paused (all faults found): drop the rest of this poll's time
            self._carry = 0.0

    def step_n(self, n: int) -> None:
        """Step n sim seconds; stops early if the sim pauses itself (all faults found)."""
        for _ in range(n):
            if self.status == "paused":
                break
            self._step()

    def _step(self) -> None:
        self.t += DT
        for d in self.drones:
            if d.status == "flying":
                self._fly(d)
            elif d.status == "landing":
                d.battery_min = max(0.0, d.battery_min - DT / 60)
                d.landing_left_s -= DT
                if d.landing_left_s <= 0:
                    self._landed(d)
            elif d.status == "charging":
                p = self.config.drone
                if not self._is_dead(d.segment_id):  # linear charging, partial charges allowed
                    d.battery_min = min(p.max_flight_min, d.battery_min + DT / 60 * p.max_flight_min / p.full_charge_min)
                if (self.t - DT) % ASK_PLANNER_EVERY_S == 0:  # planner decides whether it is worth flying now
                    self._try_takeoff(d)

    def _try_takeoff(self, d: Drone) -> None:
        p = self.config.drone
        route = self.planner.next_route(d.state(), p.speed_inspect_mps, p.landing_min)
        if not route:
            return
        d.route, d.leg, d.status = route, 1, "flying"
        d.mag_read = set()
        self._emit("takeoff", d, f"{d.id} takes off from {d.segment_id}")

    def _divert(self, d: Drone, dead_seg: str) -> None:
        p = self.config.drone
        state = d.state().model_copy(update={"segment_id": dead_seg})  # planner: "my landing line is dead, hop"
        route = self.planner.next_route(state, p.speed_inspect_mps, p.landing_min)
        if not route:
            return
        d.route, d.leg = route, 1
        dest = route[-1].segment_id
        self._log(Event(t=self.t, type="line_dead", drone_id=d.id, segment_id=dead_seg, position=list(d.position),
                        message=f"{d.id} diverts to {dest}: its landing line {dead_seg} is dead"))

    def _fly(self, d: Drone) -> None:
        p = self.config.drone
        d.battery_min = max(0.0, d.battery_min - DT / 60)
        budget = DT
        while budget > 0 and d.leg < len(d.route):
            wp = d.route[d.leg]
            speed = p.speed_inspect_mps if wp.segment_id else p.speed_transit_mps
            dist = dist_m(d.position, wp.position)
            if dist > 1e-6:
                d.heading = heading_deg(d.position, wp.position)
            if dist <= speed * budget:
                budget -= dist / speed
                d.position = list(wp.position)
                d.segment_id = wp.segment_id
                d.leg += 1
            else:
                d.position = lerp(d.position, wp.position, speed * budget / dist)
                d.segment_id = wp.segment_id
                budget = 0
        # air sensors: inspect every cell we enter while flying along a line (not during transit)
        cell = self._cell_of(d.segment_id, d.position) if d.segment_id else None
        if cell and cell != d.last_cell:
            self._inspect(d, cell, "air")
        d.last_cell = cell
        if d.leg >= len(d.route):
            d.status, d.landing_left_s = "landing", p.landing_min * 60
            d.route, d.leg = [], 0
        elif d.battery_min <= 0:
            d.status = "stranded"
            self._emit("stranded", d, f"{d.id} ran out of battery")

    def _landed(self, d: Drone) -> None:
        live = not self._is_dead(d.segment_id)
        self._emit("landing", d, f"{d.id} landed on {d.segment_id}: " + ("line live, charging" if live else "no current"))
        cell = self._cell_of(d.segment_id, d.position) if d.segment_id else None
        if cell:
            self._inspect(d, cell, "landed")  # landed-only sensors + current sensor (true live/dead)
        d.last_cell = cell
        if live:
            d.status = "charging"
            return
        self._emit("line_dead", d, f"{d.id}: no current on {d.segment_id} - line is dead, cannot charge")
        for o in self.drones:  # drones on their way to land on this line divert now
            if o is not d and o.status == "flying" and o.route and o.route[-1].segment_id == d.segment_id:
                self._divert(o, d.segment_id)
        if d.battery_min > self.config.drone.landing_min:
            self._try_takeoff(d)  # hop on with the reserve (status is still "landing" here)
        if d.status == "landing":
            d.status = "stranded"
            self._emit("stranded", d, f"{d.id} is stranded on dead line {d.segment_id}")

    # ---------- sensing ----------

    def _inspect(self, d: Drone, cell: str, mode: str) -> None:
        """Sense one cell, tell the planner (also when nothing is found), log findings + recommendations."""
        faults = self.cell_faults.get(cell, [])
        read_live = mode == "air" and d.segment_id not in d.mag_read
        d.mag_read.add(d.segment_id)
        obs = inspect(self.rng, t=self.t, drone_id=d.id, cell_id=cell, segment_id=d.segment_id, mode=mode,
                      faults=faults, sensors=self.config.sensors, live=not self._is_dead(d.segment_id),
                      read_live=read_live)
        before = self._risk(cell)
        self.planner.on_observation(obs)
        risk = self._risk(cell)
        self.obs_log.append(CellObservation(t=obs.t, drone_id=d.id, cell_id=cell, mode=mode, detections=obs.detections,
                                            risk_before=before, risk_after=risk))
        for det in obs.detections:
            true = next((f for f in faults if f.fault_type == det.fault_type), None)
            sp = self.config.sensors[det.fault_type]
            how = "from the air" if mode == "air" else "landed"
            key = (cell, det.fault_type)
            self.n_det[key] = self.n_det.get(key, 0) + 1
            where = f"{d.segment_id} km {self.cell_km[cell]}"
            if key not in self.confirmed and (self.n_det[key] >= 2 or (mode == "landed" and det.confidence >= CONFIRM_CONF)):
                self.confirmed.add(key)
                what = f"{det.fault_type} confirmed on {where}"
            elif key in self.confirmed:
                what = f"{det.fault_type} seen again on {where}"
            elif risk < LOG_MIN_RISK:
                continue  # weak single detection on a low-prior cell: the belief absorbs it, the log stays readable
            else:
                what = f"possible {det.fault_type} on {where}"
            self._emit("finding", d, f"{d.id}: {what} ({sp.sensor or 'sensor'}, {how}, confidence {det.confidence:.0%})",
                       cell_id=cell, fault_type=det.fault_type, confidence=det.confidence,
                       position=list(true.position) if true else list(d.position))
            self._track_found(d, cell, det.fault_type, faults)
        self._recommend()
        if mode == "air" and d.status == "flying" and obs.detections:
            self._confirm_reroute(d, cell, [det.fault_type for det in obs.detections])

    def _confirm_reroute(self, d: Drone, cell: str, types: list[str]) -> None:
        """Ask the planner whether this drone should break off its flight and land on the finding to confirm it."""
        route = self.planner.confirm_route(d.state(), cell)
        if not route:
            return
        d.route, d.leg = route, 1
        what = " / ".join(t for t in types if (cell, t) not in self.confirmed) or " / ".join(types)
        where = f"{route[-1].segment_id} km {self.cell_km[cell]}"
        self._emit("reroute", d, f"{d.id} re-routes to land at {where} to confirm possible {what}", cell_id=cell)

    def _track_found(self, d: Drone, cell: str, fault_type: str, faults: list[Fault]) -> None:
        """Benchmark: a true fault counts as found at its first logged finding of its type on its cell (the
        same rule as test_demo / compare_planners). Pauses the sim once, when the last fault is found."""
        for f in faults:
            if f.fault_type != fault_type:
                continue
            if f.fault_id not in self.found:
                self.found[f.fault_id] = FaultFound(fault_id=f.fault_id, fault_type=f.fault_type,
                                                    segment_id=f.segment_id, cell_id=cell, km=self.cell_km[cell],
                                                    t_found=self.t, drone_id=d.id)
            if (cell, fault_type) in self.confirmed and self.found[f.fault_id].t_confirmed is None:
                self.found[f.fault_id].t_confirmed = self.t
        n = len(self.config.faults)
        if len(self.found) < n:
            return
        if self.all_found_t is None:
            self.all_found_t = self.t
            self.status = "paused"  # benchmark stop; play continues and it does not fire again until reset
            self._log(Event(t=self.t, type="all_found", message=f"All {n} faults found in {clock(self.t)} sim time"
                            " - simulation paused, press play to continue"))
        if self.all_confirmed_t is None and all(f.t_confirmed is not None for f in self.found.values()):
            self.all_confirmed_t = self.t
            self._log(Event(t=self.t, type="all_found", message=f"All {n} faults confirmed in {clock(self.t)} sim time"))

    def _recommend(self) -> None:
        for _, cell, seg, msg in recommendations(self.planner.cell_states(), self.planner.segment_states(),
                                                 self.cell_km, self.report_cells, self.done_recs, self.confirmed):
            if cell:
                pos = self.cell_mid[cell]
            elif seg:
                pos = list(shape(next(f["geometry"] for f in self.static.segments["features"]
                                      if f["properties"]["segment_id"] == seg)).interpolate(0.5, normalized=True).coords[0])
            else:
                pos = None
            self._log(Event(t=self.t, type="recommendation", cell_id=cell, segment_id=seg, position=pos, message=msg))

    def _risk(self, cell: str) -> float:
        return next((c.risk for c in self.planner.cell_states() if c.cell_id == cell), 0.0)

    def _cell_of(self, segment_id: str, position: list[float]) -> str:
        line, n = self.seg_m[segment_id], self.n_cells.get(segment_id, 1)
        i = min(n - 1, int(line.project(Point(to_xy(*position))) / line.length * n))
        return f"{segment_id}#{i:02d}"

    def _report_cells(self) -> None:
        self.report_cells = {}
        for r in self.inputs.reports:
            poly = shape(r.polygon)
            self.report_cells[r.report_id] = [cid for cid, m in self.cell_mid.items() if poly.contains(Point(m))]

    # ---------- helpers ----------

    def _is_dead(self, segment_id: str | None) -> bool:
        return segment_id in self.config.dead_segments or segment_id in self.inputs.known_dead_segments

    def _place_drones(self) -> None:
        self.drones = [Drone(sp, self.config.drone.start_battery_min, self.config.drone.max_flight_min)
                       for sp in self.start_positions]

    def _emit(self, type_: str, d: Drone, message: str, **kw) -> None:
        kw.setdefault("position", list(d.position))
        self._log(Event(t=self.t, type=type_, drone_id=d.id, segment_id=d.segment_id, message=message, **kw))

    def _log(self, ev: Event) -> None:
        self.events.append(ev)
        self.planner.on_event(ev)

    def state(self) -> State:
        return State(t=self.t, speed=self.speed, status=self.status, drones=[d.state() for d in self.drones],
                     cells=self.planner.cell_states(), segments=self.planner.segment_states(),
                     inputs=self.inputs, start_positions=self.start_positions, events=self.events,
                     progress=Progress(total=len(self.config.faults),
                                       found=sorted(self.found.values(), key=lambda f: f.t_found),
                                       all_found_t=self.all_found_t, all_confirmed_t=self.all_confirmed_t),
                     observations=observations_for(self.obs_log, self.done_recs, self.report_cells))


def clock(t: float) -> str:
    s = int(t)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
