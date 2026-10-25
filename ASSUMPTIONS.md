# Assumptions

Draft values (see PLAN.md). Change them here when the team decides.

## Drone
- Transit speed 16 m/s (Nomad website). Inspection speed along a line: 10 m/s (assumed, so the camera can see).
- One flight ≤ 30 min. Landing/docking takes 3 min and counts as flight time (website: "under 5 min").
- Charging is linear: 180 min for a full 30 min. Partial charges allowed.
- All drones start with a full battery, already docked on a live line chosen by the planner.
- A drone keeps a 5 min reserve so it can hop to another line if its landing line turns out to be dead.
- A drone landed on a dead line with no reserve is `stranded` (out of the game).
- Drones may fly cross-country in straight lines at transit speed. They only inspect while flying along a line.
- Wind is ignored: no flight ban above 36 km/h gusts and no effect on speed or battery (limitation).

## Sensors
- Camera, thermal, acoustic: work during flight along the line (per cell, probabilistic).
- Current, sag (IMU), vibration, conductor temperature: only when landed, only for the span where the drone sits.
- Current ≈ 0 on landing ⇒ line is dead ⇒ no charging.
- Sim model (`backend/app/sim/sensors.py`): every cell a drone enters while flying along a line is one air pass; landing inspects only the cell under the drone. Per pass and fault type: a true fault is detected with `p_detect_air` / `p_detect_landed`, otherwise a false positive appears with `p_false_positive`. A fault type with `p_detect_air = 0` is landed-only (galloping in the default scenario).
- The sensor reports its own confidence: true detections 0.6–0.95 from the air, 0.85–0.99 landed; false positives 0.3–0.7. The planner's Bayes update mixes posterior and prior by this confidence, so a shaky single detection lands in "needs confirmation" (0.4–0.8) rather than "send crew".
- Proposed 50 Hz magnetometer: one live/dead reading per segment per flight, correct with p = 0.9. The current sensor on landing always gives the truth.
- Sensor and drone specs (`SimConfig.sensors`, `SimConfig.drone`) are treated as known to the planner (calibration data). Fault locations and dead lines are not.
- False-positive rates in the default scenario are low (0.2–0.5 % per cell pass and fault type) so the log is not flooded; 0.1 % in the demo scenario.
- Confirmation (PLAN.md §4.5 "after a confirmed finding"): a fault is confirmed by a second detection of the same type in the cell, or by one landed detection with confidence ≥ 0.8. "Send repair crew" only fires for confirmed faults; until then the cell shows "Needs confirmation" and keeps its value for the planner, so a second pass or landing is scheduled.
- Log: the first detection is logged as "possible …", the confirming one as "… confirmed". An unconfirmed detection that leaves the cell below 40 % risk (e.g. a weak false positive on a low-prior cell) is not logged at all; the belief update still happens.

## Grid / data
- Lines are split into ~500 m cells. Risk and coverage are tracked per cell.
- The operator can mark lines as known dead. LINE_024 is pre-filled (report 03). Operator knowledge is assumed correct, so the sim treats these lines as dead too.
- A line's live/dead status is unknown until a drone lands on it or we get a report. Prior `p_live` = 0.9.
- Vegetation comes from a one-off OSM Overpass export (forest, wood, tree rows) and is stored offline.
- Simulation length: open-ended (pause/stop manually). `scenario_window_hours: 4` in the config is unexplained; check with the organisers.
- Cells: each line is cut into `n = max(1, round(length / 500 m))` equal pieces (418 cells). `cell_id = "LINE_001#03"` with a 0-based, 2-digit index. Short lines (< 250 m) are one cell. Distances use a local equirectangular projection around the study-area centre (error well below 1 %).

## Report mapping (default operator inputs)
Report polygon = `location_area_coordinates`. Report text = `description`. Textual confidence → number: low 0.2, low to medium 0.35, medium 0.5, medium to high 0.65, high 0.8. For compound texts ("high that an event was logged; low for its location") we use the location/asset-level part, so reports 01 and 03 get 0.2.

