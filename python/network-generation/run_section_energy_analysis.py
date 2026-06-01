# -*- coding: utf-8 -*-
"""
Orchestration script: Generate section network variants at different link lengths,
run full MATSim simulations via RunSectionScenario, parse resistance_debug.csv,
and produce comparison plots including a flat base-case reference.

Usage:
    python run_section_energy_analysis.py [--jar <path>] [--skip-generation] [--variants-dir <path>]
"""

import argparse
import gzip
import shutil
import subprocess
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ==============================
# Configuration
# ==============================
_SCRIPT_DIR = Path(__file__).parent

# Path to MATSim JAR
DEFAULT_JAR = _SCRIPT_DIR / ".." / ".." / "matsim-example-project-0.0.1-SNAPSHOT.jar"

# Section input directory (fine 50m sections)
DEFAULT_SECTIONS_DIR = r"data\sections_quantile_run_20260307_090732"

# Link lengths to test
LINK_LENGTHS = [50, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000]

# Vehicle parameters for the two loading scenarios
VEHICLE_PARAMS = {
    "empty": {
        "mass": 12000,     # [kg] tare weight
        "payload": 0,      # [kg]
        "cdXA": 5.0,       # [m^2]
        "rollingC": 0.005, # [-]
        "maxMotorPower": 400000,  # [W]
        "maxSpeed": 25.0,  # [m/s] = 90 km/h
    },
    "loaded": {
        "mass": 12000,     # [kg] tare weight
        "payload": 25000,  # [kg] full payload
        "cdXA": 5.0,       # [m^2]
        "rollingC": 0.005, # [-]
        "maxMotorPower": 400000,  # [W]
        "maxSpeed": 25.0,  # [m/s] = 90 km/h
    },
}

# Sections to simulate (Q50 dropped, flat + Q75 + Q97)
SECTIONS = ["q75", "q97"]

# Plot styling
SECTION_COLORS = {"flat": "#757575", "q75": "#FF9800", "q97": "#E53935"}
SECTION_LABELS = {"flat": "Flat (no grade)", "q75": "Q75 (medium)", "q97": "Q97 (hilly)"}
SECTION_LINESTYLES_LOADED = {"flat": "-", "q75": "-", "q97": "-"}
SECTION_LINESTYLES_EMPTY = {"flat": "--", "q75": "--", "q97": "--"}

# Flat network parameters
FLAT_TOTAL_LENGTH_M = 100_000  # 100 km
FLAT_FREESPEED_MS = 22.22      # 80 km/h
FLAT_CAPACITY = 2000.0
FLAT_LANES = 2.0


def generate_flat_network(max_link_length, output_path):
    """
    Generate a synthetic flat MATSim network (all nodes at z=0).

    Creates a straight 100 km path with uniform link lengths.
    """
    num_links = int(np.ceil(FLAT_TOTAL_LENGTH_M / max_link_length))
    actual_link_length = FLAT_TOTAL_LENGTH_M / num_links

    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">')
    lines.append('<network>')
    lines.append('  <nodes>')

    # Nodes along x-axis, y=0, z=0
    for i in range(num_links + 1):
        x = i * actual_link_length
        lines.append(f'    <node id="{i}" x="{x:.1f}" y="0.0" z="0.0"/>')

    lines.append('  </nodes>')
    lines.append('  <links>')

    for i in range(num_links):
        lines.append(
            f'    <link id="{i}" from="{i}" to="{i+1}" '
            f'length="{actual_link_length:.2f}" '
            f'freespeed="{FLAT_FREESPEED_MS}" '
            f'capacity="{FLAT_CAPACITY:.0f}" '
            f'permlanes="{FLAT_LANES:.0f}" '
            f'modes="car"/>'
        )

    lines.append('  </links>')
    lines.append('</network>')

    xml_content = "\n".join(lines)

    # Write gzipped
    output_path = Path(output_path)
    if str(output_path).endswith(".gz"):
        with gzip.open(output_path, "wt", encoding="utf-8") as f:
            f.write(xml_content)
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(xml_content)

    return output_path


