#!/usr/bin/env python3
"""Verify imported DHIS2 values against the AI-READI source files.

READ-ONLY against DHIS2. GET requests only, using the auditor account.

WHAT THE AUDIT FOUND, and what changed. Read this before quoting any number.
---------------------------------------------------------------------------
H-10  The sampling was not stratified. sample_participants was called with the
      same seed and the same population for all seven groups, so
      random.Random(42).sample(...) returned the identical 500 participants
      seven times. Effective coverage was 500 of 2,280, not seven draws. Each
      group now derives its own seed.

      The proportional allocation the old docstring described in detail,
      plan_sample_sizes, was never called: main overrode it with a fixed 500.
      Allocation is now genuinely proportional to live event counts, with
      --fixed-n available if the counting queries are too slow.

H-11  The match rate was inflated by construction. An event whose hour had no
      matching source hour was skipped with `continue`, so it counted neither
      as checked nor as a discrepancy and simply left the denominator. A
      systematic hour misalignment would have produced checked=0, printed as
      "n/a" rather than as a failure. Unmatched records are now counted as
      discrepancies and reported as their own category.

H-12  CORE_REGISTRY_CHECKED = 82302 and CORE_REGISTRY_MATCHED = 82302 were
      hard-coded and folded into the headline "combined exact-match rate",
      asserting 100% for 82,302 values this script never checks. The prior
      exhaustive result is now reported separately, with its provenance, and
      never merged into a measured rate.

H-13  metadata_uids was imported but never committed, so this script could not
      run at all. It now lives in common/metadata_uids.py.

H-14  NOx was excluded on the stated grounds that "this participant's raw
      source CSV has no nox column at all". That is not what the data shows:
      all 40 environment files sampled carry a nox column, and person 7427 has
      203,183 usable values in 203,185 rows. NOx is now verified like every
      other column.

M-03  The hour lookup was a linear scan that re-parsed every source hour for
      every event. Source hours are now indexed once.

M-11  Only rows[0] of each participant's manifest was used, ignoring a second
      wearable period or re-scan.

Every mismatch is also written to stratified_sample_mismatches.json with its
group, stage, participant, hour, field and both values, and the run ends with
a breakdown by field and kind.

WHAT PEER REVIEW FOUND (October 2026), and what changed
-------------------------------------------------------
Empty pairs. In the 12 October 2026 run all 200 mismatches were statistics
      undefined on both sides: a standard deviation of a one-reading hour, or
      the mean/min/max of a glucose hour containing only High/Low markers. An
      empty pair now counts as agreement only where a rule applied to the
      source says the value must be empty (glucose_undefined, sd_undefined,
      or a source record that does not carry the field), and is reported in
      its own column. Anywhere else two blanks are still a mismatch, because
      a parser returning None for a populated field produces exactly that.

Thin strata. Proportional allocation gave electrocardiograms one draw and
      diagnoses four, and the ECG and OCTA groups compared event counts only.
      The four per-record stages are now verified exhaustively, every event
      and every mapped field, against values read from the source by code
      written separately from the importers. The 2,000-participant sample is
      allocated across the three hourly groups only, with a floor of
      MIN_PER_GROUP per group.

Coverage. Activity, Calories and Sleep were never compared, and on the other
      hourly stages only the summary statistics were: 4 of 13 wearable fields,
      10 of 16 glucose fields, 5 of 7 environmental fields. Every hourly stage
      and every field it stores is now compared (thresholds, statuses, lists
      of flagged times, hour start and end), and --exhaustive checks every
      participant rather than a sample. A second event for an hour already
      matched now counts as an event with no source record, and sleep
      segments the export repeats are paired one to one.

Diagnosis pairing. Events were matched to the first source row with the same
      code, so a code recurring on two dates was compared against the wrong
      row. Both sides are now sorted within a code and paired in order.

What this does not show. The hourly groups recompute the source with the
same aggregation functions the importers used, so they establish that the
registry holds what that code computed. They do not test the aggregation
itself. cgm_iglu_crosscheck.py does that for glucose against an
independent library.

USAGE
-----
    python3 "7. verification data script/stratified_sample_verification.py" --fresh
    python3 "7. verification data script/stratified_sample_verification.py" --fixed-n 200
    python3 "7. verification data script/stratified_sample_verification.py" --groups ecg --record-limit 20
"""

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "2. wearable script"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "3. ecg"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "4. envt"))

from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.timeutil import hour_end, hour_key, parse_instant, time_only  # noqa: E402

from hourly_aggregation_logic_final import (  # noqa: E402
    aggregate_activity, aggregate_calories, aggregate_glucose, aggregate_simple,
    extract_heart_rate, extract_oxygen_saturation, extract_respiratory_rate,
    extract_sleep_segments, extract_stress,
)
from threshold_step1_metadata import (  # noqa: E402
    FIELD_NAMES as THRESHOLD_FIELD_NAMES, HR_THRESHOLDS, RR_THRESHOLDS,
)
from threshold_step2_backfill import compute_hr_rr, compute_spo2  # noqa: E402
from glucose_recount_step2_backfill import (  # noqa: E402
    classify as classify_glucose, extract_raw as extract_raw_glucose,
)
from ecg_step1_metadata import (  # noqa: E402
    HR_RANGE, PR_RANGE, QRS_STATUS_SET, QRS_WIDENED_AT, RANGE_STATUS_SET,
)
from env_aggregation_logic_v2 import (  # noqa: E402
    RELEVANT_COLUMNS, THRESHOLDS as ENV_THRESHOLDS, aggregate_env_column, read_env_csv,
)

CHECKPOINT_FILE = "stratified_sample_checkpoint.json"
MISMATCH_FILE = "stratified_sample_mismatches.json"
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
BASE_SEED = 42
TOTAL_SAMPLE_TARGET = 2000
MIN_PER_GROUP = 50

HOURLY_GROUPS = ("wearable", "glucose", "environmental")
RECORD_GROUPS = ("diagnosis", "retinal_photography", "retinal_octa", "ecg")

# The prior exhaustive check of the core registry. Reported separately and
# never merged into a measured rate. See H-12.
PRIOR_CORE_REGISTRY = {
    "values": 82302,
    "discrepancies": 0,
    "method": "exhaustive, all values, established in an earlier project phase",
    "verified_by_this_script": False,
}

