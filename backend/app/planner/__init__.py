"""Planner interface + a deliberately dumb placeholder strategy (M1).

The planner never imports app.sim. It only sees OperatorInputs, Events and Observations, plus the
published sensor/drone specs (set_params).
BE1 replaces DumbPlanner with the real strategy (risk prior, greedy start positions, risk-driven routes).
"""

from shapely.geometry import Point, shape
from shapely.ops import substring

from app.geo import project, to_lonlat, to_xy
from app.models import (CellState, DroneParams, DroneState, Event, FaultType, Observation, OperatorInputs,
                        SegmentState, SensorParams, StartPosition, Static, Waypoint)
from app.planner.risk import update_finding, update_live, update_no_finding

P_LIVE_PRIOR = 0.9
RESERVE_MIN = 5
MIN_TAKEOFF_BATTERY_MIN = 20  # charge until min(battery the next route needs, this) before flying again
DEAD_LINE_RISK = 0.2  # a dead line means a fault somewhere on it (or upstream)
ON_LINE_M = 50
ALL_TYPES: list[FaultType] = ["vegetation", "sag", "ice", "galloping", "hotspot", "broken_conductor"]
INSPECTED_RANK = {"none": 0, "air": 1, "landed": 2}


class Planner:
    """Interface. The sim/engine only calls these methods."""

    def __init__(self, static: Static): ...
    def set_inputs(self, inputs: OperatorInputs) -> None: ...
    def set_params(self, sensors: dict[FaultType, SensorParams], drone: DroneParams) -> None: ...
    def start_positions(self, n: int = 5) -> list[StartPosition]: ...
    def on_event(self, event: Event) -> None: ...
    def on_observation(self, obs: Observation) -> None: ...  # every inspected cell, incl. "nothing found"
    def next_route(self, drone: DroneState, speed_mps: float = 10, landing_min: float = 3) -> list[Waypoint] | None: ...
    # mid-flight, after an air finding on cell_id: a new route (land there to confirm it) or None = keep the plan
    def confirm_route(self, drone: DroneState, cell_id: str) -> list[Waypoint] | None: ...
    def cell_states(self) -> list[CellState]: ...
    def segment_states(self) -> list[SegmentState]: ...


