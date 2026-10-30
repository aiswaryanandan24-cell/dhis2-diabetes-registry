#!/usr/bin/env python3
"""Fill humidity flagged-time lists that are empty although the source flags readings.

WHY THIS EXISTS
---------------
Exhaustive verification found humidity hours whose above- or below-range
count is stored but whose list of flagged times is empty. The affected lists
are the long ones: on the participants checked, every list up to 712 entries
(7,118 characters) had been stored in full by the load that predates the
20-entry cap (M-19), and every list from 719 entries up was missing, mostly
hours in which all 720 readings were outside the range.

This script writes the capped list that the current loader produces
(summarise_timestamps: the first 20 times and how many more) into exactly
those hours. It never overwrites a stored value: a list that is already
there, capped or in full, is left alone. Updates carry the event's other
values (merge_data_values) and are verified per batch.

USAGE
-----
    python3 "4. envt/humidity_timestamp_repair.py" --dry-run
    python3 "4. envt/humidity_timestamp_repair.py" --person-id 7632
    nohup python3 "4. envt/humidity_timestamp_repair.py" > humidity_repair.log 2>&1 &
    for k in 0 1 2 3; do nohup python3 "4. envt/humidity_timestamp_repair.py" --shard $k/4 > h$k.log 2>&1 & done
"""

import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import aireadi, dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402
from common.checkpoint import Checkpoint  # noqa: E402
from common.timeutil import hour_key  # noqa: E402

from env_aggregation_logic_v2 import aggregate_env_column, read_env_csv  # noqa: E402

CHECKPOINT_FILE = "humidity_timestamp_repair_checkpoint.json"
LIST_FIELDS = {
    "above_ts": "Humidity Above Comfort Range (>50%) - Timestamps",
    "below_ts": "Humidity Below Comfort Range (<30%) - Timestamps",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="count, do not write")
    parser.add_argument("--person-id", help="one participant")
    parser.add_argument("--shard", help="K/N: this process takes every N-th participant from K, "
                        "so N copies can run side by side")
    args, _ = parser.parse_known_args()

    session = dhis2.get_session(read_only=args.dry_run)
    registry = M.load(session)
    stage_uid = registry.environment_stage("hum")
    list_uids = {key: registry.data_element(name) for key, name in LIST_FIELDS.items()}

    manifest = aireadi.load_manifest("environment")
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
    shard_suffix = ""
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        ids = ids[k::n]
        shard_suffix = f"_{k}of{n}"
    totals = {"participants_affected": 0, "hours": 0, "lists_filled": 0, "written": 0}
    start = time.time()
    checkpoint_file = (CHECKPOINT_FILE.replace(".json", f"{shard_suffix}.json") if not args.dry_run
                       else os.path.join(tempfile.mkdtemp(), CHECKPOINT_FILE))

    with Checkpoint(checkpoint_file, flush_every=20) as checkpoint:
        remaining = ids if args.dry_run else checkpoint.pending(ids)
        for index, pid in enumerate(remaining, start=1):
            path = None
            for row in manifest.get(pid, []):
                path = aireadi.resolve("environment", row.get("env_sensor_filepath")) or path
            if not path:
                continue
            try:
                expected = {hour_key(h): s for h, s in
                            aggregate_env_column(read_env_csv(path), "hum").items()}
                events = dhis2.fetch_events(session, M.PROGRAM_UID, stage_uid, tei_of[pid])
                updates = []
                for event in events:
                    stats = expected.get(hour_key(event["occurredAt"]))
                    if not stats:
                        continue
                    changes = {}
                    for key, uid in list_uids.items():
                        if stats.get(key) and not dhis2.data_value_of(event, uid):
                            changes[uid] = stats[key]
                    if changes:
                        totals["lists_filled"] += len(changes)
                        updates.append(dhis2.event_update_payload(
                            event, stage_uid, M.PROGRAM_UID,
                            dhis2.merge_data_values(event, changes)))
                if updates:
                    totals["participants_affected"] += 1
                    totals["hours"] += len(updates)
                    if not args.dry_run:
                        dhis2.send_events(session, updates, "UPDATE", batch_size=200)
                        totals["written"] += len(updates)
                if not args.dry_run:
                    checkpoint.mark_done(pid, note=f"filled {len(updates)} hour(s)")
            except Exception as exc:
                if not args.dry_run:
                    checkpoint.mark_failed(pid, exc)
                print(f"  {pid}: FAILED {str(exc)[:200]}", flush=True)
            if index % 200 == 0:
                print(f"  {index}/{len(remaining)} {totals} {time.time() - start:.0f}s", flush=True)

    print(f"\n{'DRY RUN ' if args.dry_run else ''}totals: {totals}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
