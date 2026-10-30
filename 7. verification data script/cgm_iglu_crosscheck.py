#!/usr/bin/env python3
"""Cross-check stored hourly glucose summaries against the iglu R package.

READ-ONLY against DHIS2. GET requests only.

WHY THIS EXISTS
---------------
stratified_sample_verification.py recomputes each hourly glucose summary with
aggregate_glucose, the function the importer used, so it can only show that
the registry holds what that function computed. A mistake in the function
itself, in the hour bucketing, the time-in-range denominator or the handling
of the sensor's High and Low markers, would be reproduced on both sides and
pass. This script takes that function out of the loop: it reads the raw
Dexcom export with its own parser, buckets readings by UTC hour with pandas,
computes the metrics with iglu (Chun, Fernandes and Gaynanova, 2024), and
compares the result with the values stored in DHIS2.

Definitions it has to reconcile
-------------------------------
* High and Low markers. iglu takes numbers only. For the range percentages a
  High reading becomes 401 and a Low reading 39, just outside the Dexcom G6
  reporting range of 40 to 400 mg/dL, so each still counts above 180 or
  below 70. Mean, minimum, maximum and standard deviation use the numeric
  readings alone, as the registry does.
* Standard deviation. iglu's sd_glu is R's sd(), the sample standard
  deviation (divisor n - 1). The registry stores the population standard
  deviation (divisor n). The stored value is compared with
  sd_glu * sqrt((n - 1) / n), and the size of the difference between the two
  definitions is reported as well.
* Counts. iglu has no count function; numeric, High and Low counts are
  computed here with pandas, independently of aggregate_glucose.

Setup
-----
Needs R with iglu. Without a Fortran compiler, install the prebuilt Ubuntu
binaries from Posit Package Manager:

    Rscript -e 'options(HTTPUserAgent = sprintf("R/%s R (%s)", getRversion(),
        paste(getRversion(), R.version["platform"], R.version["arch"],
        R.version["os"]))); install.packages("iglu",
        repos = "https://packagemanager.posit.co/cran/__linux__/jammy/latest")'

Participant-level readings pass through a temporary directory that is
deleted on exit. Only aggregate counts are written to results/.

USAGE
-----
    python3 "7. verification data script/cgm_iglu_crosscheck.py"
    python3 "7. verification data script/cgm_iglu_crosscheck.py" --participants 50
"""

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
BASE_SEED = 42
DEFAULT_PARTICIPANTS = 200
HIGH_STANDIN, LOW_STANDIN = 401, 39

# metric: (tolerance, comparison). Stored means and standard deviations are
# rounded to 2 decimals and percentages to 1, so the tolerance is half a unit
# in the last stored place plus floating-point slack.
METRICS = {
    "mean": 0.0051,
    "min": 0.0001,
    "max": 0.0001,
    "sd": 0.0051,
    "tir_pct": 0.0501,
    "tar_pct": 0.0501,
    "tbr_pct": 0.0501,
    "count": 0.0001,
    "high_count": 0.0001,
    "low_count": 0.0001,
}

R_SCRIPT = r"""
suppressMessages(library(iglu))
args <- commandArgs(trailingOnly = TRUE)
d <- read.csv(args[1], stringsAsFactors = FALSE)
d$time <- as.POSIXct(d$time, tz = "UTC", format = "%Y-%m-%dT%H:%M:%S")
num <- d[!is.na(d$gl_numeric), c("id", "time")]
num$gl <- d$gl_numeric[!is.na(d$gl_numeric)]
rng <- data.frame(id = d$id, time = d$time, gl = d$gl_range)

s <- summary_glu(num)
out <- data.frame(id = s$id, mean = s$Mean, min = s$Min., max = s$Max.)
out <- merge(out, setNames(as.data.frame(sd_glu(num)), c("id", "sd_sample")), all = TRUE)
tir <- in_range_percent(rng, target_ranges = list(c(70, 180)))
out <- merge(out, setNames(as.data.frame(tir), c("id", "tir_pct")), all = TRUE)
tar <- above_percent(rng, targets_above = 180)
out <- merge(out, setNames(as.data.frame(tar), c("id", "tar_pct")), all = TRUE)
tbr <- below_percent(rng, targets_below = 70)
out <- merge(out, setNames(as.data.frame(tbr), c("id", "tbr_pct")), all = TRUE)
write.csv(out, args[2], row.names = FALSE)
cat("iglu", as.character(packageVersion("iglu")), "\n")
"""


