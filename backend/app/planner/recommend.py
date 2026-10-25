"""When to act: turn the planner's belief into recommendation messages.

Pure function over CellState / SegmentState; the caller wraps the results into Events and passes
`done` (keys already emitted) so every recommendation appears only once, and `confirmed`
((cell, fault type) pairs seen twice, or once by a confident landed sensor).
"""

from app.models import CellState, SegmentState

# The frontend classifies recommendations by message prefix: keep "Send repair crew", "Needs confirmation",
# "Area of <report_id> cleared"; anything else is shown as a ground check.

CREW = 0.8
CONFIRM = 0.4
CONFIRM_CONF = 0.8  # a landed detection at least this confident confirms a fault on its own


def report_name(report_id: str) -> str:
    return report_id.replace("SYN_REPORT_", "report ")


def recommendations(cells: list[CellState], segments: list[SegmentState], cell_km: dict[str, float],
                    report_cells: dict[str, list[str]], done: set[str],
                    confirmed: set[tuple[str, str]] = frozenset()) -> list[tuple[str, str | None, str | None, str]]:
    """Returns new (key, cell_id, segment_id, message) tuples and adds their keys to `done`."""
    out = []
    by_id = {c.cell_id: c for c in cells}

    def add(key, cell_id, seg, msg):
        if key not in done:
            done.add(key)
            out.append((key, cell_id, seg, msg))

    for c in cells:
        seg = c.cell_id.split("#")[0]
        where = f"{seg} km {cell_km.get(c.cell_id, 0):.1f}"
        if c.top_fault and c.risk > CREW and (c.cell_id, c.top_fault) in confirmed:
            add(f"crew:{c.cell_id}", c.cell_id, seg,
                f"Send repair crew to {where}: {c.top_fault} confirmed (risk {c.risk:.0%})")
        elif c.top_fault and c.risk >= CONFIRM:
            add(f"confirm:{c.cell_id}", c.cell_id, seg,
                f"Needs confirmation: possible {c.top_fault} at {where} (risk {c.risk:.0%}), second look scheduled")

    by_seg: dict[str, list[CellState]] = {}
    for c in cells:
        by_seg.setdefault(c.cell_id.split("#")[0], []).append(c)
    for s in segments:
        own = by_seg.get(s.segment_id, [])
        if s.p_live == 0 and own and all(c.inspected != "none" for c in own) and not any(c.top_fault for c in own):
            add(f"ground:{s.segment_id}", None, s.segment_id,
                f"{s.segment_id} is dead but drones saw no fault along it: inspect from ground / check protection")

    big = len(cells) / 2  # a report covering half the grid (weather alert) is never "cleared"
    for rid, ids in report_cells.items():
        own = [by_id[i] for i in ids if i in by_id]
        if own and len(own) < big and all(c.inspected != "none" and c.risk < CONFIRM for c in own):
            add(f"cleared:{rid}", None, None,
                f"Area of {rid} cleared ({report_name(rid)}): all {len(own)} cells inspected, no fault found")
    return out


def relevant_cells(done: set[str], report_cells: dict[str, list[str]]) -> set[str]:
    """Cells whose observations explain an emitted recommendation (keys in `done`): the cell itself
    (crew/confirm), every cell of the line (ground check) or of the report area (cleared)."""
    out: set[str] = set()
    for key in done:
        kind, _, ref = key.partition(":")
        if kind in ("crew", "confirm"):
            out.add(ref)
        elif kind == "cleared":
            out.update(report_cells.get(ref, []))
        elif kind == "ground":
            out.add(ref + "#")  # prefix: all cells of the segment
    return out


def observations_for(obs_log: list, done: set[str], report_cells: dict[str, list[str]]) -> list:
    """The CellObservations behind the emitted recommendations (for the operator's report panel)."""
    cells = relevant_cells(done, report_cells)
    prefixes = tuple(c for c in cells if c.endswith("#"))
    return [o for o in obs_log if o.cell_id in cells or (prefixes and o.cell_id.startswith(prefixes))]
