"""Sensor model. Knows the ground truth; turns it into noisy Observations.

- Air pass along a cell: each true fault in the cell is seen with p_detect_air; every fault type
  can also give a false positive with p_false_positive. Fault types with p_detect_air = 0 are
  landed-only (e.g. galloping: accelerometer).
- Landed: same with p_detect_landed, only for the cell the drone sits on.
- Live/dead: proposed 50 Hz magnetometer gives one air reading per segment per flight (correct with
  MAG_ACCURACY). Landing always gives the truth via the current sensor.
"""

import random

from app.models import Detection, Fault, Observation, SensorParams

MAG_ACCURACY = 0.9


def inspect(rng: random.Random, *, t: float, drone_id: str, cell_id: str, segment_id: str, mode: str,
            faults: list[Fault], sensors: dict[str, SensorParams], live: bool, read_live: bool) -> Observation:
    detections = []
    true_types = {f.fault_type for f in faults}
    for ftype, sp in sensors.items():
        pd = sp.p_detect_air if mode == "air" else sp.p_detect_landed
        if pd <= 0:
            continue  # this fault type cannot be sensed in this mode at all
        if ftype in true_types:
            if rng.random() < pd:
                lo, hi = (0.6, 0.95) if mode == "air" else (0.85, 0.99)
                detections.append(Detection(fault_type=ftype, confidence=round(rng.uniform(lo, hi), 2)))
        elif rng.random() < sp.p_false_positive:
            detections.append(Detection(fault_type=ftype, confidence=round(rng.uniform(0.3, 0.7), 2)))
    reads_live, acc = None, 1.0
    if mode == "landed":
        reads_live = live
    elif read_live:
        acc = MAG_ACCURACY
        reads_live = live if rng.random() < acc else not live
    return Observation(t=t, drone_id=drone_id, cell_id=cell_id, segment_id=segment_id, mode=mode,
                       detections=detections, reads_live=reads_live, live_accuracy=acc)
