# Backend

FastAPI app: API (PLAN.md §3), on-demand simulation, planner. Everything in memory.

## Run

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs. Quick try:

```bash
curl -s localhost:8000/api/static | uv run python -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["default_inputs"]))' | curl -X PUT localhost:8000/api/inputs -H 'content-type: application/json' --data-binary @-
curl -X POST localhost:8000/api/plan/start-positions
curl -X POST localhost:8000/api/sim/control -H 'content-type: application/json' -d '{"action":"start","speed":60}'
curl localhost:8000/api/state
```

## Tests

```bash
uv run pytest -q
```

## Planner: smart vs dumb

`SmartPlanner` is the default. Run the M1 baseline instead with `PLANNER=dumb uv run uvicorn app.main:app --port 8000`; `LAND_ON_FINDING=0` turns off landing on findings (see below).

How SmartPlanner works:
- **Belief** = P(fault of type t in cell). Prior from reports (confidence × w(type), spread thinner over big polygons), forest share of the 30 m corridor, storm footprint, dead lines. Every observation is a Bayes update per type, using the published sensor specs.
- **Value of a look** = expected detections = Σ_t p_t · p_detect_t (air or landed). Confirmed faults above the crew threshold are worth nothing more. That one number drives everything below.
- **Start positions**: greedy, one drone at a time. Each drone gets the start whose real first flight collects the most value, plus 3 × the landed-only checks around its first landing (a drone charging there can hop to them later; this is what stations a drone in the galloping report area). Cells already covered by an earlier drone count only 15 %. The first flights are then flown exactly as planned.
- **Routes**: greedy runs along lines, ranked by value / time. Transit counts 4× (nothing is inspected while transiting). The flight always keeps enough battery to land on a live line with a 5 min reserve. The landing spot is chosen for what landed-only sensors can still find there, the value left nearby for the next flight and p_live. Only one drone per landing line, so a line that turns out dead strands nobody else.
- **Confirmation**: a single detection is only "possible". It needs a second detection or a confident landed one before "Send repair crew"; until then the cell keeps its value, so the next flight or charging hop looks again.
- **Land on finding** (`LAND_ON_FINDING=1`, default; `0` = old behaviour): when a flying drone's air detection leaves a cell in "needs confirmation", the drone breaks off its flight and lands on that cell right away, so the landed sensors confirm it. Only if the line is believed live, no other drone lands or charges on it, and the battery covers the hop + 3 min landing + 5 min reserve. A drone charging within 2 km that can make the hop takes it instead (the flyer keeps its plan). Lines not known live are only chosen as landing spots if another free landable line is within hop range of the reserve.
- **Sensor use**: while charging, a drone makes short take-off/land hops to nearby cells where only landed sensors help (galloping/sag in the report 06 area) or to confirm a finding. A dead line found by landing is dropped as a charging spot for all drones; drones already on their way to land there divert.
- **Explaining away**: when a whole report area is cleared, that report's contribution is removed from the inspected cells (cell-wise Bayes would keep most of it), so drones don't re-fly a cleared false alarm.

Comparison on the demo scenario, 3 h sim, seeds 1–10 (`uv run python scripts/compare_planners.py 3 1 2 3 4 5 6 7 8 9 10`). Times are the first logged detection of each fault, averaged over the runs that found it:

| planner | faults found (of 4) | mean time to detection | F1 vegetation (report 02) | F2 hotspot (report 05) | F3 galloping (report 06, landed only) | F4 vegetation (forest, no report) | cells inspected | landings | stranded |
|---|---|---|---|---|---|---|---|---|---|
| dumb | 1.8 | 56 min | 0/10 | 8/10, 117 min | 0/10 | 10/10, 8 min | 178 | 10.0 | 1.0 |
| **smart** | **3.4** | **48 min** | **8/10, 7 min** | 9/10, 126 min | **8/10, 55 min** | 9/10, 1 min | 138 | 14.1 | **0.0** |

On the `default` preset (4 h, seeds 1–10, `SCENARIO=default ...`): dumb 1.7 faults found, smart 2.5 (F1 9/10 at 19 min, F2 10/10, F3 galloping 6/10, F4 0/10; F4 there sits in a weakly forested cell, 15 % forest).

Caveats, honestly: the demo ground truth was placed knowing where the planner flies (see "Demo script"), so the demo numbers flatter smart. Dumb finds F4 only because its "5 longest lines" start puts a drone on LINE_035. Smart misses F1 in 2/10 runs: the camera misses it on the first pass (p = 0.85) and the report 02 area is then cleared, a real risk of one-pass clearing. Galloping detection still depends on the start-position station weight (F3 on `default`: 0/10 with weight 1, 6/10 with weights 2–4). Compute time is about 5 s for a 3 h sim.

