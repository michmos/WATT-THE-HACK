"""The scripted demo scenario (scenarios/demo.json, default at startup) tells the demo story in 3 sim hours."""

from app.data import load_static
from app.main import load_default_config
from app.planner import SmartPlanner
from app.sim.engine import Engine


def test_demo_story_three_hours():
    eng = Engine(load_static(), load_default_config(), SmartPlanner)
    eng.set_inputs(eng.static.default_inputs)  # "Load defaults"
    eng.plan_start_positions()
    eng.control("start")
    eng.step_n(3 * 3600)
    assert eng.status == "paused"  # auto-paused when all faults were found; play on to see the confirmations
    eng.control("resume")
    eng.step_n(3 * 3600 - int(eng.t))
    ev = eng.events

    # every fault is found (a finding event of the right type on its cell)
    for f in eng.config.faults:
        cell = eng._cell_of(f.segment_id, f.position)
        assert any(e.type == "finding" and e.cell_id == cell and e.fault_type == f.fault_type for e in ev), f.fault_id
        assert any(e.type == "recommendation" and e.cell_id == cell and e.message.startswith("Send repair crew")
                   for e in ev), f.fault_id

    # the hidden dead line is found by a landing, and nobody gets stranded
    hidden = set(eng.config.dead_segments)
    assert any(e.type == "line_dead" and e.segment_id in hidden for e in ev)
    assert not any(e.type == "stranded" for e in ev)

    # report 04 (generator test) is cleared; report 02 (real fault inside) is not
    msgs = [e.message for e in ev if e.type == "recommendation"]
    assert any(m.startswith("Area of SYN_REPORT_04 cleared") for m in msgs)
    assert not any(m.startswith("Area of SYN_REPORT_02 cleared") for m in msgs)
    # no crew sent to a cell without a fault
    fault_cells = {eng._cell_of(f.segment_id, f.position) for f in eng.config.faults}
    assert all(e.cell_id in fault_cells for e in ev if e.type == "recommendation"
               and e.message.startswith("Send repair crew"))
