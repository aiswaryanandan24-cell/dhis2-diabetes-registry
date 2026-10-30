#!/usr/bin/env python3
"""Export the whole Diabetes Registry program as one importable metadata package.

READ-ONLY against DHIS2. GET requests only.

WHY THIS EXISTS
---------------
The program, its ten attributes and the six core stages were built by hand in
the Maintenance app, so before this export nothing in the repository could
recreate them; only the 21 modality stages had step-1 scripts. This script
takes the program's dependency export (/api/programs/{uid}/metadata), which
carries every stage, data element, option set, attribute and the tracked
entity type, adds the four organisation units the program is assigned to,
and writes metadata/diabetes_registry_program.json.

Removed from the export, because they identify accounts on this server or
change on every export without changing what is imported:
created, lastUpdated, createdBy, lastUpdatedBy, sharing owners and per-user
or per-group sharing (public access is kept), the program's user roles, and
the export's system block.

The package is checked before it is written: every object a reference points
to must be in the package, apart from the default category model that every
DHIS2 instance carries under the same identifiers.

IMPORTING IT
------------
Import into a DHIS2 2.44 instance in one step, either in the Import/Export app
(Metadata import, JSON) or through the API:

    curl -u USER -H "Content-Type: application/json" \
         -X POST "$DHIS2_URL/api/metadata?importStrategy=CREATE_AND_UPDATE&atomicMode=ALL" \
         --data-binary @metadata/diabetes_registry_program.json

Identifiers are preserved, so common/metadata_uids.py and every import script
work against the new instance unchanged. The organisation units arrive as a
new root, "AI-READI Study", with the three recruitment sites beneath it;
assign your users to it before loading data.

USAGE
-----
    python3 "1. core registry/export_program_metadata.py"
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import dhis2  # noqa: E402
from common import metadata_uids as M  # noqa: E402

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "metadata", "diabetes_registry_program.json")

DROP_KEYS = {"created", "lastUpdated", "createdBy", "lastUpdatedBy", "user",
             "userRoles", "href", "access", "favorites"}
ORG_UNIT_FIELDS = "id,name,shortName,code,openingDate,parent[id]"

# The default category model has the same identifiers on every instance.
DEFAULT_IDS = {"bjDvmb4bfuf", "GLevLNI9wkl", "xYerKDKCefk", "HllvX50cXC0"}


def scrub(node):
    """Drop account references and volatile fields at every depth."""
    if isinstance(node, list):
        return [scrub(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in DROP_KEYS:
            continue
        if key == "sharing" and isinstance(value, dict):
            out[key] = {"public": value.get("public"), "external": value.get("external", False),
                        "users": {}, "userGroups": {}}
            continue
        out[key] = scrub(value)
    return out


def referenced_ids(node, found=None, top=True):
    """Every {"id": ...} reference below the top level of each object."""
    if found is None:
        found = set()
    if isinstance(node, list):
        for item in node:
            referenced_ids(item, found, top)
    elif isinstance(node, dict):
        if not top and set(node) == {"id"}:
            found.add(node["id"])
        for value in node.values():
            referenced_ids(value, found, top=False)
    return found


def check_references(package):
    defined = {obj["id"] for objects in package.values() if isinstance(objects, list)
               for obj in objects if isinstance(obj, dict) and "id" in obj}
    referenced = set()
    for objects in package.values():
        if isinstance(objects, list):
            for obj in objects:
                referenced |= referenced_ids(obj)
    missing = referenced - defined - DEFAULT_IDS
    if missing:
        raise SystemExit(f"package references {len(missing)} object(s) it does not "
                         f"contain: {sorted(missing)[:10]}")
    return len(defined), len(referenced)


def main():
    session = dhis2.get_session(read_only=True)
    package = dhis2.get_json(session, f"programs/{M.PROGRAM_UID}/metadata.json")
    package.pop("system", None)

    program = package["programs"][0]
    site_ids = [ou["id"] for ou in program.get("organisationUnits", [])]
    sites = dhis2.get_json(session, "organisationUnits", {
        "filter": f"id:in:[{','.join(site_ids)}]", "fields": ORG_UNIT_FIELDS,
        "paging": "false",
    })["organisationUnits"]
    parent_ids = sorted({ou["parent"]["id"] for ou in sites if ou.get("parent")})
    parents = dhis2.get_json(session, "organisationUnits", {
        "filter": f"id:in:[{','.join(parent_ids)}]", "fields": ORG_UNIT_FIELDS,
        "paging": "false",
    })["organisationUnits"] if parent_ids else []
    if any(ou.get("parent") for ou in parents):
        raise SystemExit("the sites' parent is not a root; extend the export to its ancestors")
    package["organisationUnits"] = parents + sites

    package = scrub(package)
    for key, objects in package.items():
        if isinstance(objects, list):
            objects.sort(key=lambda obj: obj.get("id", ""))

    text = json.dumps(package, indent=1, ensure_ascii=False, sort_keys=True)
    for marker in ("username", '"email"', "password"):
        if marker in text:
            raise SystemExit(f"refusing to write: the package still contains {marker}")
    defined, referenced = check_references(package)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")

    stages = package["programStages"]
    slots = sum(len(stage.get("programStageDataElements", [])) for stage in stages)
    print(f"Wrote {os.path.relpath(OUTPUT)}")
    for key, objects in sorted(package.items()):
        if isinstance(objects, list):
            print(f"  {key}: {len(objects)}")
    print(f"  data element slots across stages: {slots}")
    print(f"  {defined} objects defined, {referenced} distinct references, all resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
