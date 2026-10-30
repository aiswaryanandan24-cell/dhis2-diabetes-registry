#!/usr/bin/env python3
"""Delete sleep events that repeat a segment verbatim or end before they start.

WHY THIS EXISTS
---------------
The Garmin sleep export repeats segments verbatim: 169,960 of 565,959 valid
segments in the AI-READI release are exact copies of another (same start,
stage and duration), in 1,567 of 2,184 participants, and summed as exported
they overstate sleep time by about 43%. The loader gave repeated source rows
distinct identifiers so that neither would be lost, which carried the
artifact into the registry. Nine more events, loaded in August 2026, are
segments whose end precedes their start; the loader has dropped those for
some time but the early load kept them.

extract_sleep_segments now drops exact repeats and negative durations, so a
re-run cannot bring them back. This script removes what is already on the
server: for each participant it keeps as many sleep events per (start,
stage, duration) as the de-duplicated source has, earliest created first,
and deletes the rest.

SAFETY
------
A participant is touched only if every de-duplicated source segment is
matched by at least one event. If any segment has no event, the keys are not
lining up, deleting the "surplus" could delete real data, and the
participant is recorded as failed instead. Deletions are verified per batch
from the server's per-type counts.

USAGE
-----
    python3 "2. wearable script/sleep_dedupe_repair.py" --dry-run
    python3 "2. wearable script/sleep_dedupe_repair.py" --person-id 4310
    nohup python3 "2. wearable script/sleep_dedupe_repair.py" > sleep_dedupe.log 2>&1 &
"""

import argparse
import collections
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402
from common.timeutil import parse_instant  # noqa: E402

from hourly_aggregation_logic_final import extract_sleep_segments  # noqa: E402

CHECKPOINT_FILE = "sleep_dedupe_checkpoint.json"
DELETE_BATCH = 500


def segment_key(start, stage, minutes):
    try:
        minutes = round(float(minutes), 1)
    except (TypeError, ValueError):
        minutes = None
    return (parse_instant(start).isoformat(), stage, minutes)


def plan_participant(session, stage_uid, stage_de, duration_de, tei, path):
    """(events to delete, reason it cannot be planned or None, events seen)."""
    wanted = collections.Counter(segment_key(*seg) for seg in extract_sleep_segments(path))
    events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei,
                                fields="event,occurredAt,createdAt,dataValues[dataElement,value]")
    by_key = collections.defaultdict(list)
    for event in events:
        values = {dv["dataElement"]: dv.get("value") for dv in event.get("dataValues", [])}
        by_key[segment_key(event["occurredAt"], values.get(stage_de),
                           values.get(duration_de))].append(event)

    missing = sum(max(0, n - len(by_key.get(key, []))) for key, n in wanted.items())
    if missing:
        return [], f"{missing} source segment(s) have no event; keys do not line up", len(events)

    surplus = []
    for key, group in by_key.items():
        group.sort(key=lambda e: (e.get("createdAt", ""), e["event"]))
        surplus.extend(e["event"] for e in group[wanted.get(key, 0):])
    return surplus, None, len(events)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="count, do not delete")
    parser.add_argument("--person-id", help="one participant")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session(read_only=args.dry_run)
    registry = M.load(session)
    stage_uid = registry.stage("Wearable - Sleep")
    stage_de = registry.data_element("Sleep Stage")
    duration_de = registry.data_element("Sleep Segment Duration Minutes")

    manifest = aireadi.load_manifest("wearable_activity_monitor")
    entities = dhis2.fetch_all_pages(
        session, "tracker/trackedEntities",
        {"program": M.PROGRAM_UID, "fields": "trackedEntity,attributes[attribute,value]"},
        ("trackedEntities", "instances"),
    )
    tei_of = {}
    for item in entities:
        for attr in item.get("attributes", []):
            if attr.get("attribute") == M.PERSON_ID_ATTR_UID and attr.get("value"):
                tei_of[attr["value"]] = item["trackedEntity"]

    ids = [args.person_id] if args.person_id else sorted(p for p in manifest if p in tei_of)
    totals = {"participants": 0, "events": 0, "surplus": 0, "deleted": 0}
    blocked = {}
    start = time.time()

    # A dry run writes nothing that a real run would read back.
    checkpoint_file = (CHECKPOINT_FILE if not args.dry_run
                       else os.path.join(tempfile.mkdtemp(), CHECKPOINT_FILE))
    with Checkpoint(checkpoint_file, flush_every=20) as checkpoint:
        remaining = ids if args.dry_run else checkpoint.pending(ids)
        for index, pid in enumerate(remaining, start=1):
            path = None
            for row in manifest.get(pid, []):
                path = aireadi.resolve("wearable_activity_monitor", row.get("sleep_filepath")) or path
            if not path:
                continue
            try:
                surplus, reason, seen = plan_participant(
                    session, stage_uid, stage_de, duration_de, tei_of[pid], path)
                totals["participants"] += 1
                totals["events"] += seen
                if reason:
                    blocked[pid] = reason
                    if not args.dry_run:
                        checkpoint.mark_failed(pid, reason)
                    continue
                totals["surplus"] += len(surplus)
                if not args.dry_run:
                    for i in range(0, len(surplus), DELETE_BATCH):
                        batch = surplus[i:i + DELETE_BATCH]
                        dhis2.import_tracker(
                            session, {"events": [{"event": uid} for uid in batch]}, "DELETE",
                            expect="deleted", expect_count=len(batch), tracker_type="EVENT")
                        totals["deleted"] += len(batch)
                    checkpoint.mark_done(pid, note=f"deleted {len(surplus)}")
            except Exception as exc:
                blocked[pid] = str(exc)[:200]
                if not args.dry_run:
                    checkpoint.mark_failed(pid, exc)
            if index % 200 == 0:
                print(f"  {index}/{len(remaining)} {totals} {time.time() - start:.0f}s", flush=True)

    print(f"\n{'DRY RUN ' if args.dry_run else ''}totals: {totals}")
    print(f"participants not touched: {len(blocked)}")
    for pid, reason in list(blocked.items())[:10]:
        print(f"  {pid}: {reason}")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