def run_section_scenario(jar_path, network_path, output_dir, vehicle_params):
    """Run the Java RunSectionScenario as a subprocess and parse results."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "java", "-cp", str(jar_path),
        "org.matsim.mpm.run.RunSectionScenario",
        "--network", str(network_path),
        "--mass", str(vehicle_params["mass"]),
        "--payload", str(vehicle_params["payload"]),
        "--cdXA", str(vehicle_params["cdXA"]),
        "--rollingC", str(vehicle_params["rollingC"]),
        "--maxMotorPower", str(vehicle_params["maxMotorPower"]),
        "--maxSpeed", str(vehicle_params["maxSpeed"]),
        "--output-dir", str(output_dir),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"  ERROR running Java tool:")
        print(f"  stdout: {result.stdout[-500:]}")
        print(f"  stderr: {result.stderr[-500:]}")
        return None

    return parse_resistance_debug(output_dir)


def parse_resistance_debug(output_dir):
    """Parse resistance_debug.csv from MATSim output and compute summary."""
    output_dir = Path(output_dir)
    debug_csv = output_dir / "resistance_debug.csv"

    if not debug_csv.exists():
        print(f"  WARNING: resistance_debug.csv not found in {output_dir}")
        return None

    df = pd.read_csv(debug_csv)

    total_length_m = df["length_m"].sum()
    total_energy_wh = df["energy_Wh"].sum()

    if total_length_m > 0:
        kwh_per_km = (total_energy_wh / 1000.0) / (total_length_m / 1000.0)
    else:
        kwh_per_km = 0.0

    return {
        "total_length_m": total_length_m,
        "total_energy_Wh": total_energy_wh,
        "kWh_per_km": kwh_per_km,
    }


def generate_plot(results_df, output_dir):
    """Generate the kWh/km vs max_link_length plot."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 6))

    all_sections = ["flat"] + SECTIONS

    for section in all_sections:
        color = SECTION_COLORS[section]
        label = SECTION_LABELS[section]

        for loading, ls_label in [("loaded", "loaded"), ("empty", "empty")]:
            if loading == "loaded":
                linestyle = SECTION_LINESTYLES_LOADED[section]
            else:
                linestyle = SECTION_LINESTYLES_EMPTY[section]

            mask = (results_df["section"] == section) & (results_df["loading"] == loading)
            subset = results_df[mask].sort_values("max_link_length")

            if subset.empty:
                continue

            marker = "o" if section != "flat" else "s"

            ax.plot(
                subset["max_link_length"],
                subset["kWh_per_km"],
                color=color,
                linestyle=linestyle,
                marker=marker,
                markersize=4,
                linewidth=1.5,
                label=f"{label} ({ls_label})",
            )

    ax.set_xscale("log")
    ax.set_xlabel("Max. allowed link length [m]", fontsize=12)
    ax.set_ylabel("Energy consumption [kWh/km]", fontsize=12)
    ax.set_title("BET Energy Consumption vs. Network Resolution", fontsize=14)
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.3, which="both")
    ax.set_xticks(LINK_LENGTHS)
    ax.set_xticklabels([str(x) for x in LINK_LENGTHS], rotation=45, fontsize=8)

    fig.tight_layout()

    # Save as PDF and PNG
    pdf_path = output_dir / "energy_vs_link_length.pdf"
    png_path = output_dir / "energy_vs_link_length.png"
    fig.savefig(pdf_path, dpi=300, bbox_inches="tight")
    fig.savefig(png_path, dpi=300, bbox_inches="tight")
    plt.close(fig)

    print(f"\nPlot saved to:")
    print(f"  {pdf_path}")
    print(f"  {png_path}")


