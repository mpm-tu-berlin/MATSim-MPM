#!/usr/bin/env python3
"""
Assign varied initial SoC values to BET vehicle definitions.

50% of vehicles are randomly selected to receive initialSoC = 1.0 (fully charged).
The remaining 50% receive a SoC value sampled from the distribution in a
soc_histogram_time_profiles output file at the last time step (or a specified time).

Supports both histogram formats produced by this project:
- Standard MATSim 10-bin format:  columns  0+  0.1+  0.2+  ...  0.9+
- Custom MpmSocHistogram 20-bin:  columns  0%+ 5%+  10%+  ...  95%+

Usage:
    python assign_varied_soc.py <vehicles_xml> <histogram_txt> [options]

Options:
    --output PATH        Output XML path (default: <input>_varied_soc.xml)
    --full-share FLOAT   Fraction of vehicles with SoC=1.0 (default: 0.5)
    --time HH:MM         Histogram time step to use (default: last row)

Example:
    python scripts/assign_varied_soc.py \\
        scenarios/BETs/1.0pctBETs_1Iteration_unlimited/eTrucks_Vehicle.xml \\
        output/BETs/1.0pctBET_1Iteration_unlimited/ITERS/it.0/0.soc_histogram_time_profiles.txt
"""

import argparse
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Hard-coded seed — change here to alter the random vehicle assignment
RANDOM_SEED = 42

MATSIM_NS = "http://www.matsim.org/files/dtd"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


# ---------------------------------------------------------------------------
# Histogram parsing
# ---------------------------------------------------------------------------

def parse_bin_label(label: str) -> float:
    """Parse a bin column header to its lower SoC bound (fraction, 0.0–1.0).

    Two formats are handled:
    - Fraction notation:    '0.2+'  -> 0.2    (standard MATSim 10-bin output)
    - Percentage notation:  '20%+'  -> 0.2    (custom MpmSocHistogram 20-bin output)
    """
    label = label.strip()
    if label.endswith('%+'):
        return float(label[:-2]) / 100.0
    elif label.endswith('+'):
        return float(label[:-1])
    else:
        raise ValueError(f"Cannot parse bin column label: '{label}'")


def parse_histogram(hist_file: str, time_str: str = None):
    """Parse a soc_histogram_time_profiles file.

    Returns:
        bin_lowers  -- list of float, lower SoC bound of each bin
        bin_width   -- float, width of each bin (inferred from first two bins)
        weights     -- list of float, normalised vehicle fraction per bin
        time_label  -- str, the time stamp of the selected row
        bin_labels  -- list of str, original column header labels
    """
    with open(hist_file, encoding='utf-8') as f:
        lines = [line.rstrip('\n') for line in f if line.strip()]

    if not lines:
        raise ValueError("Histogram file is empty")

    header = lines[0].split('\t')
    if header[0].lower() != 'time':
        raise ValueError(f"Expected first header column to be 'time', got '{header[0]}'")

    bin_labels = header[1:]
    if not bin_labels:
        raise ValueError("No bin columns found in histogram header")

    bin_lowers = [parse_bin_label(b) for b in bin_labels]

    if len(bin_lowers) >= 2:
        bin_width = round(bin_lowers[1] - bin_lowers[0], 6)
    else:
        bin_width = 1.0 - bin_lowers[0]

    data_lines = lines[1:]
    if not data_lines:
        raise ValueError("Histogram file contains only a header row — no data")

    if time_str is None:
        row_str = data_lines[-1]
    else:
        row_str = None
        for line in data_lines:
            if line.startswith(time_str + '\t'):
                row_str = line
                break
        if row_str is None:
            raise ValueError(
                f"Time '{time_str}' not found in histogram.\n"
                f"Available range: {data_lines[0].split(chr(9))[0]} – "
                f"{data_lines[-1].split(chr(9))[0]}"
            )

    parts = row_str.split('\t')
    time_label = parts[0]
    counts = [float(x) for x in parts[1: len(bin_lowers) + 1]]
    total = sum(counts)
    if total == 0:
        raise ValueError(f"All bin counts are zero at time {time_label}")

    weights = [c / total for c in counts]
    return bin_lowers, bin_width, weights, time_label, bin_labels


# ---------------------------------------------------------------------------
# SoC sampling
# ---------------------------------------------------------------------------

