"""THE API contract. Field names are mirrored in the frontend types.

Changing anything here = PR touching models.py + frontend/src/mocks + frontend/openapi.json
(regenerate with `uv run python scripts/export_mocks.py`).
GeoJSON objects are typed loosely as dicts.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

# [lon, lat] in WGS84 degrees
LonLat = list[float]
GeoJSON = dict[str, Any]

FaultType = Literal["vegetation", "sag", "ice", "galloping", "hotspot", "broken_conductor"]
SimStatus = Literal["setup", "running", "paused"]
DroneStatus = Literal["charging", "flying", "landing", "stranded"]
Inspected = Literal["none", "air", "landed"]
EventType = Literal["takeoff", "landing", "finding", "line_dead", "recommendation", "stranded", "all_found",
                    "reroute"]


# ---------- operator inputs ----------

class Report(BaseModel):
    report_id: str
    polygon: GeoJSON  # GeoJSON Polygon geometry
    fault_types: list[FaultType]
    confidence: float = Field(ge=0, le=1)
    text: str


class OperatorInputs(BaseModel):
    reports: list[Report] = []
    known_dead_segments: list[str] = []


class StartPosition(BaseModel):
    drone_id: str
    segment_id: str
    position: LonLat
    reason: str


# ---------- static (fetched once) ----------

class Static(BaseModel):
    segments: GeoJSON  # FeatureCollection, props: segment_id, voltage_kv, length_km, from_node, to_node, ...
    cells: GeoJSON  # FeatureCollection, props: cell_id "LINE_001#03", segment_id, index, length_m
    nodes: GeoJSON  # FeatureCollection, props: node_id, node_type, ...
    storm: GeoJSON  # Feature (synthetic storm footprint polygon)
    default_inputs: OperatorInputs
    vegetation: GeoJSON  # FeatureCollection (may be empty)


# ---------- state (polled) ----------

class DroneState(BaseModel):
    drone_id: str
    position: LonLat
    heading: float = 0  # degrees, 0 = north, clockwise
    status: DroneStatus
    battery_min: float  # 0..max_flight_min
    max_flight_min: float = 30  # full battery (for the UI's battery ring)
    segment_id: str | None = None
    route: list[LonLat] = []  # planned path of the current flight (for drawing)


class CellState(BaseModel):
    cell_id: str
    risk: float = Field(ge=0, le=1)
    inspected: Inspected = "none"
    top_fault: FaultType | None = None


class SegmentState(BaseModel):
    segment_id: str
    p_live: float = Field(ge=0, le=1)


class Event(BaseModel):
    t: float  # sim seconds
    type: EventType
    drone_id: str | None = None
    cell_id: str | None = None
    segment_id: str | None = None
    fault_type: FaultType | None = None
    confidence: float | None = None
    position: LonLat | None = None  # addition to the plan: where it happened (for "fly map to event")
    message: str


class FaultFound(BaseModel):
    """Benchmark (sim ground truth, not seen by the planner): when a true fault was first found / confirmed."""
    fault_id: str
    fault_type: FaultType
    segment_id: str
    cell_id: str
    km: float  # position along the line, as in the log messages
    t_found: float  # sim seconds of the first logged finding of this type on the fault's cell
    drone_id: str
    t_confirmed: float | None = None


class Progress(BaseModel):
    """How many ground-truth faults have been found; all_found_t is set once (and the sim pauses) when all are."""
    total: int = 0
    found: list[FaultFound] = []
    all_found_t: float | None = None
    all_confirmed_t: float | None = None


class CellObservation(BaseModel):
    """One inspection pass of a cell (also "nothing found") with the cell risk before/after it.
    Only sent for cells behind a recommendation (recommend.relevant_cells), for its report panel."""
    t: float
    drone_id: str
    cell_id: str
    mode: Literal["air", "landed"]
    detections: list["Detection"] = []
    risk_before: float
    risk_after: float


class State(BaseModel):
    t: float  # sim seconds since start
    speed: float
    status: SimStatus
    drones: list[DroneState]
    cells: list[CellState]
    segments: list[SegmentState]
    inputs: OperatorInputs
    start_positions: list[StartPosition]
    events: list[Event]  # full log, newest last
    progress: Progress = Progress()
    observations: list[CellObservation] = []  # simulated passes behind the recommendations, oldest first


# ---------- simulation ground truth + params ----------

class Fault(BaseModel):
    fault_id: str
    segment_id: str
    position: LonLat
    fault_type: FaultType


class SensorParams(BaseModel):
    p_detect_air: float = Field(ge=0, le=1)
    p_detect_landed: float = Field(ge=0, le=1)
    p_false_positive: float = Field(ge=0, le=1)
    sensor: str = ""  # human-readable: which sensor detects this fault type


class DroneParams(BaseModel):
    speed_inspect_mps: float = 10
    speed_transit_mps: float = 16
    max_flight_min: float = 30
    full_charge_min: float = 180
    landing_min: float = 3
    start_battery_min: float = 30


class SimConfig(BaseModel):
    seed: int = 42
    dead_segments: list[str] = []  # hidden ground truth; operator's known_dead_segments are also dead in the sim
    faults: list[Fault] = []
    sensors: dict[FaultType, SensorParams] = {}
    drone: DroneParams = DroneParams()


class SimControl(BaseModel):
    action: Literal["start", "pause", "resume", "reset"]
    speed: float | None = Field(default=None, ge=1, le=600)


# ---------- internal (planner <-> sim), not part of the HTTP API ----------

class Waypoint(BaseModel):
    position: LonLat
    segment_id: str | None = None  # set = flying along this line (inspecting, slow); None = transit (fast)


class Detection(BaseModel):
    fault_type: FaultType
    confidence: float  # the sensor's own certainty 0..1


class Observation(BaseModel):
    """One inspected cell (air pass or landed), INCLUDING passes with no finding. Sim -> planner only."""
    t: float
    drone_id: str
    cell_id: str
    segment_id: str
    mode: Literal["air", "landed"]
    detections: list[Detection] = []  # empty = nothing found
    reads_live: bool | None = None  # magnetometer (air) or current sensor (landed); None = no reading
    live_accuracy: float = 1.0  # P(reading is correct)


CellObservation.model_rebuild()
