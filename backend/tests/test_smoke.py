from fastapi.testclient import TestClient

from app.main import app, engine


client = TestClient(app)


def test_static_loads():
    s = client.get("/api/static").json()
    assert len(s["segments"]["features"]) == 64
    assert 380 <= len(s["cells"]["features"]) <= 450
    assert len(s["default_inputs"]["reports"]) == 6
    assert s["default_inputs"]["known_dead_segments"] == ["LINE_024"]


def reset_and_load_defaults():
    client.post("/api/sim/control", json={"action": "reset"})
    assert client.get("/api/state").json()["inputs"]["reports"] == []  # nothing until the operator loads reports
    client.put("/api/inputs", json=client.get("/api/static").json()["default_inputs"])  # "Load defaults"


def test_drones_move_after_start():
    reset_and_load_defaults()
    sps = client.post("/api/plan/start-positions").json()
    assert len(sps) == 5 and all(sp["segment_id"] != "LINE_024" for sp in sps)

    client.post("/api/sim/control", json={"action": "start", "speed": 60})
    before = client.get("/api/state").json()
    engine.step_n(300)  # 5 sim minutes, independent of wall time
    after = client.get("/api/state").json()

    assert after["status"] == "running" and after["t"] > before["t"]
    assert len(after["cells"]) == len(engine.static.cells["features"])
    moved = [a["position"] != b["position"] for a, b in zip(after["drones"], before["drones"])]
    assert all(moved)
    assert any(e["type"] == "takeoff" for e in after["events"])


def test_play_requires_reports():
    client.post("/api/sim/control", json={"action": "reset"})
    response = client.post("/api/sim/control", json={"action": "start"})
    assert response.status_code == 400
    assert client.get("/api/state").json()["status"] == "setup"
    client.put("/api/inputs", json=client.get("/api/static").json()["default_inputs"])
    assert client.post("/api/sim/control", json={"action": "start"}).json()["status"] == "running"


def test_sim_two_hours_finds_faults():
    reset_and_load_defaults()
    client.post("/api/sim/control", json={"action": "start", "speed": 600})
    engine.step_n(2 * 3600)
    s = client.get("/api/state").json()
    types = {e["type"] for e in s["events"]}
    assert {"takeoff", "landing", "finding"} <= types
    assert sum(c["inspected"] != "none" for c in s["cells"]) > 50
    assert any(c["top_fault"] for c in s["cells"])
    # report panel: every crew/confirm recommendation comes with its cell's observations (risk before -> after)
    obs_cells = {o["cell_id"] for o in s["observations"]}
    recs = [e for e in s["events"] if e["type"] == "recommendation" and e["cell_id"]]
    assert recs and all(e["cell_id"] in obs_cells for e in recs)
    assert all({"risk_before", "risk_after", "mode", "drone_id"} <= o.keys() for o in s["observations"])
