# Nomadic drone hackathon
**Find a blog post with a detailed explanation of the approach [here](https://michmos.github.io/posts/nomadic-hackathon)** 

## Challenge
A storm has just passed over a section of the Bavarian overhead grid. Some areas have lost power and the operator needs to know where to look first. The toolkit is five mobile sensor packages, basically drones that sit on power lines. Each one can fly for 30 minutes, then has to charge for 3 hours, and it can only charge on a live line.

What we had to start from was a map of line segments and six fictional storm reports ("a flash was seen somewhere around here", "trees in the area", confidence: low to medium). The reports are vague and possibly wrong. The questions: what do you measure, where do the drones start, how do they move as information comes in, and when is the evidence strong enough to send a repair crew?


https://github.com/user-attachments/assets/0970a3e1-9dc5-4b21-bb7a-d080fed83f09


## Quick start

Needs [uv](https://docs.astral.sh/uv/) and Node 20+. From the repo root:

```bash
make dev   # backend on :8000 + frontend on :5173, prefixed logs; Ctrl-C stops both
```

Then open http://localhost:5173. Also: `make install`, `make backend`, `make frontend`, `make test`. Without make: `./dev.sh`. Other ports: `BACKEND_PORT=8001 FRONTEND_PORT=5174 make dev`.


## Two views

- **Operator** (`/`): three steps. 
  1. Inputs: upload reports in GeoJSON, draw other high risk areas and click lines to mark them known dead.
  2. Find start positions: hover a marker to see why it was chosen.
  3. Start mission: drones fly live; the sidebar shows what each drone is doing, the recommendations to act on and the event log.

- **Simulation** (`/setup`): the hidden ground truth. Click a line to make it dead or add a fault, tune sensor detection probabilities and drone parameters, load/save scenario presets as JSON.


https://github.com/user-attachments/assets/ad6a8c36-8903-4e40-a701-469691e8d1fb

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
- **Wind is not modeled**: no flight ban above 36 km/h gusts, no effect on speed or battery.
- Lines are treated electrically independent: no power-flow or connectivity model, so a dead line only nudges its neighbours' risk.
- Sensors are a simple per-cell probabilistic model (one air pass or one landing per cell), not real imagery.
- Drones fly straight cross-country with no airspace, terrain or collision constraints.
- The simulation is open-ended.

## Hackathon scope
Everything was built during the hackathon except: this README update, the operator drawing functionality and 2 buttons on the simulation view as well as unnecessary files have been removed 
