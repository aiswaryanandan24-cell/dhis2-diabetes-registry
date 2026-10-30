#!/usr/bin/env python3
"""How often each reference-range status fires, CGM wear lengths, and a worked count.

READ-ONLY. GET requests only, through the tracker API with server-side
filters, so nothing depends on analytics tables.

  1. Distribution of Sensor Wear Duration (days) on the CGM Summary stage,
     for comparison with the 14 days the time-in-range consensus asks for.
  2. Events per status option on the heart rate, respiratory rate and SpO2
     stages. The statuses flag any reading outside a resting reference
     range, so their prevalence says whether they carry information.
  3. A worked programme-style count: glucose hours with time in range below
     70 percent, by recruitment site, with the time the queries took.

Writes counts only to results/flags_and_wear_<date>.json.

USAGE
-----
    python3 "7. verification data script/flags_and_wear_summary.py"
"""

import collections
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
STATUS_FIELDS = {
    "Wearable - Heart Rate": "HR Hourly Status",
    "Wearable - Respiratory Rate": "RR Hourly Status",
    "Wearable - SpO2": "SpO2 Hourly Status",
}


def main():
    session = dhis2.get_session(read_only=True)
    registry = M.load(session)

    def total(stage, **params):
        payload = dhis2.get_json(session, "tracker/events", {
            "program": M.PROGRAM_UID, "programStage": registry.stage(stage),
            "pageSize": 1, "totalPages": "true", "fields": "event", **params})
        return (payload.get("pager") or payload.get("page") or {}).get("total")

    out = {"run_date": dt.date.today().isoformat(),
           "script": "7. verification data script/flags_and_wear_summary.py"}

    wear_de = registry.data_element("Sensor Wear Duration (days)")
    events = dhis2.fetch_events(session, M.PROGRAM_UID, registry.stage("CGM Summary"),
                                fields="event,dataValues[dataElement,value]")
    days = [float(dv["value"]) for e in events for dv in e["dataValues"]
            if dv["dataElement"] == wear_de and dv.get("value")]
    out["cgm_wear_days"] = {"participants": len(days), "by_whole_days": dict(sorted(
        collections.Counter(int(round(d)) for d in days).items()))}

    out["status_prevalence"] = {}
    for stage, de_name in STATUS_FIELDS.items():
        de = registry.data_element(de_name)
        options = dhis2.get_json(session, f"dataElements/{de}",
                                 {"fields": "optionSet[options[code,name]]"})["optionSet"]["options"]
        counts = {o["name"]: total(stage, filter=f"{de}:eq:{o['code']}") for o in options}
        counts["all hours"] = total(stage)
        out["status_prevalence"][stage] = counts

    program = dhis2.get_json(session, f"programs/{M.PROGRAM_UID}",
                             {"fields": "organisationUnits[id,name]"})
    tir = M.GLUCOSE_FIELD_UIDS["tir_pct"]
    start = time.perf_counter()
    by_site = {ou["name"]: {
        "hours_tir_below_70": total("CGM - Glucose", orgUnit=ou["id"], filter=f"{tir}:lt:70"),
        "all_hours": total("CGM - Glucose", orgUnit=ou["id"])}
        for ou in program["organisationUnits"]}
    out["worked_example_tir_below_70_by_site"] = {
        "sites": by_site, "requests": 2 * len(by_site),
        "seconds": round(time.perf_counter() - start, 1)}

    print(json.dumps(out, indent=1))
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"flags_and_wear_{out['run_date']}.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"Results written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