# stream: (manifest column, extractor, stage, threshold metric or None)
CONTINUOUS_WEARABLE = {
    "heart_rate": ("heartrate_filepath", extract_heart_rate, "Wearable - Heart Rate", "HR"),
    "respiratory_rate": ("respiratory_rate_filepath", extract_respiratory_rate,
                         "Wearable - Respiratory Rate", "RR"),
    "spo2": ("oxygen_saturation_filepath", extract_oxygen_saturation, "Wearable - SpO2",
             "SPO2"),
    "stress": ("stress_level_filepath", extract_stress, "Wearable - Stress", None),
}
# stage: (manifest column, aggregator, sum element, count element)
SEGMENT_WEARABLE = {
    "Wearable - Activity": ("physical_activity_filepath", aggregate_activity,
                            "Steps Sum", "Steps Reading Count"),
    "Wearable - Calories": ("active_calories_filepath", aggregate_calories,
                            "Calories Sum", "Calories Reading Count"),
}
SLEEP_STAGE = "Wearable - Sleep"
WEARABLE_STAGES = ([s for _, _, s, _ in CONTINUOUS_WEARABLE.values()]
                   + list(SEGMENT_WEARABLE) + [SLEEP_STAGE])

# Lists of flagged-reading times. Empty by rule when no reading of the hour
# falls in that class.
TIMESTAMP_FIELDS = {"low_ts", "high_ts", "mild_low_ts", "marked_low_ts",
                    "above_ts", "below_ts"}
HOUR_FIELDS = {"hour_start", "hour_end"}

RETINAL_PHOTOGRAPHY_COLUMNS = [
    "manufacturer", "manufacturers_model_name", "laterality", "anatomic_region",
    "imaging", "height", "width", "color_channel_dimension", "filepath",
]

# RETINAL_OCTA_FIELD_UIDS key: manifest column. The flow cube SOP Instance UID
# is the match key and so is not compared; the en-face fields depend on which
# slot holds the superficial layer and are handled in verify_retinal_octa.
RETINAL_OCTA_COLUMNS = {
    "manufacturer": "manufacturer",
    "manufacturers_model_name": "manufacturers_model_name",
    "laterality": "laterality",
    "anatomic_region": "anatomic_region",
    "imaging": "imaging",
    "flow_cube_height": "flow_cube_height",
    "flow_cube_width": "flow_cube_width",
    "flow_cube_number_of_frames": "flow_cube_number_of_frames",
    "flow_cube_file_path": "flow_cube_file_path",
    "segmentation_file_path": "associated_segmentation_file_path",
    "segmentation_sop_instance_uid": "associated_segmentation_sop_instance_uid",
    "segmentation_type": "associated_segmentation_type",
}
SUPERFICIAL_LAYER = "superficial"
NOT_REPORTED = "Not reported"

# ECG data element: WFDB header comment key, for the fields copied verbatim
# (text) or as a number. Status, date and signal fields are derived below.
ECG_TEXT_FIELDS = {
    "ECG Participant Position": "participant_position",
    "ECG Machine Interpretation Status": "interpretation_comment_1",
    "ECG Machine Interpretation Summary": "interpretation_comment_2",
    "ECG Finding 1": "comment_1_key",
    "ECG Finding 1 Detail": "comment_1_val",
    "ECG Finding 2": "comment_2_key",
    "ECG Finding 2 Detail": "comment_2_val",
    "ECG Finding 3": "comment_3_key",
    "ECG Finding 3 Detail": "comment_3_val",
    "ECG Device": "device_model",
}
ECG_NUMBER_FIELDS = {
    "ECG Heart Rate (bpm)": "Rate",
    "ECG PR Interval (ms)": "PR",
    "ECG QRS Duration (ms)": "QRSD",
    "ECG QT Interval (ms)": "QT",
    "ECG QTc Interval (ms)": "QTc",
    "ECG P Axis (deg)": "P",
    "ECG QRS Axis (deg)": "QRS",
    "ECG T Axis (deg)": "T",
}


class Tally:
    """Match counts that distinguish a mismatch from a record never compared.

    H-11: a source hour with no event, or an event with no source hour, is a
    real finding. It is counted here rather than dropped.
    """

    def __init__(self, group=None):
        self.group = group
        self.matched = 0
        self.empty_as_expected = 0
        self.empty_by_field = {}
        self.mismatched = 0
        self.unmatched_events = 0
        self.unmatched_source = 0
        self.participants_checked = 0
        # Humidity lists stored in full before the 20-entry cap; see compare_keyed.
        self.legacy_uncapped = 0
        # Source records repeated verbatim in the export (sleep segments).
        self.source_repeats = 0
        # Per-stage counts and the fields seen, for the coverage table.
        self.by_stage = {}
        # Every mismatch, with enough context to see why. Written to
        # MISMATCH_FILE at the end of the run so that a discrepancy can be
        # traced to its cause without re-running the aggregation.
        self.mismatches = []

    @property
    def checked(self):
        return self.matched + self.empty_as_expected + self.mismatched

    @property
    def discrepancies(self):
        return self.mismatched + self.unmatched_events + self.unmatched_source

    @property
    def total_compared(self):
        return self.checked + self.unmatched_events + self.unmatched_source

    def _stage(self, stage):
        return self.by_stage.setdefault(stage or self.group, {
            "compared": 0, "matched": 0, "empty_as_expected": 0, "mismatched": 0,
            "no_source": 0, "no_event": 0, "fields": set(),
        })

    def note_unmatched(self, stage, events=0, source=0):
        """Count events with no source record, and source records with no event."""
        self.unmatched_events += events
        self.unmatched_source += source
        entry = self._stage(stage)
        entry["no_source"] += events
        entry["no_event"] += source

    def compare(self, dhis2_value, source_value, expect_empty=False, **context):
        """Count one value pair.

        expect_empty comes from a rule applied to the source, never from the
        pair itself: a standard deviation over fewer than two readings, a
        mean of an hour with no numeric reading, a field the source record
        does not carry. An empty pair is agreement only then. The per-field
        counts are kept so that a field which is empty everywhere, the mark
        of a parser reading the wrong key, stands out in the summary.
        """
        entry = self._stage(context.get("stage"))
        entry["compared"] += 1
        entry["fields"].add(context.get("field") or "-")
        if expect_empty and is_empty(dhis2_value) and is_empty(source_value):
            self.empty_as_expected += 1
            entry["empty_as_expected"] += 1
            field = context.get("field") or "-"
            self.empty_by_field[field] = self.empty_by_field.get(field, 0) + 1
        elif close_enough(dhis2_value, source_value):
            self.matched += 1
            entry["matched"] += 1
        else:
            self.mismatched += 1
            entry["mismatched"] += 1
            self.mismatches.append({
                "group": self.group, "dhis2": dhis2_value,
                "source": None if is_empty(source_value) else str(source_value),
                "expect_empty": expect_empty,
                **context,
            })

    def as_dict(self):
        agreed = self.matched + self.empty_as_expected
        rate = (100 * agreed / self.total_compared) if self.total_compared else None
        return {
            "values_compared": self.total_compared,
            "matched": self.matched,
            "empty_as_expected": self.empty_as_expected,
            "empty_as_expected_by_field": dict(sorted(self.empty_by_field.items())),
            "mismatched": self.mismatched,
            "events_with_no_source_hour": self.unmatched_events,
            "source_hours_with_no_event": self.unmatched_source,
            "discrepancies": self.discrepancies,
            "agreement_pct": round(rate, 4) if rate is not None else None,
            "flagged_time_lists_stored_uncapped": self.legacy_uncapped,
            "source_records_repeated": self.source_repeats,
            "by_stage": {
                stage: {**{k: v for k, v in entry.items() if k != "fields"},
                        "fields_compared": sorted(entry["fields"])}
                for stage, entry in sorted(self.by_stage.items())
            },
        }


