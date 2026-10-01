#!/usr/bin/env python3
"""
Filter a MATSim vehicles file: keep only vehicles whose ID appears as a
person ID in the given plans file.

Usage:
    python filter_vehicles_by_plans.py plans.xml.gz vehicles.xml.gz output.xml.gz
"""

import argparse
import gzip
import re
import sys
from pathlib import Path


def collect_person_ids(plans_path: Path) -> set:
    """Return the set of all person IDs found in the plans file."""
    person_re = re.compile(r'<person\s[^>]*id="([^"]+)"')
    open_fn = gzip.open if plans_path.suffix == ".gz" else open
    ids = set()
    with open_fn(plans_path, "rt", encoding="utf-8") as f:
        for line in f:
            m = person_re.search(line)
            if m:
                ids.add(m.group(1))
    return ids


def filter_vehicles(plans_path: Path, vehicles_path: Path, output_path: Path) -> None:
    person_ids = collect_person_ids(plans_path)

    vehicle_id_re = re.compile(r'<vehicle\s[^>]*id="([^"]+)"')
    open_in  = gzip.open if vehicles_path.suffix == ".gz" else open
    open_out = gzip.open if output_path.suffix   == ".gz" else open

    kept = removed = 0
    in_vehicle = False
    buffer = []
    current_id = None
    # Track whether we've seen the first <vehicle ...> line yet, so we can
    # pass through the header / vehicleType block unchanged.
    past_header = False

    with open_in(vehicles_path, "rt", encoding="utf-8") as fin, \
         open_out(output_path, "wt", encoding="utf-8") as fout:

        for line in fin:
            if not in_vehicle:
                m = vehicle_id_re.search(line)
                if m:
                    past_header = True
                    in_vehicle = True
                    current_id = m.group(1)
                    buffer = [line]
                    # Single-line <vehicle .../> element
                    if "/>" in line or "</vehicle>" in line:
                        if current_id in person_ids:
                            fout.writelines(buffer)
                            kept += 1
                        else:
                            removed += 1
                        in_vehicle = False
                        buffer = []
                else:
                    fout.write(line)
            else:
                buffer.append(line)
                if "</vehicle>" in line:
                    if current_id in person_ids:
                        fout.writelines(buffer)
                        kept += 1
                    else:
                        removed += 1
                    in_vehicle = False
                    buffer = []

        # Flush any unclosed buffer (shouldn't happen with valid XML)
        if buffer:
            fout.writelines(buffer)

    print(f"Plans file       : {plans_path}")
    print(f"Vehicles file    : {vehicles_path}")
    print(f"Persons in plans : {len(person_ids)}")
    print(f"Kept             : {kept}")
    print(f"Removed          : {removed}")
    print(f"Total            : {kept + removed}")
    print(f"Output           : {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove vehicles that have no matching person in the plans file."
    )
    parser.add_argument("plans",    type=Path, help="Plans file (.xml or .xml.gz)")
    parser.add_argument("vehicles", type=Path, help="Vehicles file (.xml or .xml.gz)")
    parser.add_argument("output",   type=Path, help="Output vehicles file (.xml or .xml.gz)")
    args = parser.parse_args()

    for p in (args.plans, args.vehicles):
        if not p.exists():
            print(f"Error: file not found: {p}", file=sys.stderr)
            sys.exit(1)

    if args.output == args.vehicles:
        print("Error: output path must differ from input vehicles path.", file=sys.stderr)
        sys.exit(1)

    filter_vehicles(args.plans, args.vehicles, args.output)


if __name__ == "__main__":
    main()