def seed():
    digest = hashlib.sha256(f"{BASE_SEED}:iglu".encode()).hexdigest()
    return int(digest[:8], 16)


def read_dexcom(path):
    """Raw readings as (UTC timestamp, value) with no filtering or aggregation."""
    with open(path) as fh:
        body = json.load(fh)["body"]["cgm"]
    rows = []
    for record in body:
        frame = record["effective_time_frame"]
        ts = frame.get("date_time") or frame["time_interval"]["start_date_time"]
        rows.append((ts, record["blood_glucose"]["value"]))
    frame = pd.DataFrame(rows, columns=["time", "raw"])
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


def hourly_readings(pid, frame):
    """One row per reading with its hour id and the two numeric encodings."""
    marker = frame["raw"].astype(str).str.strip()
    numeric = pd.to_numeric(frame["raw"], errors="coerce")
    numeric[~numeric.apply(lambda v: math.isfinite(v) if pd.notna(v) else False)] = float("nan")
    out = pd.DataFrame({
        "hour": frame["time"].dt.floor("h"),
        "time": frame["time"].dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "gl_numeric": numeric,
        "is_high": marker.eq("High"),
        "is_low": marker.eq("Low"),
    })
    out["gl_range"] = out["gl_numeric"]
    out.loc[out["is_high"], "gl_range"] = HIGH_STANDIN
    out.loc[out["is_low"], "gl_range"] = LOW_STANDIN
    # A reading that is neither a number nor a marker carries no glucose value.
    out = out[out["gl_range"].notna()].copy()
    out["id"] = pid + "|" + out["hour"].dt.strftime("%Y-%m-%dT%H")
    return out


def stored_hours(session, tei, stage_uid, field_uids):
    events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
    out = {}
    for event in events:
        hour = pd.Timestamp(event["occurredAt"]).tz_localize("UTC").strftime("%Y-%m-%dT%H")
        values = {dv["dataElement"]: dv.get("value") for dv in event.get("dataValues", [])}
        out[hour] = {name: values.get(uid) for name, uid in field_uids.items()}
    return out