def main():
    parser = argparse.ArgumentParser(description="Run section energy analysis across link lengths.")
    parser.add_argument("--jar", type=str, default=str(DEFAULT_JAR),
                        help="Path to MATSim JAR file")
    parser.add_argument("--variants-dir", type=str, default=None,
                        help="Path to pre-generated variants directory (skip generation)")
    parser.add_argument("--sections-dir", type=str, default=DEFAULT_SECTIONS_DIR,
                        help="Directory with section_q*.xml.gz files")
    parser.add_argument("--skip-generation", action="store_true",
                        help="Skip network generation step (use existing variants)")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Output directory for results")
    args = parser.parse_args()

    jar_path = Path(args.jar)
    if not jar_path.exists():
        print(f"ERROR: JAR file not found: {jar_path}")
        print(f"Build it first: mvnw.cmd clean package -DskipTests")
        sys.exit(1)

    # --- Step 1: Generate network variants ---
    if args.skip_generation and args.variants_dir:
        variants_dir = Path(args.variants_dir)
        if not variants_dir.is_absolute():
            variants_dir = _SCRIPT_DIR / variants_dir
        print(f"Using existing variants from: {variants_dir}")
    else:
        print("=" * 60)
        print("Step 1: Generating network variants")
        print("=" * 60)

        from generate_section_link_length_variants import main as gen_main
        # Temporarily override sys.argv for the generation script
        old_argv = sys.argv
        gen_args = ["generate_section_link_length_variants.py", "--sections-dir", args.sections_dir]
        if args.output_dir:
            gen_args.extend(["--output-dir", args.output_dir])
        sys.argv = gen_args
        variants_dir_str = gen_main()
        sys.argv = old_argv
        variants_dir = Path(variants_dir_str)

    # --- Step 2: Setup output ---
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = variants_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    sim_results_dir = output_dir / "sim_results"
    sim_results_dir.mkdir(parents=True, exist_ok=True)

    flat_networks_dir = output_dir / "flat_networks"
    flat_networks_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 3: Generate flat base-case networks ---
    print("\n" + "=" * 60)
    print("Step 2: Generating flat base-case networks")
    print("=" * 60)

    for max_len in LINK_LENGTHS:
        flat_net_path = flat_networks_dir / f"flat_{max_len}m.xml.gz"
        if not flat_net_path.exists():
            generate_flat_network(max_len, flat_net_path)
            print(f"  Generated: {flat_net_path.name}")
        else:
            print(f"  Exists:    {flat_net_path.name}")

    # --- Step 4: Run energy simulations ---
    print("\n" + "=" * 60)
    print("Step 3: Running MATSim simulations")
    print("=" * 60)

    all_results = []

    # --- Flat base-case ---
    for max_len in LINK_LENGTHS:
        flat_net_path = flat_networks_dir / f"flat_{max_len}m.xml.gz"
        if not flat_net_path.exists():
            print(f"  SKIP flat/{max_len}m: network not found")
            continue

        for loading, vparams in VEHICLE_PARAMS.items():
            run_dir = sim_results_dir / f"flat_{max_len}m_{loading}"
            print(f"  Simulating: flat / {max_len}m / {loading}...", end=" ", flush=True)

            result = run_section_scenario(jar_path, flat_net_path, run_dir, vparams)

            if result:
                print(f"kWh/km = {result['kWh_per_km']:.4f}")
                all_results.append({
                    "section": "flat",
                    "max_link_length": max_len,
                    "loading": loading,
                    "total_length_m": result["total_length_m"],
                    "total_energy_Wh": result["total_energy_Wh"],
                    "kWh_per_km": result["kWh_per_km"],
                })
            else:
                print("FAILED")

    # --- Section networks (Q75, Q97) ---
    for section in SECTIONS:
        for max_len in LINK_LENGTHS:
            network_file = variants_dir / f"section_{section}_{max_len}m.xml.gz"
            if not network_file.exists():
                print(f"  SKIP: {network_file.name} not found")
                continue

            for loading, vparams in VEHICLE_PARAMS.items():
                run_dir = sim_results_dir / f"{section}_{max_len}m_{loading}"
                print(f"  Simulating: {section} / {max_len}m / {loading}...", end=" ", flush=True)

                result = run_section_scenario(jar_path, network_file, run_dir, vparams)

                if result:
                    print(f"kWh/km = {result['kWh_per_km']:.4f}")
                    all_results.append({
                        "section": section,
                        "max_link_length": max_len,
                        "loading": loading,
                        "total_length_m": result["total_length_m"],
                        "total_energy_Wh": result["total_energy_Wh"],
                        "kWh_per_km": result["kWh_per_km"],
                    })
                else:
                    print("FAILED")

    if not all_results:
        print("\nERROR: No energy results computed. Cannot generate plot.")
        sys.exit(1)

    # --- Step 5: Save results and plot ---
    print("\n" + "=" * 60)
    print("Step 4: Generating plots")
    print("=" * 60)

    results_df = pd.DataFrame(all_results)
    results_csv = output_dir / "energy_results_summary.csv"
    results_df.to_csv(results_csv, index=False)
    print(f"\nResults saved to: {results_csv}")

    generate_plot(results_df, output_dir)

    print(f"\nDone! All outputs in: {output_dir}")


if __name__ == "__main__":
    main()
