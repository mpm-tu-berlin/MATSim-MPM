#!/usr/bin/env python3
"""
Filter a MATSim plans file: keep only agents whose first activity
ends (i.e. departs) within the specified QSim time window.

Usage:
    python filter_plans_by_time.py input.xml.gz output.xml.gz
    python filter_plans_by_time.py input.xml.gz output.xml.gz --max-hours 96
"""

import argparse
import gzip
import re
import sys
from pathlib import Path


def parse_matsim_time(time_str: str) -> float:
    """Parse 'HH:MM:SS' (hours may exceed 24) to seconds."""
    h, m, s = time_str.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def filter_plans(input_path: Path, output_path: Path, max_hours: float) -> None:
    max_seconds = max_hours * 3600

    open_in  = gzip.open if input_path.suffix  == ".gz" else open
    open_out = gzip.open if output_path.suffix == ".gz" else open

    end_time_re = re.compile(r'end_time="([^"]+)"')

    kept = removed = 0

    with open_in(input_path, "rt", encoding="utf-8") as fin, \
         open_out(output_path, "wt", encoding="utf-8") as fout:

        in_person = False
        buffer = []
        person_end_time = None

        for line in fin:
            if not in_person:
                # Detect start of a <person ...> block
                if re.search(r"<person[\s>]", line):
                    in_person = True
                    buffer = [line]
                    person_end_time = None
                else:
                    # Header, comments, <population>, </population> — write as-is
                    fout.write(line)
            else:
                buffer.append(line)

                # Capture the first end_time we encounter in this person block
                if person_end_time is None:
                    m = end_time_re.search(line)
                    if m:
                        person_end_time = parse_matsim_time(m.group(1))

                if "</person>" in line:
                    if person_end_time is None or person_end_time <= max_seconds:
                        fout.writelines(buffer)
                        kept += 1
                    else:
                        removed += 1
                    in_person = False
                    buffer = []

        # Flush any unclosed buffer (shouldn't happen with valid XML)
        if buffer:
            fout.writelines(buffer)

    print(f"Threshold : {max_hours}h  ({max_seconds:.0f} s)")
    print(f"Kept      : {kept}")
    print(f"Removed   : {removed}")
    print(f"Total     : {kept + removed}")
    print(f"Output    : {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove MATSim agents that depart after the QSim end time."
    )
    parser.add_argument("input",  type=Path, help="Input plans file (.xml or .xml.gz)")
    parser.add_argument("output", type=Path, help="Output plans file (.xml or .xml.gz)")
    parser.add_argument(
        "--max-hours", type=float, default=96.0,
        help="QSim end time in hours (default: 96)",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    if args.output == args.input:
        print("Error: output path must differ from input path.", file=sys.stderr)
        sys.exit(1)

    filter_plans(args.input, args.output, args.max_hours)


if __name__ == "__main__":
    main()