| Report | information_type | fault_types | confidence |
|---|---|---|---|
| 01 | weather_alert | vegetation, galloping, broken_conductor | 0.2 |
| 02 | vegetation_and_access_report | vegetation | 0.35 |
| 03 | protection_event_summary | broken_conductor, vegetation | 0.2 |
| 04 | visual_flash_report | hotspot, broken_conductor | 0.35 |
| 05 | sound_and_warmth_report | hotspot | 0.35 |
| 06 | conductor_motion_report | galloping, sag | 0.2 |

## Simulation (M1)
- Play requires at least one loaded/uploaded report. An early Play click stays in setup and highlights Play and the input controls red; marking dead lines alone does not satisfy this prerequisite. The API enforces the same rule.
- Sim time advances in 1 s steps; one HTTP poll advances `wall time × speed` (default speed 60×). Same `seed` ⇒ same run.
- Charging drones ask the planner for a route every 60 sim-s. Placeholder rule: take off once battery ≥ min(20 min, what the next full route needs incl. landing + 5 min reserve).
- M1 routing (DumbPlanner): transit to the nearest line with uninspected cells (or a finding that needs confirmation), fly along it, repeat while the battery allows; end on a line not known to be dead (`p_live ≥ 0.5`), else add a hop to the nearest such line. Drones don't take lines another drone is currently flying.
- A dead line (landing or operator) adds 0.2 risk to each of its cells (DumbPlanner only; SmartPlanner: see below).
- Recommendations (§4.5) are emitted once per cell / segment / report: risk > 0.8 with a finding → send crew; 0.4–0.8 with a finding → needs confirmation; dead line fully inspected without finding → inspect from ground; all cells of a report area inspected and < 0.4 → area cleared.
- Default ground truth (`backend/scenarios/default.json`): hidden dead line LINE_036; faults: vegetation on LINE_026 (report 02 area), hotspot on LINE_002 (report 05 area), galloping on LINE_039 (report 06 area), vegetation on LINE_035 (outside the specific report areas 02–06; report 01 covers the whole region; picked without the vegetation layer, which is not in the repo yet). Galloping is landed-only (`p_detect_air = 0`). Sensor probabilities there are guesses; hotspot assumes the extra thermal camera.

## Smart planner (M2, `backend/app/planner/`)
Belief = P(fault of type t in cell), with cell risk = 1 − Π(1 − p_t). All weights are hand-calibrated guesses.