## Demo script

`scenarios/demo.json` is loaded at startup (`default.json` stays available as a preset). The sim starts (and Reset returns) with no operator inputs: click **Load defaults** in the Inputs step to load the 6 reports + LINE_024 known dead, then find start positions and start. Seed 2, LINE_024 known dead (operator), hidden dead line LINE_015 (the line that tripped in report 03's protection sector). Faults: F1 vegetation LINE_026 km 4.2 (report 02), F2 hotspot LINE_002 km 1.7 (report 05), F3 galloping LINE_054 km 6.6 (report 06, landed only), F4 vegetation LINE_035 km 11.8 (forest, outside every report area). Report 04 (flash) has no fault: it was the generator test.

Sim time → what happens (at 300×, 1 sim minute ≈ 0.2 s wall time, 3 h ≈ 36 s):

| t (sim) | event | talking point |
|---|---|---|
| 0 | 5 drones start: D1 report 04, D2 report 02, D3 LINE_004/LINE_024 forest, D4 LINE_035 forest, D5 report 06 | start positions = best first flight + where landed-only sensors matter; each has a `reason` |
| 1 min | D4: possible vegetation LINE_035 km 11.8 → "Needs confirmation"; "D4 re-routes to land at LINE_035 km 11.8 to confirm possible vegetation" | F4: no report covers it, the vegetation layer sent D4 here. Land on finding: the drone that saw it lands on it |
| 4 min | D4 landed: vegetation confirmed (landed camera) → "Send repair crew to LINE_035 km 11.8"; D4 takes off again with the rest of its battery | F4 confirmed 3 min after it was found (about 2 h with `LAND_ON_FINDING=0`) |
| 6 min | "Area of SYN_REPORT_04 cleared" | the flash was the generator test |
| 7 min | D2 flies over F1 (LINE_026 km 4.2), the camera misses it this time (p = 0.85) | single passes can miss |
| 24 min | D2 lands on LINE_015: no current → line dead, hops to LINE_030 | hidden dead line; LINE_015 is dropped as a charging spot for everyone |
| 24–25 min | all drones land on live lines, one per line, and charge | 30 min flight, 3 h charge: this is the bottleneck |
| 55 min | D5 hops to the next span on LINE_054: galloping confirmed → "Send repair crew to LINE_054 km 6.6" | F3, only the landed accelerometer can see it |
| 56–115 min | drones charge | pause / talk about sensors and the risk map |
| 116 min | D3: possible vegetation LINE_026 km 4.2 → re-routes to land on it; the second look on the way down confirms it → "Send repair crew" | F1, report 02 area |
| 126 min | D1: possible hotspot LINE_002 km 1.7 (thermal) → "Needs confirmation". A wrong magnetometer reading made LINE_002 doubtful (p_live 0.5), so no re-route; D1 lands on the next span as planned (it is live) | all four faults found (auto-pause) |
| 151 min | D1 hops over from the next span as soon as its battery covers hop + landing + reserve: hotspot confirmed → "Send repair crew to LINE_002 km 1.7"; lands on it at 154 min | all four confirmed |

## Frontend mocks / OpenAPI

After changing `app/models.py` (= the contract), regenerate and commit:

```bash
uv run python scripts/export_mocks.py   # -> frontend/src/mocks/{static,state}.json, frontend/openapi.json
```

## Layout

- `app/models.py` Pydantic models = API contract
- `app/data.py` loads `resources/`, splits lines into ~500 m cells, default operator inputs
- `app/planner/` (never imports `sim/`): `Planner` interface + `DumbPlanner` (M1 baseline: nearest unchecked line) in `__init__.py`; `smart.py` `SmartPlanner` (default); `risk.py` prior per fault type + pure Bayes updates; `start_positions.py` greedy max-coverage; `missions.py` greedy routes, landing choice, dead-line hop; `recommend.py` §4.5 recommendations
- `app/sim/engine.py` sim stepping (1 s steps, wall time x speed, seeded), cell inspection, findings
- `app/sim/sensors.py` sensor model: ground truth -> noisy `Observation` (air per cell, landed per cell, magnetometer)
- `app/main.py` endpoints
- `scenarios/*.json` `SimConfig` presets (ground truth + sensor/drone params); `demo.json` (scripted story) is loaded at start, `default.json` is the older preset. Listed by `GET /api/sim/presets`, fetched by `GET /api/sim/presets/{name}` (sim-setup view)