def as_float(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--participants", type=int, default=DEFAULT_PARTICIPANTS)
    parser.add_argument("--rscript", default="Rscript")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session(read_only=True)
    registry = M.load(session)
    stage_uid = registry.stage("CGM - Glucose")
    field_uids = {k: M.GLUCOSE_FIELD_UIDS[k] for k in METRICS}

    entities = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": M.PROGRAM_UID, "fields": "trackedEntity,attributes[attribute,value]"},
        ("trackedEntities", "instances"),
    )
    tei_by_pid = {}
    for item in entities:
        for attr in item.get("attributes", []):
            if attr.get("attribute") == M.PERSON_ID_ATTR_UID and attr.get("value"):
                tei_by_pid[attr["value"]] = item["trackedEntity"]

    manifest = aireadi.load_manifest("wearable_blood_glucose")
    eligible = sorted(pid for pid in manifest if pid in tei_by_pid)
    sample = random.Random(seed()).sample(eligible, min(args.participants, len(eligible)))
    print(f"{len(eligible)} participants with glucose files, sampling {len(sample)}")

    readings, stored = [], {}
    for index, pid in enumerate(sample, start=1):
        frames = [read_dexcom(path) for row in manifest[pid]
                  if (path := aireadi.resolve("wearable_blood_glucose",
                                              row.get("glucose_filepath")))]
        if frames:
            readings.append(hourly_readings(pid, pd.concat(frames)))
        for hour, values in stored_hours(session, tei_by_pid[pid], stage_uid,
                                         field_uids).items():
            stored[f"{pid}|{hour}"] = values
        if index % 25 == 0:
            print(f"  read {index}/{len(sample)}")
    data = pd.concat(readings, ignore_index=True)

    counts = data.groupby("id").agg(
        count=("gl_numeric", "count"),
        high_count=("is_high", "sum"),
        low_count=("is_low", "sum"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        infile, outfile = os.path.join(tmp, "in.csv"), os.path.join(tmp, "out.csv")
        rfile = os.path.join(tmp, "iglu.R")
        data[["id", "time", "gl_numeric", "gl_range"]].to_csv(infile, index=False)
        with open(rfile, "w") as fh:
            fh.write(R_SCRIPT)
        run = subprocess.run([args.rscript, rfile, infile, outfile],
                             capture_output=True, text=True, check=False)
        if run.returncode != 0:
            sys.exit(f"iglu failed:\n{run.stderr[-2000:]}")
        iglu_version = run.stdout.split()[-1]
        computed = pd.read_csv(outfile).set_index("id").join(counts, how="outer")

    def population_sd(row):
        n, s = row["count"], row["sd_sample"]
        if pd.isna(s) or n < 2:
            return None
        return s * math.sqrt((n - 1) / n)

    computed["sd"] = computed.apply(population_sd, axis=1)

    tallies = {m: {"compared": 0, "agree": 0, "empty_as_expected": 0, "mismatch": 0}
               for m in METRICS}
    examples = {}
    sd_gap = []
    hours_source, hours_stored = set(computed.index), set(stored)
    for hour_id in sorted(hours_source & hours_stored):
        row, saved = computed.loc[hour_id], stored[hour_id]
        for metric, tol in METRICS.items():
            ours = as_float(saved.get(metric))
            theirs = row[metric]
            theirs = None if theirs is None or pd.isna(theirs) else float(theirs)
            tally = tallies[metric]
            tally["compared"] += 1
            if ours is None and theirs is None:
                tally["empty_as_expected"] += 1
            elif ours is not None and theirs is not None and abs(ours - theirs) <= tol:
                tally["agree"] += 1
            else:
                tally["mismatch"] += 1
                examples.setdefault(metric, (ours, theirs, int(row["count"])))
        ours_sd = as_float(saved.get("sd"))
        if ours_sd is not None and not pd.isna(row["sd_sample"]) and row["sd_sample"] > 0:
            sd_gap.append((row["sd_sample"] - ours_sd) / row["sd_sample"])

    for metric, tally in tallies.items():
        agreed = tally["agree"] + tally["empty_as_expected"]
        tally["agreement_pct"] = round(100 * agreed / tally["compared"], 4) if tally["compared"] else None
    sd_gap_series = pd.Series(sd_gap)
    result = {
        "run_date": dt.date.today().isoformat(),
        "script": "7. verification data script/cgm_iglu_crosscheck.py",
        "iglu_version": iglu_version,
        "participants_sampled": len(sample),
        "seed": seed(),
        "hours_in_source": len(hours_source),
        "hours_in_registry": len(hours_stored),
        "source_hours_with_no_event": len(hours_source - hours_stored),
        "events_with_no_source_hour": len(hours_stored - hours_source),
        "per_metric": tallies,
        "sd_definition": {
            "registry": "population standard deviation (divisor n)",
            "iglu": "sample standard deviation (divisor n - 1)",
            "relative_gap_median": round(float(sd_gap_series.median()), 4) if sd_gap else None,
            "relative_gap_iqr": [round(float(sd_gap_series.quantile(q)), 4) for q in (0.25, 0.75)]
            if sd_gap else None,
            "hours_where_definitions_differ_by_more_than_0.01": int(
                sum(1 for hour_id in hours_source & hours_stored
                    if as_float(stored[hour_id].get("sd")) is not None
                    and not pd.isna(computed.loc[hour_id, "sd_sample"])
                    and abs(computed.loc[hour_id, "sd_sample"]
                            - as_float(stored[hour_id].get("sd"))) > 0.01)),
        },
        "method": ("raw Dexcom readings parsed independently of the importer, bucketed by "
                   "UTC hour with pandas; mean, minimum, maximum and sample SD by iglu on "
                   "numeric readings; time in, above and below range by iglu with High as "
                   f"{HIGH_STANDIN} and Low as {LOW_STANDIN} mg/dL; counts by pandas; "
                   "stored SD compared with the iglu SD rescaled to the population "
                   "definition; an empty pair counts as agreement and is reported "
                   "separately"),
    }

    print(json.dumps({k: v for k, v in result.items() if k != "method"}, indent=1))
    if any(t["mismatch"] for t in tallies.values()):
        # Kinds only: the stored value, iglu's, and the hour's numeric count.
        print("first mismatch per metric (stored, iglu, numeric readings):", examples)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"cgm_iglu_crosscheck_{result['run_date']}.json")
    with open(path, "w") as fh:
        json.dump(result, fh, indent=1)
    print(f"Results written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
