# Storm Patrol

After a storm, five mobile sensor packages (30 min flight, 3 h charge, charging only on a live line) help a grid operator find faults. Storm Patrol turns vague storm reports, known dead lines and a vegetation layer into a **risk map** of the grid, picks where the five drones start, and then plans every flight: which line sections to inspect, where to land (and charge, or confirm a finding with landed sensors). Each observation updates the risk map with a Bayes update and the plan follows. The operator watches it live and gets **recommendations**: send a repair crew, needs confirmation, inspect from the ground, area cleared. A built-in simulation (ground-truth faults and dead lines the planner never sees) drives the demo.

![Operator view](docs/screenshot.png)

## Quick start

Needs [uv](https://docs.astral.sh/uv/) and Node 20+. From the repo root:

```bash
make dev   # backend on :8000 + frontend on :5173, prefixed logs; Ctrl-C stops both
```

Then open http://localhost:5173. Also: `make install`, `make backend`, `make frontend`, `make test`. Without make: `./dev.sh`. Other ports: `BACKEND_PORT=8001 FRONTEND_PORT=5174 make dev`.

## Run

Backend (Python, [uv](https://docs.astral.sh/uv/)):

```bash
cd backend
uv run uvicorn app.main:app --port 8000
```

Frontend (Node 20+), then open http://localhost:5173:

```bash
cd frontend
npm install && npm run dev
```

## Two views

- **Operator** (`/`): three steps. ① Inputs: load the default reports or upload GeoJSON, click lines to mark them known dead. ② Find start positions: hover a marker to see why it was chosen. ③ Start mission: drones fly live; the sidebar shows what each drone is doing, the recommendations to act on and the event log (click to fly the map there). Top bar: clock, play/pause, speed 1×–300×, reset. Benchmark: when every ground-truth fault has been found (first logged finding of its type on its cell) the sim pauses once and a banner shows the sim time plus a per-fault breakdown; press play to continue.
- **Simulation** (`/setup`): the hidden ground truth. Click a line to make it dead or add a fault, tune sensor detection probabilities and drone parameters, load/save scenario presets as JSON.

## Architecture

- One FastAPI process, everything in memory. The frontend fetches `GET /api/static` once and polls `GET /api/state` every 500 ms; it only renders and interpolates drone positions.
- The sim advances on demand (each poll steps it by wall time × speed, 1 s steps), seeded so the same scenario gives the same run.
- `backend/app/planner/` never imports `sim/`: it only receives observations (events), like real drones would send them.
- Planner: per-cell, per-fault-type belief (reports + OSM vegetation + storm footprint + dead lines), greedy start positions and routes by expected detections per flight time, battery and live-line landing constraints.
- Frontend: React + Vite + TypeScript, MapLibre (`react-map-gl`), Tailwind. See [frontend/README.md](frontend/README.md) and [backend/README.md](backend/README.md).

## Assumptions

All numbers and modelling choices (speeds, charging, sensor probabilities, report-to-confidence mapping, planner weights) are in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## Limitations

- **Synthetic data**: the network, reports and the ground-truth faults are fictional; sensor detection probabilities and planner weights are hand-tuned guesses.
- **Wind is not modelled**: no flight ban above 36 km/h gusts, no effect on speed or battery.
- Lines are treated electrically independent: no power-flow or connectivity model, so a dead line only nudges its neighbours' risk.
- Sensors are a simple per-cell probabilistic model (one air pass or one landing per cell), not real imagery.
- Drones fly straight cross-country with no airspace, terrain or collision constraints.
- The simulation is open-ended (the 4 h `scenario_window_hours` in the public config is unexplained).
- The basemap is fetched from OpenFreeMap; offline the map falls back to a plain background.