def is_empty(value):
    """None, NaN or blank text, the forms a missing value takes on either side.

    The text "nan" is deliberately not empty: a registry holding it is the
    defect the finiteness guard exists to prevent, and must show as a
    mismatch.
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return isinstance(value, str) and not value.strip()


def close_enough(a, b, tol=0.01):
    """Numeric comparison with a tolerance, falling back to exact strings.

    A missing value on either side is never a match. Two blanks are not
    evidence of correctness; Tally.compare decides when they are expected.
    """
    if is_empty(a) or is_empty(b):
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def glucose_undefined(field, stats):
    """True where a glucose statistic has no value by definition.

    count is the number of numeric readings in the hour; the sensor's High
    and Low markers are not numbers. A standard deviation needs two numeric
    readings, and a mean, minimum or maximum needs one.
    """
    numeric = stats.get("count") or 0
    if field == "sd":
        return numeric < 2
    if field in ("mean", "min", "max"):
        return numeric == 0
    return False


def sd_undefined(field, stats):
    """True for the standard deviation of an hour with fewer than two readings."""
    return field == "sd" and (stats.get("count") or 0) < 2


def group_seed(group):
    """A distinct, reproducible seed per group. See H-10."""
    digest = hashlib.sha256(f"{BASE_SEED}:{group}".encode()).hexdigest()
    return int(digest[:8], 16)


def sample_participants(group, n, participants):
    rng = random.Random(group_seed(group))
    valid = [(tei, pid) for tei, pid in participants if pid]
    return rng.sample(valid, min(n, len(valid)))


def get_participants(session):
    items = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": M.PROGRAM_UID, "fields": "trackedEntity,attributes[attribute,value]"},
        ("trackedEntities", "instances"),
    )
    out = []
    for item in items:
        pid = next(
            (a.get("value") for a in item.get("attributes", [])
             if a.get("attribute") == M.PERSON_ID_ATTR_UID),
            None,
        )
        out.append((item["trackedEntity"], pid))
    return out


def event_value(event, de_uid):
    for dv in event.get("dataValues", []):
        if dv.get("dataElement") == de_uid:
            return dv.get("value")
    return None


def hour_text(value):
    """The UTC minute 'YYYY-MM-DD HH:MM' held by the Hour Start and Hour End fields."""
    return parse_instant(value).strftime("%Y-%m-%d %H:%M")


def stored_hour_text(value):
    """A stored hour field without the ' UTC' label that later loads append.

    Events loaded before the label existed hold the same minute without it,
    so both forms are compared as the same UTC minute.
    """
    if is_empty(value):
        return value
    text = str(value).strip()
    return text[:-4].strip() if text.endswith(" UTC") else text


def instant_key(value):
    """Key for an event that is not hourly, such as a sleep segment."""
    return parse_instant(value).isoformat()


def compare_keyed(tally, events, expected, field_uids, event_key, stage, pid,
                  undefined=None, order_fields=()):
    """Compare every stored field of one stage's events with the source.

    expected maps a key (an hour, or a segment's start instant) to the value
    of every field the importers write for it, or to a list of them where the
    source itself repeats a record. event_key gives the key of a stored event.
    Events and source records sharing a key are sorted on order_fields and
    paired one to one; an event left over is counted as having no source
    record, so a duplicate cannot hide behind its twin, and a source record
    left over as having no event. An empty pair is agreement only where
    undefined(field, stats) says the statistic has no value, or for a list of
    flagged times whose class holds no reading. See Tally.compare.
    """
    def wanted(key):
        value = expected.get(key)
        return value if isinstance(value, list) else ([value] if value is not None else [])

    by_key = {}
    for event in events:
        occurred = event.get("occurredAt")
        by_key.setdefault(event_key(occurred) if occurred else None, []).append(event)

    no_source = no_event = 0
    for key, stored_events in by_key.items():
        sources = wanted(key)
        stored_events = sorted(stored_events, key=lambda e: tuple(
            str(event_value(e, field_uids[f]) or "") for f in order_fields))
        sources = sorted(sources, key=lambda s: tuple(str(s.get(f) or "") for f in order_fields))
        no_source += max(0, len(stored_events) - len(sources))
        no_event += max(0, len(sources) - len(stored_events))
        for event, stats in zip(stored_events, sources):
            occurred = event.get("occurredAt")
            for field, de_uid in field_uids.items():
                stored = event_value(event, de_uid)
                if field in HOUR_FIELDS:
                    stored = stored_hour_text(stored)
                value = stats.get(field)
                # Humidity hours loaded before the 20-entry cap hold the full
                # list of flagged times; that list is the source too.
                if (field in TIMESTAMP_FIELDS and not close_enough(stored, value)
                        and not is_empty(stored) and stored == stats.get(field + "_full")):
                    value = stored
                    tally.legacy_uncapped += 1
                expect_empty = ((field in TIMESTAMP_FIELDS and is_empty(value))
                                or bool(undefined and undefined(field, stats)))
                tally.compare(stored, value, expect_empty=expect_empty, stage=stage,
                              pid=pid, hour=occurred, field=field,
                              source_count=stats.get("count"))
    no_event += sum(len(wanted(k)) for k in expected if k not in by_key)
    tally.note_unmatched(stage, events=no_source, source=no_event)


# ---------------------------------------------------------------------------
# Per-domain verification
# ---------------------------------------------------------------------------

def hourly_participants(group, n, participants, exhaustive):
    return every_participant(participants) if exhaustive else sample_participants(
        group, n, participants)


def resolve_one(modality, rows, column):
    """The single source file for a column; no participant has two periods."""
    for row in rows:
        path = aireadi.resolve(modality, row.get(column))
        if path:
            return path
    return None


def hour_fields(registry):
    return {"hour_start": registry.data_element("Hour Start"),
            "hour_end": registry.data_element("Hour End")}


def with_hours(stats, hour):
    return dict(stats, hour_start=hour_text(hour), hour_end=hour_text(hour_end(hour)))


def expected_continuous(readings, metric):
    """Every stored field of a continuous stream: the summary, hours and thresholds."""
    by_hour = {}
    for ts, value in readings:
        by_hour.setdefault(hour_key(ts), []).append((ts, value))
    expected = {}
    for hour, stats in aggregate_simple(readings).items():
        key = hour_key(hour)
        entry = with_hours(stats, hour)
        if metric == "HR":
            entry.update(compute_hr_rr(by_hour[key], *HR_THRESHOLDS))
        elif metric == "RR":
            entry.update(compute_hr_rr(by_hour[key], *RR_THRESHOLDS))
        elif metric == "SPO2":
            entry.update(compute_spo2(by_hour[key]))
        expected[key] = entry
    return expected


def expected_sleep(path):
    """Every stored field of each sleep segment, keyed by its start instant."""
    expected = {}
    for start, sleep_stage, minutes in extract_sleep_segments(path):
        begin = parse_instant(start)
        expected.setdefault(instant_key(begin), []).append({
            "stage": sleep_stage, "duration_minutes": minutes,
            "hour_start": hour_text(begin),
            "hour_end": hour_text(begin + dt.timedelta(minutes=minutes)),
        })
    return expected


def verify_wearable(session, registry, participants, n=None, exhaustive=False):
    """All seven wearable stages, every field each stage stores."""
    manifest = aireadi.load_manifest("wearable_activity_monitor")
    hours = hour_fields(registry)
    tally = Tally("wearable")

    continuous_fields = {}
    for _, (_, _, stage_name, metric) in CONTINUOUS_WEARABLE.items():
        fields = dict(registry.wearable_shared_uids(), **hours)
        if metric:
            fields.update({k: registry.data_element(name)
                           for k, name in THRESHOLD_FIELD_NAMES[metric].items()})
        continuous_fields[stage_name] = fields
    segment_fields = {
        stage_name: dict({"sum": registry.data_element(sum_name),
                          "count": registry.data_element(count_name)}, **hours)
        for stage_name, (_, _, sum_name, count_name) in SEGMENT_WEARABLE.items()
    }
    sleep_fields = dict({"stage": registry.data_element("Sleep Stage"),
                         "duration_minutes": registry.data_element(
                             "Sleep Segment Duration Minutes")}, **hours)

    for tei, pid in hourly_participants("wearable", n, participants, exhaustive):
        rows = manifest.get(pid, [])
        checked = False
        for _, (column, extractor, stage_name, metric) in CONTINUOUS_WEARABLE.items():
            path = resolve_one("wearable_activity_monitor", rows, column)
            expected = expected_continuous(extractor(path), metric) if path else {}
            events = dhis2.fetch_events(session, M.PROGRAM_UID, registry.stage(stage_name), tei)
            checked |= bool(expected or events)
            compare_keyed(tally, events, expected, continuous_fields[stage_name],
                          hour_key, stage_name, pid, undefined=sd_undefined)
        for stage_name, (column, aggregate, _, _) in SEGMENT_WEARABLE.items():
            path = resolve_one("wearable_activity_monitor", rows, column)
            expected = ({hour_key(h): with_hours(s, h) for h, s in aggregate(path).items()}
                        if path else {})
            events = dhis2.fetch_events(session, M.PROGRAM_UID, registry.stage(stage_name), tei)
            checked |= bool(expected or events)
            compare_keyed(tally, events, expected, segment_fields[stage_name],
                          hour_key, stage_name, pid)
        path = resolve_one("wearable_activity_monitor", rows, "sleep_filepath")
        expected = expected_sleep(path) if path else {}
        # The Garmin export repeats some segments verbatim; the importer
        # loaded each repeat, so they are paired one to one and counted.
        tally.source_repeats += sum(len(v) - 1 for v in expected.values())
        events = dhis2.fetch_events(session, M.PROGRAM_UID, registry.stage(SLEEP_STAGE), tei)
        checked |= bool(expected or events)
        compare_keyed(tally, events, expected, sleep_fields, instant_key, SLEEP_STAGE, pid,
                      order_fields=("stage", "duration_minutes"))
        tally.participants_checked += checked
    return tally


def expected_glucose(path):
    """Every stored glucose field: statistics, hours and the clinical-range lists."""
    raw_by_hour = {}
    for ts, value in extract_raw_glucose(path):
        raw_by_hour.setdefault(hour_key(ts), []).append((ts, value))
    expected = {}
    for hour, stats in aggregate_glucose(path).items():
        key = hour_key(hour)
        above, below = classify_glucose(raw_by_hour.get(key, []))
        expected[key] = dict(
            with_hours(stats, hour),
            above_ts=", ".join(time_only(t) for t in sorted(above)) or None,
            below_ts=", ".join(time_only(t) for t in sorted(below)) or None,
            above_count=len(above), below_count=len(below),
        )
    return expected


def verify_glucose(session, registry, participants, n=None, exhaustive=False):
    """The CGM glucose stage, all 16 fields."""
    manifest = aireadi.load_manifest("wearable_blood_glucose")
    field_uids = dict(M.GLUCOSE_FIELD_UIDS, **hour_fields(registry))
    stage_uid = registry.stage("CGM - Glucose")
    tally = Tally("glucose")

    for tei, pid in hourly_participants("glucose", n, participants, exhaustive):
        path = resolve_one("wearable_blood_glucose", manifest.get(pid, []), "glucose_filepath")
        expected = expected_glucose(path) if path else {}
        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
        tally.participants_checked += bool(expected or events)
        compare_keyed(tally, events, expected, field_uids, hour_key, "CGM - Glucose",
                      pid, undefined=glucose_undefined)
    return tally


HUMIDITY_LIST_FIELDS = {
    "above_ts": ("Humidity Above Comfort Range (>50%) - Timestamps", "above"),
    "below_ts": ("Humidity Below Comfort Range (<30%) - Timestamps", "below"),
}


def expected_environment(rows, column):
    """Every stored field of one channel, with the full flagged-time lists too."""
    expected = {}
    thresholds = ENV_THRESHOLDS.get(column, {})
    for hour, stats in aggregate_env_column(rows, column).items():
        expected[hour_key(hour)] = with_hours(stats, hour)
    if thresholds:
        flagged = {}
        for row in rows:
            if column not in row:
                continue
            value = row[column]
            for side, limit in thresholds.items():
                if (side == "above" and value > limit) or (side == "below" and value < limit):
                    flagged.setdefault((hour_key(row["ts"]), side), []).append(row["ts"])
        for (key, side), times in flagged.items():
            expected[key][f"{side}_ts_full"] = ", ".join(time_only(t) for t in sorted(times))
    return expected


def verify_environmental(session, registry, participants, n=None, exhaustive=False):
    """All eight channels, every field each stage stores."""
    manifest = aireadi.load_manifest("environment")
    base = registry.env_field_uids()
    humidity = dict(base, **M.ENV_HUMIDITY_EXTRA_UIDS)
    for key, (name, _) in HUMIDITY_LIST_FIELDS.items():
        humidity[key] = registry.data_element(name)
    tally = Tally("environmental")

    for tei, pid in hourly_participants("environmental", n, participants, exhaustive):
        rows = []
        path = resolve_one("environment", manifest.get(pid, []), "env_sensor_filepath")
        if path:
            rows = read_env_csv(path)
        checked = False
        # H-14: every column including nox. A participant whose nox column is
        # all nan simply produces no buckets, which read_env_csv handles.
        for column in RELEVANT_COLUMNS:
            expected = expected_environment(rows, column) if rows else {}
            events = dhis2.fetch_events(
                session, M.PROGRAM_UID, registry.environment_stage(column), tei
            )
            checked |= bool(expected or events)
            compare_keyed(tally, events, expected, humidity if column == "hum" else base,
                          hour_key, column, pid, undefined=sd_undefined)
        tally.participants_checked += checked
    return tally


def every_participant(participants, limit=None):
    """The whole registry, for the per-record groups that are not sampled."""
    valid = sorted((tei, pid) for tei, pid in participants if pid)
    return valid[:limit] if limit else valid


def count_source_outside_registry(tally, by_person, participants, limit=None):
    """Source records whose participant has no tracked entity have no event.

    Skipped under --record-limit, where most of the registry is left out on
    purpose and the count would mean nothing.
    """
    if limit:
        return
    registered = {pid for _, pid in participants if pid}
    tally.unmatched_source += sum(
        len(rows) for pid, rows in by_person.items() if pid not in registered
    )


def preview_state(value):
    """A file resource reference reduced to present or absent."""
    return None if is_empty(value) else "present"


def match_by_key(tally, events, key_de, rows, row_key):
    """Pair events with source rows on an identifier; count both leftovers.

    Returns [(event, row)]. A key held by several events or rows pairs as
    many as both sides have, and the surplus on either side is a finding.
    """
    by_key = {}
    for event in events:
        by_key.setdefault(event_value(event, key_de), []).append(event)
    pairs = []
    for row in rows:
        candidates = by_key.get(row_key(row))
        if candidates:
            pairs.append((candidates.pop(), row))
        else:
            # H-11: a source record with no event is a real gap.
            tally.unmatched_source += 1
    tally.unmatched_events += sum(len(left) for left in by_key.values())
    return pairs


def verify_diagnosis(session, registry, participants, limit=None):
    """Every diagnosis event against every condition row.

    Events are matched to rows by condition code. Where a code recurs, both
    sides are sorted by date and label and paired in order, so a repeated
    code is never compared against another row's date. The code itself is
    checked by the matching: a wrong code leaves an unmatched event and an
    unmatched row.
    """
    conditions = pd.read_csv(aireadi.clinical_file("condition_occurrence.csv"),
                             dtype=str, keep_default_na=False)
    by_person = {}
    for row in conditions.to_dict("records"):
        code, _, label = row["condition_source_value"].partition(",")
        by_person.setdefault(row["person_id"], []).append(
            (code.strip(), row["condition_start_date"][:10], label.strip())
        )

    stage_uid = registry.stage("Diagnosis History")
    code_de = registry.data_element("Diagnosis Condition Code")
    label_de = registry.data_element("Diagnosis Condition Label")
    date_de = registry.data_element("Diagnosis Date")
    tally = Tally("diagnosis")

    def by_code(triples):
        grouped = {}
        for code, date, label in triples:
            grouped.setdefault(code, []).append((date or "", label or ""))
        return {code: sorted(pairs) for code, pairs in grouped.items()}

    for tei, pid in every_participant(participants, limit):
        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
        source = by_code(by_person.get(pid, []))
        stored = by_code(
            (event_value(e, code_de), event_value(e, date_de), event_value(e, label_de))
            for e in events
        )
        if source or stored:
            tally.participants_checked += 1
        for code in set(source) | set(stored):
            src, reg = source.get(code, []), stored.get(code, [])
            for (s_date, s_label), (r_date, r_label) in zip(src, reg):
                tally.compare(r_date, s_date, pid=pid, field="date", code=code)
                tally.compare(r_label, s_label, expect_empty=is_empty(s_label),
                              pid=pid, field="label", code=code)
            tally.unmatched_source += max(0, len(src) - len(reg))
            tally.unmatched_events += max(0, len(reg) - len(src))
    count_source_outside_registry(tally, by_person, participants, limit)
    return tally


def manifest_rows(modality):
    frame = pd.read_csv(aireadi.manifest_path(modality), sep="\t")
    frame["person_id"] = frame["person_id"].astype(str)
    by_person = {}
    for row in frame.to_dict("records"):
        by_person.setdefault(row["person_id"], []).append(row)
    return by_person


def verify_retinal_photography(session, registry, participants, limit=None):
    """Every photograph event: nine manifest fields and the preview."""
    by_person = manifest_rows("retinal_photography")
    stage_uid = registry.stage("Retinal Photography")
    fields = M.RETINAL_PHOTOGRAPHY_FIELD_UIDS
    tally = Tally("retinal_photography")

    for tei, pid in every_participant(participants, limit):
        rows = by_person.get(pid, [])
        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
        if rows or events:
            tally.participants_checked += 1
        pairs = match_by_key(tally, events, fields["sop_instance_uid"], rows,
                             lambda r: r.get("sop_instance_uid"))
        for event, row in pairs:
            sop = row.get("sop_instance_uid")
            for column in RETINAL_PHOTOGRAPHY_COLUMNS:
                tally.compare(event_value(event, fields[column]), row.get(column),
                              expect_empty=is_empty(row.get(column)),
                              pid=pid, field=column, sop=sop)
            tally.compare(preview_state(event_value(event, fields["preview"])), "present",
                          pid=pid, field="preview", sop=sop)
    count_source_outside_registry(tally, by_person, participants, limit)
    return tally


def superficial_enface_slot(row):
    """The en-face slot holding the superficial vascular plexus, else slot 1."""
    for slot in (1, 2, 3, 4):
        label = row.get(f"associated_enface_{slot}_ophthalmic_image_type")
        if isinstance(label, str) and SUPERFICIAL_LAYER in label.lower():
            return slot
    return 1


def verify_retinal_octa(session, registry, participants, limit=None):
    """Every OCTA event: twelve manifest fields, the en-face layer, and the preview."""
    by_person = manifest_rows("retinal_octa")
    stage_uid = registry.stage("Retinal OCTA")
    fields = dict(M.RETINAL_OCTA_FIELD_UIDS)
    fields["enface_file_path"] = registry.maybe_data_element(
        "Retinal OCTA - En-face DICOM File Path"
    )
    tally = Tally("retinal_octa")

    for tei, pid in every_participant(participants, limit):
        rows = by_person.get(pid, [])
        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
        if rows or events:
            tally.participants_checked += 1
        pairs = match_by_key(tally, events, fields["flow_cube_sop_instance_uid"], rows,
                             lambda r: r.get("flow_cube_sop_instance_uid"))
        for event, row in pairs:
            sop = row.get("flow_cube_sop_instance_uid")
            slot = superficial_enface_slot(row)
            expected = {key: row.get(column) for key, column in RETINAL_OCTA_COLUMNS.items()}
            expected["enface_layer"] = row.get(f"associated_enface_{slot}_ophthalmic_image_type")
            expected["enface_sop_instance_uid"] = row.get(
                f"associated_enface_{slot}_sop_instance_uid")
            if fields["enface_file_path"]:
                expected["enface_file_path"] = row.get(f"associated_enface_{slot}_file_path")
            for key, value in expected.items():
                tally.compare(event_value(event, fields[key]), value,
                              expect_empty=is_empty(value), pid=pid, field=key, sop=sop)
            # A preview is owed only where the source has the superficial
            # en-face image; the manifest writes "Not reported" where it does not.
            enface_path = row.get(f"associated_enface_{slot}_file_path")
            has_image = not is_empty(enface_path) and str(enface_path).strip() != NOT_REPORTED
            tally.compare(preview_state(event_value(event, fields["preview"])),
                          "present" if has_image else None, expect_empty=not has_image,
                          pid=pid, field="preview", sop=sop)
    count_source_outside_registry(tally, by_person, participants, limit)
    return tally


def read_wfdb_header(path):
    """Signal properties and '# key: value' comments of a WFDB header.

    Written apart from the importer's parser on purpose, so that a mistake in
    one is not silently reproduced by the other.
    """
    with open(path) as fh:
        lines = fh.read().splitlines()
    _, n_sig, fs, n_samples = lines[0].split()[:4]
    comments = {}
    for line in lines[1:]:
        if line.startswith("#"):
            key, sep, value = line[1:].partition(":")
            if sep:
                comments[key.strip()] = value.strip()
    return {"n_sig": int(n_sig), "fs": float(fs), "n_samples": int(n_samples)}, comments


def as_number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def as_iso_date(text):
    """20241014 or 2024-10-14 as 2024-10-14; None when it is neither."""
    if is_empty(text):
        return None
    text = str(text).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def ecg_range_status(value, low, high):
    if value is None:
        return None
    if value < low:
        name = "Below reference range"
    elif value > high:
        name = "Above reference range"
    else:
        name = "Within reference range"
    return dhis2.option_value(RANGE_STATUS_SET, name)


def ecg_qrs_status(value):
    if value is None:
        return None
    name = ("Widened (meets bundle branch block QRS criteria)"
            if value >= QRS_WIDENED_AT else "Within reference range")
    return dhis2.option_value(QRS_STATUS_SET, name)


def expected_ecg_values(header, comments, row, visit_date):
    """Every ECG data element the importer writes, recomputed from the source."""
    expected = {name: comments.get(key) for name, key in ECG_TEXT_FIELDS.items()}
    expected.update({name: as_number(comments.get(key))
                     for name, key in ECG_NUMBER_FIELDS.items()})
    expected["ECG Heart Rate Status"] = ecg_range_status(
        expected["ECG Heart Rate (bpm)"], *HR_RANGE)
    expected["ECG PR Interval Status"] = ecg_range_status(
        expected["ECG PR Interval (ms)"], *PR_RANGE)
    expected["ECG QRS Duration Status"] = ecg_qrs_status(expected["ECG QRS Duration (ms)"])
    expected["ECG Study Visit Date"] = as_iso_date(visit_date)
    expected["ECG Validation Date"] = as_iso_date(comments.get("validation_date"))
    expected["ECG Recording Duration (sec)"] = (
        round(header["n_samples"] / header["fs"], 2) if header["fs"] > 0 else None)
    expected["ECG Sampling Frequency (Hz)"] = header["fs"]
    expected["ECG Number of Leads"] = header["n_sig"]
    expected["ECG Number of Samples"] = header["n_samples"]
    expected["ECG Raw Data File Path"] = row.get("wfdb_dat_filepath")
    return expected


def verify_ecg(session, registry, participants, limit=None):
    """Every ECG event: 28 fields against its WFDB header, matched by header path."""
    manifest = aireadi.load_manifest("cardiac_ecg")
    visits = pd.read_csv(aireadi.participants_file(), sep="\t", dtype=str)
    visit_dates = dict(zip(visits["person_id"], visits["study_visit_date"]))
    stage_uid = registry.stage("Cardiac - 12-Lead ECG")
    path_de = registry.data_element("ECG Raw Header File Path")
    tally = Tally("ecg")

    for tei, pid in every_participant(participants, limit):
        rows = manifest.get(pid, [])
        events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei)
        if rows or events:
            tally.participants_checked += 1
        pairs = match_by_key(tally, events, path_de, rows,
                             lambda r: r.get("wfdb_hea_filepath"))
        for event, row in pairs:
            path = aireadi.resolve("cardiac_ecg", row["wfdb_hea_filepath"])
            header, comments = read_wfdb_header(path)
            expected = expected_ecg_values(header, comments, row, visit_dates.get(pid))
            for name, value in expected.items():
                tally.compare(event_value(event, registry.data_element(name)), value,
                              expect_empty=is_empty(value), pid=pid, field=name,
                              header=row["wfdb_hea_filepath"])
    count_source_outside_registry(tally, manifest, participants, limit)
    return tally


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def stage_event_count(session, stage_uid):
    """Live event count for a stage. Returns None rather than a silent zero."""
    try:
        payload = dhis2.get_json(session, "tracker/events", {
            "program": M.PROGRAM_UID, "programStage": stage_uid,
            "pageSize": 1, "totalPages": "true",
        })
    except dhis2.Dhis2Error as exc:
        print(f"    count failed for {stage_uid}: {exc}")
        return None
    for path in (("page", "total"), ("pager", "total"), ("total",)):
        node = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, int):
            return node
    print(f"    could not find a total in the count response for {stage_uid}")
    return None


def plan_sample_sizes(session, registry, groups, target_total):
    """Allocate the sample proportionally to live event counts.

    This is the allocation the old docstring described. It was never called,
    because main overrode it with a fixed 500 per group. It is called now, and
    a group whose count cannot be established is reported rather than being
    silently allocated zero.
    """
    counts, unknown = {}, []
    for group, stage_names in groups.items():
        total = 0
        for name in stage_names:
            count = stage_event_count(session, registry.stage(name))
            if count is None:
                unknown.append(group)
                break
            total += count
        else:
            counts[group] = total
            print(f"  {group}: {total} events")

    if unknown:
        return None, counts, unknown

    grand = sum(counts.values())
    if not grand:
        return None, counts, list(groups)
    sizes = {g: max(1, round(target_total * c / grand)) for g, c in counts.items()}
    return sizes, counts, []


def record_mismatches(group, mismatches):
    """Append one group's mismatches to MISMATCH_FILE, keyed by group.

    The file holds the context of every mismatch (group, stage, participant,
    hour, field, both values, the source hour's reading count) so that a
    discrepancy can be classified, for instance as a statistic undefined on
    both sides, without repeating the source aggregation.
    """
    existing = {}
    if os.path.exists(MISMATCH_FILE):
        with open(MISMATCH_FILE) as fh:
            existing = json.load(fh)
    existing[group] = mismatches
    with open(MISMATCH_FILE, "w") as fh:
        json.dump(existing, fh, indent=1)



SUMMED = ("values_compared", "matched", "empty_as_expected", "mismatched",
          "events_with_no_source_hour", "source_hours_with_no_event", "discrepancies",
          "participants_with_records", "flagged_time_lists_stored_uncapped",
          "source_records_repeated")


def merge_checkpoints(paths):
    """Sum the per-group results of shard runs into one set of results.

    Every count adds across shards because each participant is in exactly
    one shard. Elapsed time is the longest shard, since they ran side by side.
    """
    merged = {}
    for path in paths:
        with open(path) as fh:
            results = json.load(fh)["results"]
        for group, r in results.items():
            m = merged.setdefault(group, {k: 0 for k in SUMMED})
            for key in SUMMED:
                m[key] += r.get(key) or 0
            m["elapsed_seconds"] = max(m.get("elapsed_seconds", 0), r.get("elapsed_seconds", 0))
            m["participants_sampled"] = r.get("participants_sampled")
            fields = m.setdefault("empty_as_expected_by_field", {})
            for f, n in r.get("empty_as_expected_by_field", {}).items():
                fields[f] = fields.get(f, 0) + n
            stages = m.setdefault("by_stage", {})
            for stage, e in r.get("by_stage", {}).items():
                s = stages.setdefault(stage, {"compared": 0, "matched": 0, "empty_as_expected": 0,
                                              "mismatched": 0, "no_source": 0, "no_event": 0,
                                              "fields_compared": []})
                for key in ("compared", "matched", "empty_as_expected", "mismatched",
                            "no_source", "no_event"):
                    s[key] += e.get(key, 0)
                s["fields_compared"] = sorted(set(s["fields_compared"]) | set(e["fields_compared"]))
    for m in merged.values():
        agreed = m["matched"] + m["empty_as_expected"]
        m["agreement_pct"] = (round(100 * agreed / m["values_compared"], 4)
                              if m["values_compared"] else None)
        m["shards"] = len(paths)
    return merged


def mismatch_kind(m):
    if is_empty(m.get("dhis2")) and is_empty(m.get("source")):
        return "empty on both sides, unexpected"
    if is_empty(m.get("dhis2")) or is_empty(m.get("source")):
        return "empty on one side"
    return "values differ"


def summarise_mismatches():
    """Print and return a breakdown of recorded mismatches by group, field and kind.

    The returned rows carry counts only, so they can go into results/ without
    any participant-level value.
    """
    if not os.path.exists(MISMATCH_FILE):
        return []
    with open(MISMATCH_FILE) as fh:
        recorded = json.load(fh)
    rows = []
    print("\nMISMATCH BREAKDOWN (from %s)" % MISMATCH_FILE)
    for group, items in recorded.items():
        by_kind = {}
        for m in items:
            key = (m.get("field") or "-", mismatch_kind(m))
            by_kind[key] = by_kind.get(key, 0) + 1
        for (field, kind), n in sorted(by_kind.items()):
            print(f"  {group:<22}{field:<34}{kind:<34}{n:>8}")
            rows.append({"group": group, "field": field, "kind": kind, "count": n})
    if not rows:
        print("  none")
    return rows


def write_results(session, results, method, breakdown):
    """Aggregate counts only, dated, under results/. No participant values."""
    totals = {key: sum(r[key] for r in results.values()) for key in (
        "values_compared", "matched", "empty_as_expected", "mismatched",
        "events_with_no_source_hour", "source_hours_with_no_event", "discrepancies")}
    agreed = totals["matched"] + totals["empty_as_expected"]
    totals["agreement_pct"] = (round(100 * agreed / totals["values_compared"], 4)
                               if totals["values_compared"] else None)
    today = dt.date.today().isoformat()
    payload = {
        "run_date": today,
        "script": "7. verification data script/stratified_sample_verification.py",
        "dhis2_version": dhis2.get_json(session, "system/info").get("version"),
        "method": method,
        "results": results,
        "totals": totals,
        "mismatch_breakdown": breakdown,
        "prior_core_registry_result": PRIOR_CORE_REGISTRY,
    }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"stratified_verification_{today}.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    print(f"\nResults written to {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixed-n", type=int,
                        help="participants per hourly group, skipping the live count queries")
    parser.add_argument("--target-total", type=int, default=TOTAL_SAMPLE_TARGET)
    parser.add_argument("--min-per-group", type=int, default=MIN_PER_GROUP,
                        help="floor on the participants drawn for each hourly group")
    parser.add_argument("--groups", help="comma-separated subset of groups to run")
    parser.add_argument("--record-limit", type=int,
                        help="check only the first N participants of each per-record "
                             "group; for trying the script out, not for results")
    parser.add_argument("--exhaustive", action="store_true",
                        help="check every participant in the hourly groups too")
    parser.add_argument("--shard", help="K/N: check every N-th participant from K, so N "
                        "copies can run side by side in separate directories")
    parser.add_argument("--merge", nargs="+", metavar="CHECKPOINT",
                        help="combine shard checkpoints into this directory's results, "
                             "then report as usual")
    parser.add_argument("--fresh", action="store_true", help="ignore the checkpoint")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session(read_only=True)
    registry = M.load(session)

    hourly_stages = {
        "wearable": WEARABLE_STAGES,
        "glucose": ["CGM - Glucose"],
        "environmental": [
            "Environment - PM1", "Environment - PM2.5", "Environment - PM4",
            "Environment - PM10", "Environment - Humidity",
            "Environment - Temperature", "Environment - VOC", "Environment - NOx",
        ],
    }
    selected = args.groups.split(",") if args.groups else list(RECORD_GROUPS + HOURLY_GROUPS)
    unknown_groups = set(selected) - set(HOURLY_GROUPS + RECORD_GROUPS)
    if unknown_groups:
        parser.error(f"unknown group(s): {', '.join(sorted(unknown_groups))}")

    participants = get_participants(session)
    print(f"Participants in the registry: {len(participants)}")
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        participants = sorted(participants)[k::n]
        print(f"Shard {k}/{n}: {len(participants)} participants")

    if args.exhaustive:
        sizes = {g: None for g in HOURLY_GROUPS}
        allocation = "every participant"
    elif args.fixed_n:
        sizes = {g: args.fixed_n for g in HOURLY_GROUPS}
        allocation = f"fixed {args.fixed_n} participants per hourly group"
    else:
        print("\nCounting live events per hourly group for proportional allocation")
        sizes, _, unknown = plan_sample_sizes(
            session, registry, hourly_stages, args.target_total
        )
        if sizes is None:
            fallback = max(1, args.target_total // len(HOURLY_GROUPS))
            print(f"  counts unavailable for {unknown}, falling back to "
                  f"{fallback} per group")
            sizes = {g: fallback for g in HOURLY_GROUPS}
            allocation = f"fixed {fallback} per hourly group, live counts unavailable"
        else:
            allocation = (f"proportional to live event counts across the hourly groups, "
                          f"target {args.target_total} participants, "
                          f"floor {args.min_per_group} per group")
        sizes = {g: max(n, args.min_per_group) for g, n in sizes.items()}
    hourly_method = ("every participant, every event, every stored field" if args.exhaustive
                     else f"stratified random sampling without replacement, {allocation}, "
                          f"independent seed per group derived from base seed {BASE_SEED}, "
                          f"every stored field")
    method = (f"hourly groups: {hourly_method}; per-record groups: every participant, "
              f"every event, every mapped field; tolerance 0.01; an empty pair counts as "
              f"agreement only where a rule on the source says the value must be "
              f"empty, and is reported separately; events with no source "
              f"record and source records with no event are counted separately")

    checkpoint = {"results": {}}
    if args.fresh and os.path.exists(MISMATCH_FILE):
        os.remove(MISMATCH_FILE)
    if os.path.exists(CHECKPOINT_FILE) and not args.fresh:
        with open(CHECKPOINT_FILE) as fh:
            checkpoint = json.load(fh)
        checkpoint.setdefault("results", {})
        stale = [g for g, r in checkpoint["results"].items() if "agreement_pct" not in r]
        for group in stale:
            print(f"[checkpoint] {group} was counted under the old empty-pair rule; re-running")
            del checkpoint["results"][group]

    if args.merge:
        checkpoint = {"results": merge_checkpoints(args.merge)}
        with open(CHECKPOINT_FILE, "w") as fh:
            json.dump(checkpoint, fh, indent=2)
        mismatches = {}
        for path in args.merge:
            other = os.path.join(os.path.dirname(os.path.abspath(path)), MISMATCH_FILE)
            if os.path.exists(other):
                with open(other) as fh:
                    for group, items in json.load(fh).items():
                        mismatches.setdefault(group, []).extend(items)
        with open(MISMATCH_FILE, "w") as fh:
            json.dump(mismatches, fh, indent=1)
        print(f"Merged {len(args.merge)} checkpoint(s): {sorted(checkpoint['results'])}")

    limit = args.record_limit
    verifiers = {
        "wearable": lambda: verify_wearable(
            session, registry, participants, sizes["wearable"], args.exhaustive),
        "glucose": lambda: verify_glucose(
            session, registry, participants, sizes["glucose"], args.exhaustive),
        "environmental": lambda: verify_environmental(
            session, registry, participants, sizes["environmental"], args.exhaustive),
        "diagnosis": lambda: verify_diagnosis(session, registry, participants, limit),
        "retinal_photography": lambda: verify_retinal_photography(
            session, registry, participants, limit),
        "retinal_octa": lambda: verify_retinal_octa(session, registry, participants, limit),
        "ecg": lambda: verify_ecg(session, registry, participants, limit),
    }

    start = time.time()
    for group in selected:
        if group in checkpoint["results"]:
            print(f"[resume] {group} already done")
            continue
        exhaustive = group in RECORD_GROUPS or args.exhaustive
        print(f"\n[run] {group}, " + (f"all participants (limit {limit})" if exhaustive
                                     else f"n={sizes[group]}"))
        group_start = time.time()
        tally = verifiers[group]()
        result = tally.as_dict()
        if exhaustive:
            result["participants_sampled"] = (
                f"first {limit}" if limit and group in RECORD_GROUPS else "all")
            result["participants_with_records"] = tally.participants_checked
        else:
            result["participants_sampled"] = sizes[group]
            result["seed"] = group_seed(group)
        result["elapsed_seconds"] = round(time.time() - group_start)
        checkpoint["results"][group] = result
        with open(CHECKPOINT_FILE, "w") as fh:
            json.dump(checkpoint, fh, indent=2)
        record_mismatches(group, tally.mismatches)
        print(f"[done] {group}: {result}")
        if tally.mismatches:
            print(f"       {len(tally.mismatches)} mismatches written to {MISMATCH_FILE}")

    results = checkpoint["results"]
    compared = sum(r["values_compared"] for r in results.values())
    agreed = sum(r["matched"] + r["empty_as_expected"] for r in results.values())
    discrepancies = sum(r["discrepancies"] for r in results.values())

    print("\n" + "=" * 72)
    print("VERIFICATION")
    print(f"  method: {method}")
    print(f"  elapsed this session: {time.time() - start:.0f}s")
    print()
    print(f"  {'group':<22}{'compared':>10}{'matched':>10}{'empty ok':>10}{'mismatch':>10}"
          f"{'no source':>11}{'no event':>10}{'agree':>9}")
    for group, r in results.items():
        rate = f"{r['agreement_pct']:.2f}%" if r["agreement_pct"] is not None else "n/a"
        print(f"  {group:<22}{r['values_compared']:>10}{r['matched']:>10}"
              f"{r['empty_as_expected']:>10}{r['mismatched']:>10}"
              f"{r['events_with_no_source_hour']:>11}"
              f"{r['source_hours_with_no_event']:>10}{rate:>9}")
    print()
    print(f"  Values compared: {compared}")
    print(f"  Discrepancies:   {discrepancies}")
    if compared:
        print(f"  Agreement:       {100 * agreed / compared:.4f}%")
    for group, r in results.items():
        if r["empty_as_expected_by_field"]:
            print(f"  empty as expected, {group}: {r['empty_as_expected_by_field']}")

    breakdown = summarise_mismatches()
    if not args.groups and not args.record_limit and not args.fixed_n and not args.shard:
        write_results(session, results, method, breakdown)

    # H-12: reported next to the sampled result, never merged into it.
    print("\nPRIOR CORE REGISTRY RESULT, not re-verified by this run")
    print(f"  values: {PRIOR_CORE_REGISTRY['values']}")
    print(f"  discrepancies: {PRIOR_CORE_REGISTRY['discrepancies']}")
    print(f"  method: {PRIOR_CORE_REGISTRY['method']}")
    print("  This figure comes from an earlier phase. Do not add it to the "
          "sampled totals above: doing so folds an assumed 100% into a "
          "measured rate.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
