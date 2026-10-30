#!/usr/bin/env python3
"""Distribution of valid readings per hourly summary for heart rate, respiration and SpO2.

Reads the source files only; nothing is sent to DHIS2.

WHY THIS EXISTS
---------------
The Data Sufficiency status calls an hour Sufficient at three valid readings,
which is a deliberately low bar. Reporting only the share of hours above it
says little about how densely the devices actually report. This counts the
readings behind every hourly summary, after the same sentinel and finiteness
filters the importer applies, so the hours counted are exactly the hours
stored. The totals equal the live stage event counts (485,951 heart rate,
487,770 respiratory rate and 73,001 SpO2 hours on 30 October 2026).

Writes aggregate quantiles only to results/readings_per_hour_<date>.json.

USAGE
-----
    python3 "7. verification data script/readings_per_hour.py"
"""

import datetime as dt
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "2. wearable script"))
from common import aireadi  # noqa: E402

from hourly_aggregation_logic_final import (  # noqa: E402
    aggregate_simple, extract_heart_rate, extract_oxygen_saturation,
    extract_respiratory_rate,
)

STREAMS = {
    "heart_rate": ("heartrate_filepath", extract_heart_rate),
    "respiratory_rate": ("respiratory_rate_filepath", extract_respiratory_rate),
    "spo2": ("oxygen_saturation_filepath", extract_oxygen_saturation),
}
THRESHOLDS = (3, 30, 60)


def main():
    manifest = aireadi.load_manifest("wearable_activity_monitor")
    counts = {stream: [] for stream in STREAMS}
    for index, (pid, rows) in enumerate(sorted(manifest.items()), start=1):
        for stream, (column, extract) in STREAMS.items():
            readings = []
            for row in rows:
                path = aireadi.resolve("wearable_activity_monitor", row.get(column))
                if path:
                    readings.extend(extract(path))
            counts[stream].extend(h["count"] for h in aggregate_simple(readings).values())
        if index % 200 == 0:
            print(f"  {index}/{len(manifest)} participants", flush=True)

    out = {"run_date": dt.date.today().isoformat(),
           "script": "7. verification data script/readings_per_hour.py",
           "participants": len(manifest), "streams": {}}
    for stream, values in counts.items():
        a = np.array(values)
        out["streams"][stream] = {
            "hours": int(a.size),
            "median": float(np.median(a)),
            "q1": float(np.percentile(a, 25)),
            "q3": float(np.percentile(a, 75)),
            **{f"share_ge{t}_pct": round(100 * float((a >= t).mean()), 2) for t in THRESHOLDS},
        }
    print(json.dumps(out, indent=1))
    path = os.path.join(HERE, "results", f"readings_per_hour_{out['run_date']}.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"Results written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