**Risk prior** (`risk.py`):
- Reports: p = confidence × w(type) × min(1, 10 / n_cells), where n_cells = cells touching the polygon. w = vegetation 1.0, galloping 1.0, hotspot 0.9, sag 0.7, ice 0.5, broken_conductor 0.6. Report 01 covers all 418 cells, so it adds only ~0.005 per cell.
- Vegetation: 0.6 × (share of the cell's 30 m buffer covered by OSM forest/wood/tree rows).
- Background per cell: 0.4 % vegetation and broken conductor, 0.2 % sag and hotspot, 0.1 % galloping.
- Storm footprint: background and vegetation of wind-driven types × 1.0 inside, × 0.4 outside. Reports and operator knowledge are direct observations and are not damped.
- Dead line (operator or found by landing): p_live = 0, never a landing/charging spot. Broken-conductor p += max(0.02, 0.3 / n_cells of the line). Lines sharing a node with it get +0.03 and p_live ≤ 0.8. Node sharing does not prove electrical connection, so the effect is kept modest.
- Protection event report (text contains "protection", report 03): lines touching its polygon get p_live 0.8 (some line there may have tripped). Start positions need p_live ≥ 0.85.
- Result on the default inputs: cell risk from 0.02 to 0.62, median 0.05.

**Belief update**: per fault type, using the published p_detect_air / p_detect_landed / p_false_positive. Per type: no detection → p·(1−pd)/(p·(1−pd)+1−p); detection → confidence-softened Bayes, as in M1. A top fault whose belief drops below 0.1 is no longer shown. A line is landable if p_live ≥ 0.6, so a single "dead" magnetometer reading rules it out.

**Planning**:
- Value of a look = expected detections = Σ_t p_t·pd_t. Types with p_t ≥ 0.8 (already "send crew") count 0.
- Routes (`missions.py`): greedy runs between cell boundaries along a line, scored by value / (4 × transit s + inspect s + 60 s). A run is allowed only if the drone can still reach a landable cell afterwards with landing time and the 5 min reserve kept.
- Landing score = 3 × landed value + 0.4 × air value within 4 km + 1 × station value + 0.3 × p_live. Station value = the landed-only value of the 6 best cells within 3 km.
- Take-off: normal flights at ≥ 20 min battery. If an uncertain finding is worth ≥ 0.04 detections per battery-minute, the flight is forced to land on it. While charging (battery ≥ 10 min), a drone hops to a landing target worth ≥ 0.02 per battery-minute. A flight worth < 0.03 expected detections is not flown. Drones don't value cells another drone is flying and don't share landing cells.
- Start positions (`start_positions.py`): greedy over drones, one per line, ≥ 3 km apart, p_live ≥ 0.85. The 40 best candidates (by a cheap proxy) are scored by the value of their real first flight plus 3 × the station value at the first landing (a station serves several hops over the next hours; with weight 1 no drone was stationed in the galloping area, weights 2–4 give the same result). Cells already covered by an earlier drone keep 15 % of their value. The first flights are then flown exactly as planned.
- Land on finding (config flag `LAND_ON_FINDING`, default on; `0` = old behaviour). Routes are still planned per flight, but after an air detection the planner may re-plan the current flight: if the cell is in "needs confirmation" (risk ≥ 0.4, unconfirmed detection) and one landing would confirm it with P ≥ 0.1 (p_t × p_detect_landed), the drone transits to the cell and lands there now (logged as "D4 re-routes to land at LINE_035 km 11.8 to confirm possible vegetation"). Conditions: line landable (p_live ≥ 0.6, not dead), no other drone landing or charging on that line, battery ≥ transit + 3 min landing + 5 min reserve. The sensor model only senses the cell under a landed drone, so there is no "nearest landable cell" fallback: if the finding's own line is not landable the plan is kept. A drone charging within 2 km that can make the hop (with landing + reserve) takes it instead, and charging drones hop to an unconfirmed finding within 2 km as soon as their battery allows. With the flag on, a landing spot on a line not known live also needs another free landable line within hop range of the reserve ((5 − 3) min transit ≈ 1.9 km); without it, the shifted homes stranded D1 on the hidden dead line in 7 of 10 default-scenario seeds. Charging stays linear with partial take-off.
- One drone per landing line (first flights and later flights): if the line turns out dead only one drone has to hop. Drones already flying to a line that another drone just found dead divert to the best reachable live cell.
- Explaining away: when a report area is cleared, the inspected cells of that area are reset to "prior without that report, after one air pass" (if lower). Reports are about one event in the area, so cell-wise independent Bayes would keep too much of the report's evidence and drones would re-fly the cleared area.
- The 5 min reserve is kept even when landing on a line known to be live (simple, conservative).

## Demo scenario (`backend/scenarios/demo.json`, loaded at startup)
Scripted for the PLAN.md §7 story; the ground truth was placed knowing where the planner flies, so it is a demonstration, not a benchmark (`compare_planners.py` runs seeds 1–10 for that).
- Seed 2 (seeds 3, 4, 5, 9, 11, 16 … give the full story too; in seeds where the camera misses F1 on its first pass the report 02 area is wrongly cleared).
- Hidden dead line LINE_015: it lies in the report 03 protection sector, so "the line that tripped" is plausible. D2's first flight lands there.
- F1 vegetation LINE_026#04 (report 02, as in default). F2 hotspot LINE_002#03 (report 05; moved one cell so the second-round flight covers it within 3 h). F3 galloping LINE_054#13 (report 06; the cell the stationed drone hops to first). F4 vegetation LINE_035#23: 100 % forest, in the storm, outside every report area. The old F4 cell (LINE_035#12) is only 15 % forest and ranked ~70th by the prior, so no drone reached it in 4 h. #23 is ranked 5th of 418 cells, almost entirely because of the vegetation layer.
- Sensors as in default except false positives 0.1 % per pass and type.