class DumbPlanner(Planner):
    """Start on the midpoints of the 5 longest non-dead lines; always fly to the nearest unchecked line."""

    def __init__(self, static: Static):
        self.static = static
        self.segs = {f["properties"]["segment_id"]: f for f in static.segments["features"]}
        self.seg_m = {sid: project(shape(f["geometry"])) for sid, f in self.segs.items()}
        self.cells = static.cells["features"]
        self.cells_by_seg: dict[str, list[str]] = {}
        for c in self.cells:
            self.cells_by_seg.setdefault(c["properties"]["segment_id"], []).append(c["properties"]["cell_id"])
        self.sensors: dict[str, SensorParams] = {}
        self.drone = DroneParams()
        self.claimed: dict[str, set[str]] = {}  # drone_id -> segments of its current route
        self.set_inputs(static.default_inputs)

    # ---------- belief ----------

    def set_inputs(self, inputs: OperatorInputs) -> None:
        self.inputs = inputs
        self.dead = set(inputs.known_dead_segments)
        self.p_live = {s: 0.0 if s in self.dead else P_LIVE_PRIOR for s in self.segs}
        self.certain_live: set[str] = set(self.dead)  # p_live known exactly (landed or operator)
        polys = [(shape(r.polygon), r.confidence, r.fault_types) for r in inputs.reports]
        self.risk, self.types, self.inspected, self.top, self.air_passes = {}, {}, {}, {}, {}
        for c in self.cells:  # placeholder prior: 1 - prod(1 - 0.5 * confidence) over reports covering the cell
            cid, sid = c["properties"]["cell_id"], c["properties"]["segment_id"]
            mid = shape(c["geometry"]).interpolate(0.5, normalized=True)
            p_ok, types = 1.0, set()
            for poly, conf, ftypes in polys:
                if poly.contains(Point(mid)):
                    p_ok *= 1 - 0.5 * conf
                    types |= set(ftypes)
            if sid in self.dead:
                p_ok *= 1 - DEAD_LINE_RISK
            self.risk[cid] = 1 - p_ok
            self.types[cid] = sorted(types) or ALL_TYPES
            self.inspected[cid] = "none"
            self.top[cid] = (None, 0.0)  # (fault_type, confidence)
            self.air_passes[cid] = 0

    def set_params(self, sensors: dict[FaultType, SensorParams], drone: DroneParams) -> None:
        self.sensors, self.drone = sensors, drone

    def on_event(self, event: Event) -> None:
        if event.type == "landing" and event.drone_id:
            self.claimed.pop(event.drone_id, None)  # its route is done; others may take the rest
        if event.type == "line_dead" and event.segment_id and event.segment_id not in self.dead:
            self._mark_dead(event.segment_id)

    def _mark_dead(self, sid: str) -> None:
        self.dead.add(sid)
        self.p_live[sid] = 0.0
        self.certain_live.add(sid)
        for cid in self.cells_by_seg.get(sid, []):
            self.risk[cid] = 1 - (1 - self.risk[cid]) * (1 - DEAD_LINE_RISK)

    def on_observation(self, obs: Observation) -> None:
        cid, mode = obs.cell_id, obs.mode
        if cid not in self.risk:
            return
        if INSPECTED_RANK[mode] > INSPECTED_RANK[self.inspected[cid]]:
            self.inspected[cid] = mode
        if mode == "air":
            self.air_passes[cid] += 1
        key = "p_detect_air" if mode == "air" else "p_detect_landed"
        if not obs.detections:
            pds = [getattr(self.sensors[t], key) for t in self.types[cid] if t in self.sensors]
            if pds:
                self.risk[cid] = update_no_finding(self.risk[cid], sum(pds) / len(pds))
        for d in obs.detections:
            sp = self.sensors.get(d.fault_type)
            if sp:
                self.risk[cid] = update_finding(self.risk[cid], getattr(sp, key), sp.p_false_positive, d.confidence)
            if d.confidence >= self.top[cid][1]:
                self.top[cid] = (d.fault_type, d.confidence)
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

    # ---------- start positions ----------

    def start_positions(self, n: int = 5) -> list[StartPosition]:
        candidates = sorted((s for s in self.segs if s not in self.dead),
                            key=lambda s: -self.segs[s]["properties"]["length_km"])[:n]
        out = []
        for i, sid in enumerate(candidates):
            mid = shape(self.segs[sid]["geometry"]).interpolate(0.5, normalized=True)
            out.append(StartPosition(drone_id=f"D{i + 1}", segment_id=sid, position=[mid.x, mid.y],
                                     reason=f"placeholder: midpoint of long line {sid} "
                                            f"({self.segs[sid]['properties']['length_km']:.1f} km)"))
        return out

    # ---------- routes: nearest unchecked line ----------

    def _landable(self, sid: str | None) -> bool:
        return sid in self.segs and sid not in self.dead and self.p_live[sid] >= 0.5

    def _targets(self, sid: str) -> list[int]:
        """Cell indices on this segment still worth a pass: never inspected, or a finding that needs confirmation."""
        out = []
        for i, cid in enumerate(self.cells_by_seg.get(sid, [])):
            confirm = (self.top[cid][0] and 0.4 <= self.risk[cid] <= 0.8 and self.inspected[cid] == "air"
                       and self.air_passes[cid] < 3)
            if self.inspected[cid] == "none" or confirm:
                out.append(i)
        return out

    def _nearest_landing(self, xy: tuple[float, float]) -> tuple[str, tuple[float, float]] | None:
        pt = Point(xy)
        opts = [s for s in self.segs if self._landable(s)]
        if not opts:
            return None
        sid = min(opts, key=lambda s: self.seg_m[s].distance(pt))
        p = self.seg_m[sid].interpolate(self.seg_m[sid].project(pt))
        return sid, (p.x, p.y)

    def next_route(self, drone: DroneState, speed_mps: float = 10, landing_min: float = 3) -> list[Waypoint] | None:
        p = self.drone
        if drone.status != "charging" and not self._landable(drone.segment_id):
            # just landed on a dead line: hop to the nearest landable line using the reserve
            here = to_xy(*drone.position)
            land = self._nearest_landing(here)
            if not land:
                return None
            dist = Point(here).distance(Point(land[1]))
            if dist / p.speed_transit_mps > (drone.battery_min - p.landing_min) * 60:
                return None
            pos = list(to_lonlat(*land[1]))
            return [Waypoint(position=drone.position, segment_id=None), Waypoint(position=pos, segment_id=None),
                    Waypoint(position=pos, segment_id=land[0])]
        if drone.status == "charging" and drone.battery_min < min(MIN_TAKEOFF_BATTERY_MIN, p.max_flight_min):
            if drone.battery_min < p.landing_min + RESERVE_MIN + 2:
                return None
            full = self._plan(drone, p.max_flight_min)  # a short remaining job may need less than 20 min
            need = (full[1] / 60 + p.landing_min + RESERVE_MIN) if full else p.max_flight_min
            if drone.battery_min < min(need, MIN_TAKEOFF_BATTERY_MIN, p.max_flight_min):
                return None
        plan = self._plan(drone, drone.battery_min)
        if not plan:
            return None
        route, _, segs = plan
        self.claimed[drone.drone_id] = segs
        return route

    def _plan(self, drone: DroneState, battery_min: float) -> tuple[list[Waypoint], float, set[str]] | None:
        """Greedy: repeatedly transit to the nearest unchecked line and fly along it, within the battery
        budget (landing + reserve kept). The route must end on a landable line."""
        p = self.drone
        budget = (battery_min - p.landing_min - RESERVE_MIN) * 60
        taken = set().union(*(s for d, s in self.claimed.items() if d != drone.drone_id))
        here, here_seg = to_xy(*drone.position), drone.segment_id
        route = [Waypoint(position=drone.position, segment_id=drone.segment_id)]
        t, flown, skipped = 0.0, set(), set()

        def hop_time(xy, sid):
            if self._landable(sid):
                return 0.0
            land = self._nearest_landing(xy)
            return Point(xy).distance(Point(land[1])) / p.speed_transit_mps if land else float("inf")

        all_targets = {s: self._targets(s) for s in self.segs if s not in taken}
        while True:
            cands = [s for s, tg in all_targets.items() if tg and s not in flown and s not in skipped]
            if not cands:
                break
            here_pt = Point(here)
            sid = min(cands, key=lambda s: self.seg_m[s].distance(here_pt))
            line, targets = self.seg_m[sid], all_targets[sid]
            n = len(self.cells_by_seg[sid])
            if sid == here_seg and line.distance(here_pt) < ON_LINE_M:
                s0 = line.project(here_pt)
                cur = min(n - 1, int(s0 / line.length * n))
                ahead = sum(1 for i in targets if i >= cur)
                behind = sum(1 for i in targets if i <= cur)
                s1 = line.length if ahead > behind or (ahead == behind and s0 < line.length / 2) else 0.0
            else:
                d0 = Point(line.coords[0]).distance(Point(here))
                d1 = Point(line.coords[-1]).distance(Point(here))
                s0, s1 = (0.0, line.length) if d0 <= d1 else (line.length, 0.0)
            entry = line.interpolate(s0)
            t_transit = Point(here).distance(entry) / p.speed_transit_mps
            end = line.interpolate(s1)
            t_along = abs(s1 - s0) / p.speed_inspect_mps
            if t + t_transit + t_along + hop_time((end.x, end.y), sid) > budget:
                if not self._landable(sid):
                    skipped.add(sid)  # doesn't fit, try the next nearest
                    continue
                avail = (budget - t - t_transit) * p.speed_inspect_mps
                if avail < 200:  # less than ~half a cell left: stop here
                    break
                s1 = s0 + avail if s1 > s0 else s0 - avail
                t_along = avail / p.speed_inspect_mps
                stop = True
            else:
                stop = False
            piece = substring(line, s0, s1)
            if Point(here).distance(entry) > 1:
                route.append(Waypoint(position=list(to_lonlat(entry.x, entry.y)), segment_id=None))
            route += [Waypoint(position=list(to_lonlat(x, y)), segment_id=sid) for x, y in list(piece.coords)[1:]]
            t += t_transit + t_along
            last = piece.coords[-1]
            here, here_seg = (last[0], last[1]), sid
            flown.add(sid)
            if stop:
                break
        if len(route) == 1:
            return None
        if not self._landable(here_seg):  # end of inspection is on a dead line: hop to a live one
            land = self._nearest_landing(here)
            if not land:
                return None
            t += Point(here).distance(Point(land[1])) / p.speed_transit_mps
            pos = list(to_lonlat(*land[1]))
            route += [Waypoint(position=pos, segment_id=None), Waypoint(position=pos, segment_id=land[0])]
        return route, t, flown

    # ---------- output ----------

    def cell_states(self) -> list[CellState]:
        return [CellState(cell_id=c, risk=round(min(1.0, max(0.0, r)), 3), inspected=self.inspected[c],
                          top_fault=self.top[c][0]) for c, r in self.risk.items()]

    def segment_states(self) -> list[SegmentState]:
        return [SegmentState(segment_id=s, p_live=round(self.p_live[s], 3)) for s in self.segs]


from app.planner.smart import SmartPlanner  # noqa: E402  (needs Planner defined above)

PLANNERS = {"smart": SmartPlanner, "dumb": DumbPlanner}
