#!/usr/bin/env python3
"""Time dashboard-style queries through /api/analytics once analytics tables exist.

READ-ONLY. GET requests only.

WHY THIS EXISTS
---------------
cohort_query_gradable_cgm.py assembles its cohort through the transactional
tracker interface, two requests per participant, about 4,500 in all. That is
how a client without analytics tables has to work, and its 184 seconds say
little about how DHIS2 serves the queries behind its dashboards, program
indicators and event reports, which read the analytics tables instead. This
script times three such queries over the largest stages, each repeated so
that a cold first run can be told from the steady state:

  1. mean of the hourly glucose means, by site and year (519,617 events)
  2. number of glucose hours with less than 70 percent time in range, by site
  3. mean of the hourly heart rate means, by site and month (485,951 events)

Run it only after an analytics table generation has completed; before that
the analytics endpoints return nothing for tracker data.

Writes response times and row counts, no data values, to
results/analytics_query_timing_<date>.json.

USAGE
-----
    python3 "7. verification data script/analytics_query_timing.py"
"""

import datetime as dt
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
REPEATS = 3
YEARS = "2023;2024;2025"
MONTHS = ";".join(f"{y}{m:02d}" for y in (2023, 2024, 2025) for m in range(1, 13))


def site_uids(session):
    program = dhis2.get_json(session, f"programs/{M.PROGRAM_UID}",
                             {"fields": "organisationUnits[id]"})
    return ";".join(sorted(ou["id"] for ou in program["organisationUnits"]))


def timed(session, path, params):
    times, rows = [], None
    for _ in range(REPEATS):
        start = time.perf_counter()
        payload = dhis2.get_json(session, path, params)
        times.append(round(time.perf_counter() - start, 3))
        rows = len(payload.get("rows", []))
    return {"seconds": times, "median_seconds": statistics.median(times), "rows": rows}


def main():
    session = dhis2.get_session(read_only=True)
    registry = M.load(session)
    info = dhis2.get_json(session, "system/info")
    if str(info.get("lastAnalyticsTableSuccess", "")).startswith("1970"):
        sys.exit("Analytics tables have never been generated on this server.")

    sites = site_uids(session)
    cgm = registry.stage("CGM - Glucose")
    heart = registry.stage("Wearable - Heart Rate")
    path = f"analytics/events/aggregate/{M.PROGRAM_UID}"

    queries = {
        "glucose_mean_by_site_and_year": {
            "stage": cgm, "dimension": [f"ou:{sites}", f"pe:{YEARS}"],
            "value": M.GLUCOSE_FIELD_UIDS["mean"], "aggregationType": "AVERAGE",
        },
        "glucose_hours_below_70pct_tir_by_site": {
            "stage": cgm, "dimension": [f"ou:{sites}"],
            "filter": [f"pe:{YEARS}", f"{cgm}.{M.GLUCOSE_FIELD_UIDS['tir_pct']}:LT:70"],
        },
        "heart_rate_mean_by_site_and_month": {
            "stage": heart, "dimension": [f"ou:{sites}", f"pe:{MONTHS}"],
            "value": registry.data_element("Mean Value"), "aggregationType": "AVERAGE",
        },
    }

    results = {}
    for name, params in queries.items():
        results[name] = timed(session, path, params)
        print(f"{name}: {results[name]}")

    out = {
        "run_date": dt.date.today().isoformat(),
        "script": "7. verification data script/analytics_query_timing.py",
        "endpoint": "/api/analytics/events/aggregate",
        "last_analytics_table_success": info.get("lastAnalyticsTableSuccess"),
        "last_analytics_table_runtime": info.get("lastAnalyticsTableRuntime"),
        "repeats": REPEATS,
        "queries": results,
    }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path_out = os.path.join(RESULTS_DIR, f"analytics_query_timing_{out['run_date']}.json")
    with open(path_out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"Results written to {path_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