def sample_soc(bin_lowers: list, bin_width: float, weights: list) -> float:
    """Pick a bin by weight, then sample SoC uniformly within that bin.

    The upper bound is capped at 1.0 - 1e-9 so that no 'non-full' vehicle
    ever receives exactly 1.0 (that value is reserved for the full group).
    """
    r = random.random()
    cumulative = 0.0
    for lower, w in zip(bin_lowers, weights):
        cumulative += w
        if r <= cumulative:
            upper = min(lower + bin_width, 1.0 - 1e-9)
            return random.uniform(lower, upper)
    # Floating-point edge case: fall into last bin
    lower = bin_lowers[-1]
    upper = min(lower + bin_width, 1.0 - 1e-9)
    return random.uniform(lower, upper)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            'Assign varied initial SoC values to BET vehicles. '
            '50%% of vehicles receive SoC=1.0; the rest are drawn from the '
            'histogram distribution at the last (or specified) time step.'
        )
    )
    parser.add_argument('vehicles_xml', help='Input MATSim vehicles XML file')
    parser.add_argument('histogram_txt',
                        help='soc_histogram_time_profiles .txt file from a previous run')
    parser.add_argument('--output', metavar='PATH',
                        help='Output XML path (default: <input>_varied_soc.xml)')
    parser.add_argument('--full-share', type=float, default=0.5, metavar='FLOAT',
                        help='Fraction of vehicles with SoC=1.0 (default: 0.5)')
    parser.add_argument('--time', metavar='HH:MM',
                        help='Time step to read from histogram (default: last row)')
    args = parser.parse_args()

    if not 0.0 <= args.full_share <= 1.0:
        print(f"ERROR: --full-share must be between 0.0 and 1.0, got {args.full_share}",
              file=sys.stderr)
        sys.exit(1)

    random.seed(RANDOM_SEED)
    print(f"Random seed (hardcoded): {RANDOM_SEED}")

    # Output path
    if args.output:
        output_path = Path(args.output)
    else:
        p = Path(args.vehicles_xml)
        output_path = p.parent / (p.stem + '_varied_soc' + p.suffix)

    # --- Parse histogram ---
    bin_lowers, bin_width, weights, time_label, bin_labels = parse_histogram(
        args.histogram_txt, args.time
    )
    print(f"Histogram file : {args.histogram_txt}")
    print(f"Time step used : {time_label}")
    print(f"Bins detected  : {len(bin_lowers)} (width={bin_width:.4f}, "
          f"format={'percent' if '%+' in bin_labels[0] else 'fraction'})")

    # --- Parse vehicles XML ---
    # Register namespaces before parsing so the output uses the same prefixes
    ET.register_namespace('', MATSIM_NS)
    ET.register_namespace('xsi', XSI_NS)

    tree = ET.parse(args.vehicles_xml)
    root = tree.getroot()

    ns = {'v': MATSIM_NS}
    vehicles = root.findall('v:vehicle', ns)
    if not vehicles:
        vehicles = root.findall('vehicle')  # fallback: no namespace
    n_total = len(vehicles)
    if n_total == 0:
        print("ERROR: No <vehicle> elements found in the XML file", file=sys.stderr)
        sys.exit(1)

    print(f"\nVehicles total : {n_total}")

    # --- Random 50/50 split ---
    n_full = round(n_total * args.full_share)
    full_indices = set(random.sample(range(n_total), n_full))

    # --- Assign SoC values ---
    bin_hit_counts = [0] * len(bin_lowers)
    n_modified_full = 0
    n_modified_varied = 0

    for i, vehicle in enumerate(vehicles):
        # Locate or create <attributes>
        attributes = vehicle.find('v:attributes', ns)
        if attributes is None:
            attributes = vehicle.find('attributes')
        if attributes is None:
            attributes = ET.SubElement(vehicle, f'{{{MATSIM_NS}}}attributes')

        # Locate or create the initialSoc <attribute> element
        soc_attr = None
        candidates = attributes.findall('v:attribute', ns) or attributes.findall('attribute')
        for attr in candidates:
            if attr.get('name') == 'initialSoc':
                soc_attr = attr
                break
        if soc_attr is None:
            soc_attr = ET.SubElement(
                attributes,
                f'{{{MATSIM_NS}}}attribute',
                {'name': 'initialSoc', 'class': 'java.lang.Double'},
            )

        if i in full_indices:
            soc_attr.text = '1.0'
            n_modified_full += 1
        else:
            soc = sample_soc(bin_lowers, bin_width, weights)
            soc_attr.text = f'{soc:.4f}'
            n_modified_varied += 1
            bin_idx = min(int(soc / bin_width), len(bin_hit_counts) - 1)
            bin_hit_counts[bin_idx] += 1

    # --- Report ---
    print(f"  Full (SoC=1.0)  : {n_modified_full:>6}  ({100 * n_modified_full / n_total:.1f}%)")
    print(f"  Varied (< 1.0)  : {n_modified_varied:>6}  ({100 * n_modified_varied / n_total:.1f}%)")
    for lower, count in zip(bin_lowers, bin_hit_counts):
        upper = min(lower + bin_width, 1.0)
        print(f"    [{lower:.2f}, {upper:.2f})  :  {count:>6} vehicles")

    # --- Write output ---
    tree.write(str(output_path), encoding='UTF-8', xml_declaration=True)
    print(f"\nOutput written to: {output_path}")


if __name__ == '__main__':
    main()
