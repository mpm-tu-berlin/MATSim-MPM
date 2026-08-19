# -*- coding: utf-8 -*-
"""
Orchestration script: Generate section network variants at different link lengths,
run full MATSim simulations via RunSectionScenario, parse resistance_debug.csv,
and produce comparison plots including a flat base-case reference.

Runs both vehicle loading variants (empty + loaded) in a single MATSim process
and parallelises simulations across CPU cores.

Usage:
    python run_section_energy_analysis.py [--jar <path>] [--skip-generation] [--variants-dir <path>] [--workers N]
"""

import argparse
import csv
import gzip
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xml.etree.ElementTree as ET


def load_matsim_network(path):
    """Load nodes and links from a MATSim XML network (gzipped or plain)."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as f:
        tree = ET.parse(f)
    root = tree.getroot()

    nodes = {}
    for node in root.find("nodes").findall("node"):
        nid = node.get("id")
        x = float(node.get("x"))
        y = float(node.get("y"))
        z_attr = node.get("z")
        z = float(z_attr) if z_attr is not None else None
        nodes[nid] = {"x": x, "y": y, "z": z}

    links = {}
    for link in root.find("links").findall("link"):
        lid = link.get("id")
        links[lid] = {
            "id": lid,
            "from": link.get("from"),
            "to": link.get("to"),
            "length": float(link.get("length", "0")),
        }
    return nodes, links


def _import_variants_module():
    """Lazy import — pulls in geopandas/shapely only when network generation is requested."""
    from generate_section_link_length_variants import (
        generate_variants_for_section,
        SECTION_FILES,
        _import_script04,
        DTM_PATH,
        SIMPLIFIED_GPKG,
        DETAILED_GPKG,
    )
    return {
        "generate_variants_for_section": generate_variants_for_section,
        "SECTION_FILES": SECTION_FILES,
        "_import_script04": _import_script04,
        "DTM_PATH": DTM_PATH,
        "SIMPLIFIED_GPKG": SIMPLIFIED_GPKG,
        "DETAILED_GPKG": DETAILED_GPKG,
    }

# ==============================
# Configuration
# ==============================
_SCRIPT_DIR = Path(__file__).parent

# Path to MATSim JAR
DEFAULT_JAR = _SCRIPT_DIR / ".." / ".." / "matsim-example-project-0.0.1-SNAPSHOT.jar"

# Section input directory (Referenz-Sektionen der 20er-Auswahl im Netzgen-Worktree)
_NETGEN_DIR = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
# Run 182750 = kanonische Auswahl (Ketten-Export-Fix + Eindeutigkeits-Filter);
# 130433 hatte 9 defekte Exporte -> endpoints_from_reference wuerfe dort und die
# Sektion wuerde still uebersprungen. Endpunkte MUESSEN aus derselben Auswahl wie
# die Varianten kommen (q15/q20/q25 haben in 182750 andere Routen).
DEFAULT_SECTIONS_DIR = str(_NETGEN_DIR / "data" / "sections_quantile_run_20260706_182750")

# Link lengths to test — reduzierte 12er-Leiter (identisch zum Generator)
LINK_LENGTHS = [50, 100, 150, 200, 250, 300, 350, 400, 500, 600, 750, 1000]

# Vehicle parameters for the two loading scenarios.
# maxSpeed = 23.611 m/s = 85 km/h = VECTO-Vmax (2026-07-05: vorher 90 km/h ->
# verliess den Kalibrierbereich und hob den Flat-Case ~10 % ueber Realwerte).
# HINWEIS: cdXA/rollingC hier sind fuer den VERBRAUCH wirkungslos — der nutzt
# die CalibrationParams (.properties, CALIBRATION_PER_LOADING); Werte bleiben
# nur, weil die vehicles.csv die Spalten erwartet.
VEHICLE_PARAMS = {
    "empty": {
        "mass": 19000,     # [kg] tare weight
        "payload": 0,      # [kg]
        "cdXA": 5.0,       # [m^2] (unbenutzt, s. Hinweis)
        "rollingC": 0.0046, # [-]  (unbenutzt, s. Hinweis)
        "maxMotorPower": 500000,  # [W]
        "maxSpeed": 22.222,  # [m/s] = 80 km/h (dt. Lkw-Tempolimit; User 2026-07-06)
    },
    "loaded": {
        "mass": 19000,     # [kg] tare weight
        "payload": 21000,  # [kg] full payload
        "cdXA": 5.0,       # [m^2] (unbenutzt, s. Hinweis)
        "rollingC": 0.0046, # [-]  (unbenutzt, s. Hinweis)
        "maxMotorPower": 500000,  # [W]
        "maxSpeed": 22.222,  # [m/s] = 80 km/h (dt. Lkw-Tempolimit; User 2026-07-06)
    },
}

# Build the vehicle params list for multi-vehicle CSV mode
VEHICLE_PARAMS_LIST = [
    {"id": name, **params} for name, params in VEHICLE_PARAMS.items()
]

# 20-Sektionen-Studie (Auswahl 2026-07-06 auf V2, sigma_g-Quantile Q5..Q97)
SECTIONS = [f"q{q}" for q in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
                              55, 60, 65, 70, 75, 80, 85, 90, 95, 97)]

# Endpunkt-Koordinaten werden DYNAMISCH aus den Referenz-Sektionsnetzen gelesen
# (Grad-1-Knoten; ersetzt die alten hartkodierten q75/q97-Koordinaten).
def endpoints_from_reference(section_path):
    """((x1,y1),(x2,y2)) der beiden Grad-1-Knoten eines Referenz-Sektionsnetzes."""
    nodes, links = load_matsim_network(str(section_path))
    from collections import defaultdict
    nbrs = defaultdict(set)
    for lk in links.values():
        nbrs[lk["from"]].add(lk["to"])
        nbrs[lk["to"]].add(lk["from"])
    ends = [n for n, s in nbrs.items() if len(s) == 1]
    if len(ends) != 2:
        raise ValueError(f"{section_path}: {len(ends)} Endpunkte statt 2")
    return ((nodes[ends[0]]["x"], nodes[ends[0]]["y"]),
            (nodes[ends[1]]["x"], nodes[ends[1]]["y"]))

# Trims sind in der 20-Sektionen-Studie OBSOLET: Die Darstellung ist relativ zur
# 250-m-Stufe je Sektion/Beladung, und der Netto-Hoehenenergie-Bias (Grund des
# alten q97-Trims) ist aufloesungsUNabhaengig -> kuerzt sich exakt heraus.
SECTION_TRIM_KM = {}

# Plot styling — dynamisch fuer alle 20 Sektionen (Farbverlauf blau->rot nach Quantil)
def _quantile_color(i, n):
    import matplotlib.cm as cm
    return cm.get_cmap("coolwarm")(i / max(1, n - 1))

SECTION_COLORS = {"flat": "#757575"}
SECTION_LABELS = {"flat": "Flat (no grade)"}
SECTION_LINESTYLES_LOADED = {"flat": "-"}
SECTION_LINESTYLES_EMPTY = {"flat": "--"}
for _i, _s in enumerate(SECTIONS):
    SECTION_COLORS[_s] = _quantile_color(_i, len(SECTIONS))
    SECTION_LABELS[_s] = _s.upper()
    SECTION_LINESTYLES_LOADED[_s] = "-"
    SECTION_LINESTYLES_EMPTY[_s] = "--"

# Flat network parameters
FLAT_TOTAL_LENGTH_M = 100_000  # 100 km
FLAT_FREESPEED_MS = 22.222  # 80 km/h = dt. Lkw-Tempolimit (konsistent zu maxSpeed)
FLAT_CAPACITY = 2000.0
FLAT_LANES = 2.0

# QSim timestep [seconds] — passed to RunSectionScenario via --qsim-timestep
QSIM_TIMESTEP = 0.5

# Calibration parameters per loading — Kandidat RB (beta=0.9-Modellselektion,
# Run 20260818_120442_250m, je 500 Trials, f_rec=1.0 FIX, rho=1.188):
# empty -> RB/lh_low (RMSE 0,55 %), loaded -> RB/lh_high (RMSE 0,87 %).
# User-Entscheid Option 1 (2026-08-18); auxPowerW war fix bei 4000 W.
CALIBRATION_PER_LOADING = {
    "empty": {
        "tractionEfficiency": 0.8046992668164625,
        "inertiaC": 1.0188754610733795,
        "recupEfficiency": 0.5383179587931889,
        "maxRecupPowerFraction": 1.0,
        "auxPowerW": 4000.0,
        "cdXA": 5.686050717123754,
        "rollingC": 0.004664536015249529,
        "rollingLoadExponent": 0.9,
        "rollingRefMassKg": 35500.0,
        "airDensity": 1.188,
    },
    "loaded": {
        "tractionEfficiency": 0.8484617852034014,
        "inertiaC": 1.0139332490424946,
        "recupEfficiency": 0.4892728695571761,
        "maxRecupPowerFraction": 1.0,
        "auxPowerW": 4000.0,
        "cdXA": 5.797243132601137,
        "rollingC": 0.004922872982934466,
        "rollingLoadExponent": 0.9,
        "rollingRefMassKg": 35500.0,
        "airDensity": 1.188,
    },
}
# Backwards-compatible default (used only if a caller still wants a single set).
CALIBRATION_DEFAULTS = CALIBRATION_PER_LOADING["loaded"]


def _params_cache_path(run_dir):
    """Path to the parameter cache file in a simulation run directory."""
    return Path(run_dir) / ".sim_params.json"


def _jar_fingerprint(jar_path):
    """Groesse+mtime des Simulations-JARs: Modellaenderungen invalidieren den Cache.
    (Befund 2026-08-18: ein Lauf vor dem JAR-Rebuild cachte neue Parameter mit
    ALTEN Ergebnissen; der Folgelauf traf den Cache und rechnete nie.)"""
    try:
        st = Path(jar_path).stat()
        return [st.st_size, int(st.st_mtime)]
    except OSError:
        return None


def _is_cache_valid(run_dir, vehicle_params_list, calibration_params=None, qsim_timestep=None,
                    jar_path=None):
    """Check whether cached results match the current parameters."""
    cache_file = _params_cache_path(run_dir)
    debug_csv = Path(run_dir) / "resistance_debug.csv"
    if not debug_csv.exists() or not cache_file.exists():
        return False
    try:
        with open(cache_file) as f:
            cached = json.load(f)
        current = {
            "vehicles": vehicle_params_list,
            "calibration": calibration_params or CALIBRATION_DEFAULTS,
            "qsim_timestep": qsim_timestep if qsim_timestep is not None else QSIM_TIMESTEP,
        }
        if jar_path is not None:
            current["jar"] = _jar_fingerprint(jar_path)
        return cached == current
    except Exception:
        return False


def _write_params_cache(run_dir, vehicle_params_list, calibration_params=None, qsim_timestep=None,
                        jar_path=None):
    """Write current parameters as a cache key."""
    cache_file = _params_cache_path(run_dir)
    current = {
        "vehicles": vehicle_params_list,
        "calibration": calibration_params or CALIBRATION_DEFAULTS,
        "qsim_timestep": qsim_timestep if qsim_timestep is not None else QSIM_TIMESTEP,
    }
    if jar_path is not None:
        current["jar"] = _jar_fingerprint(jar_path)
    with open(cache_file, "w") as f:
        json.dump(current, f, indent=2)


def _flat_params_valid(flat_net_path):
    """Check whether a flat network was generated with the current parameters."""
    params_file = flat_net_path.with_suffix(".params.json")
    if not params_file.exists():
        return False
    try:
        with open(params_file) as f:
            cached = json.load(f)
        return (cached.get("freespeed") == FLAT_FREESPEED_MS
                and cached.get("total_length") == FLAT_TOTAL_LENGTH_M)
    except Exception:
        return False


def write_calibration_properties(params, output_path):
    """Write a Java .properties file for CalibrationParams."""
    output_path = Path(output_path)
    with open(output_path, "w", encoding="utf-8") as f:
        for key, value in params.items():
            f.write(f"{key}={value}\n")
    return output_path


def _write_vehicles_csv(vehicle_params_list, output_path):
    """Write a vehicles CSV file for RunSectionScenario --vehicles-csv."""
    output_path = Path(output_path)
    fieldnames = ["id", "mass", "payload", "cdXA", "rollingC", "maxMotorPower", "maxSpeed"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for vp in vehicle_params_list:
            writer.writerow({k: vp[k] for k in fieldnames})
    return output_path


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


def get_forward_links_per_vehicle(network_path, debug_csv_path):
    """
    Determine forward-direction link IDs by walking the resistance_debug.csv
    in traversal order per vehicle.

    For each vehicle, walks through its links sequentially, tracking visited
    nodes. When a node appears a second time (i.e. both from_node and to_node
    of a link are already visited), the route has turned around — stop collecting.

    Args:
        network_path: Path to the variant network XML (.xml.gz)
        debug_csv_path: Path to resistance_debug.csv

    Returns:
        dict mapping vehicleId to ORDERED list of forward link IDs
        (driving order — needed for cumulative-distance trimming).
    """
    _, links = load_matsim_network(str(network_path))
    link_nodes = {lid: (lk["from"], lk["to"]) for lid, lk in links.items()}

    df = pd.read_csv(debug_csv_path)
    result = {}

    if "vehicleId" in df.columns:
        groups = df.groupby("vehicleId", sort=False)
    else:
        groups = [("truck_1", df)]

    for vehicle_id, vdf in groups:
        visited_nodes = set()
        forward_ids = []  # ordered
        for _, row in vdf.iterrows():
            lid = str(row["linkId"])
            if lid not in link_nodes:
                continue
            from_node, to_node = link_nodes[lid]
            # Both nodes already visited means the route turned around
            if from_node in visited_nodes and to_node in visited_nodes:
                break
            visited_nodes.add(from_node)
            visited_nodes.add(to_node)
            forward_ids.append(lid)
        result[vehicle_id] = forward_ids

    return result


def run_section_scenario(jar_path, network_path, output_dir, vehicle_params_list,
                         calibration_params=None, from_coord=None, to_coord=None,
                         qsim_timestep=None, section=None):
    """
    Run the Java RunSectionScenario as a subprocess with multiple vehicles.

    Writes a vehicles.csv and uses --vehicles-csv for multi-vehicle mode.
    Optionally passes --from-coord / --to-coord (as "x,y" strings) for reliable
    nearest-node endpoint resolution.
    Returns a dict mapping vehicle IDs to their energy summaries.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write calibration .properties file so CalibrationParams.loadOrDefault() picks it up
    calib = calibration_params if calibration_params else CALIBRATION_DEFAULTS
    calib_file = output_dir / "calibration_params.properties"
    write_calibration_properties(calib, calib_file)

    # Write vehicles CSV
    vehicles_csv = output_dir / "vehicles.csv"
    _write_vehicles_csv(vehicle_params_list, vehicles_csv)

    cmd = [
        "java",
        # Heap je Sim bounden: ein 100-km/2000-Link-Netz mit wenigen Fahrzeugen
        # braucht <1 GB; ohne -Xmx nimmt die JVM 25 % RAM (24 GB) und viele
        # parallele Worker sprengen die freien 48 GB (User-HW 96 GB gesamt).
        "-Xmx2g",
        f"-Dcalibration.params.file={calib_file}",
        "-cp", str(jar_path),
        "org.matsim.mpm.run.RunSectionScenario",
        "--network", str(network_path),
        "--vehicles-csv", str(vehicles_csv),
        "--output-dir", str(output_dir),
    ]
    if from_coord:
        cmd.extend(["--from-coord", from_coord])
    if to_coord:
        cmd.extend(["--to-coord", to_coord])
    if qsim_timestep is not None:
        cmd.extend(["--qsim-timestep", str(qsim_timestep)])

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)

    # Write combined output to log file AFTER subprocess exits (avoids Windows file lock)
    log_file = output_dir / "simulation.log"
    output_dir.mkdir(parents=True, exist_ok=True)  # MATSim may have re-created the dir
    log_file.write_text(result.stdout or "", errors="replace")

    if result.returncode != 0:
        err_lines = []
        if log_file.exists():
            text = log_file.read_text(errors="replace")
            # Show last 2000 chars which typically contain the exception
            if len(text) > 2000:
                err_lines.append(text[-2000:])
            else:
                err_lines.append(text)
        return None, f"ERROR (see {log_file}):\n" + "\n".join(err_lines)

    return parse_resistance_debug(output_dir, network_path=network_path, section=section), None


def parse_resistance_debug(output_dir, network_path=None, section=None):
    """
    Parse resistance_debug.csv from MATSim output and compute per-vehicle summary.

    If network_path is provided, uses node-based forward filtering: for each vehicle,
    walks through links in traversal order and stops when a node is visited twice
    (= route turned around). This filters out reverse-direction links in section networks.

    If section is provided AND SECTION_TRIM_KM has an entry for it, the forward-driven
    path is additionally trimmed to the [trim_min_km, trim_max_km] range along cumulative
    distance. The trimmed length is what divides energy in kWh/km.

    Args:
        output_dir: Directory containing resistance_debug.csv
        network_path: If provided, path to the network XML for node-based forward filtering.
        section: Section label (e.g. "q97") for looking up SECTION_TRIM_KM.

    Returns a dict mapping vehicle IDs (e.g. "truck_empty") to their energy summary,
    or a single-entry dict with key "truck_1" for legacy single-vehicle results.
    """
    output_dir = Path(output_dir)
    debug_csv = output_dir / "resistance_debug.csv"

    if not debug_csv.exists():
        return None

    # Compute per-vehicle forward link IDs (ordered) using node-based filtering
    forward_links_per_vehicle = None
    if network_path is not None:
        forward_links_per_vehicle = get_forward_links_per_vehicle(network_path, debug_csv)

    trim_range = SECTION_TRIM_KM.get(section) if section else None

    df = pd.read_csv(debug_csv)
    df["linkId"] = df["linkId"].astype(str)

    if "vehicleId" in df.columns:
        groups = df.groupby("vehicleId")
    else:
        groups = [("truck_1", df)]

    results = {}
    for vehicle_id, vdf in groups:
        if forward_links_per_vehicle is not None and vehicle_id in forward_links_per_vehicle:
            forward_ids = forward_links_per_vehicle[vehicle_id]
            # Project the per-vehicle rows onto the ordered forward path so the cumulative
            # distance reflects actual driving order. linkId is unique per network, so
            # set_index/reindex is safe.
            vdf = vdf.set_index("linkId").reindex(forward_ids).dropna(how="all").reset_index()
            if trim_range is not None and not vdf.empty:
                trim_min_m, trim_max_m = trim_range[0] * 1000.0, trim_range[1] * 1000.0
                # Cumulative distance up to the END of each link
                cum_end = vdf["length_m"].cumsum()
                cum_start = cum_end - vdf["length_m"]
                # Keep links whose ENTIRE span lies within [trim_min, trim_max]
                mask = (cum_start >= trim_min_m) & (cum_end <= trim_max_m)
                vdf = vdf[mask]

        total_length_m = float(vdf["length_m"].sum())
        total_energy_wh = float(vdf["energy_Wh"].sum())
        if total_length_m > 0:
            kwh_per_km = (total_energy_wh / 1000.0) / (total_length_m / 1000.0)
        else:
            kwh_per_km = 0.0
        result = {
            "total_length_m": total_length_m,
            "total_energy_Wh": total_energy_wh,
            "kWh_per_km": kwh_per_km,
        }

        if "brakeLossResist_Wh" in vdf.columns:
            result["brakeLossResist_Wh"] = float(vdf["brakeLossResist_Wh"].sum())
        if "brakeLossKinHyp_Wh" in vdf.columns:
            result["brakeLossKinHyp_Wh"] = float(vdf["brakeLossKinHyp_Wh"].sum())

        results[vehicle_id] = result

    return results


def _run_one_simulation(task):
    """
    Run a single simulation task. Designed to be called from ProcessPoolExecutor.

    Each task runs ONE vehicle (one loading) with its own calibration parameter set,
    so that empty and loaded variants can use independently calibrated parameters.

    Args:
        task: tuple of (section, max_len, loading, network_path, run_dir,
                        vehicle_params_list, jar_path, calibration_params,
                        from_coord, to_coord, qsim_timestep)

    Returns:
        (section, max_len, loading, results_dict_or_None, log_messages)
    """
    (section, max_len, loading, network_path, run_dir, vehicle_params_list,
     jar_path, calibration_params, from_coord, to_coord, qsim_timestep) = task
    log = []

    run_dir = Path(run_dir)

    if _is_cache_valid(run_dir, vehicle_params_list, calibration_params, qsim_timestep,
                       jar_path=jar_path):
        log.append(f"  Cached:     {section} / {max_len}m / {loading}")
        results = parse_resistance_debug(run_dir, network_path=network_path, section=section)
    else:
        log.append(f"  Simulating: {section} / {max_len}m / {loading}")
        results, err = run_section_scenario(
            jar_path, network_path, run_dir, vehicle_params_list, calibration_params,
            from_coord=from_coord, to_coord=to_coord, qsim_timestep=qsim_timestep,
            section=section,
        )
        if results:
            _write_params_cache(run_dir, vehicle_params_list, calibration_params, qsim_timestep,
                                jar_path=jar_path)
        else:
            log.append(f"  FAILED: {section} / {max_len}m / {loading}" + (f" ({err})" if err else ""))

        # Extract debug lines from simulation log
        sim_log = run_dir / "simulation.log"
        if sim_log.exists():
            for line in sim_log.read_text(errors="replace").splitlines():
                if "[RunSectionScenario]" in line:
                    log.append(f"    {line.strip()}")

    if results:
        for vid, r in results.items():
            log.append(f"    {vid}: kWh/km = {r['kWh_per_km']:.4f}")

    return section, max_len, loading, results, log


def _plot_energy_vs_link_length(results_df, ax, x_scale="log"):
    """Plot kWh/km vs max_link_length on the given axes."""
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

    if x_scale == "log":
        ax.set_xscale("log")
    ax.set_xlabel("Max. allowed link length [m]", fontsize=12)
    ax.set_ylabel("Energy consumption [kWh/km]", fontsize=12)
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.3, which="both")
    ax.set_xticks(LINK_LENGTHS)
    ax.set_xticklabels([str(x) for x in LINK_LENGTHS], rotation=45, fontsize=8)


def generate_plot(results_df, output_dir):
    """Generate kWh/km vs max_link_length plots (log and linear x-axis)."""
    for scale, suffix in [("log", ""), ("linear", "_linear")]:
        fig, ax = plt.subplots(1, 1, figsize=(10, 6))
        _plot_energy_vs_link_length(results_df, ax, x_scale=scale)
        ax.set_title(
            f"BET Energy Consumption vs. Network Resolution ({scale} scale)",
            fontsize=14,
        )
        fig.tight_layout()

        pdf_path = output_dir / f"energy_vs_link_length{suffix}.pdf"
        png_path = output_dir / f"energy_vs_link_length{suffix}.png"
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
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel simulation workers (default: cpu_count // 2)")
    args = parser.parse_args()

    jar_path = Path(args.jar).resolve()
    if not jar_path.exists():
        print(f"ERROR: JAR file not found: {jar_path}")
        print(f"Build it first: mvnw.cmd clean package -DskipTests")
        sys.exit(1)

    max_workers = args.workers or max(1, os.cpu_count() // 2)
    print(f"Using {max_workers} parallel workers")

    # --- Step 1: Generate network variants (parallel across sections) ---
    if args.skip_generation and args.variants_dir:
        variants_dir = Path(args.variants_dir)
        if not variants_dir.is_absolute():
            variants_dir = _SCRIPT_DIR / variants_dir
        print(f"Using existing variants from: {variants_dir}")
    else:
        print("=" * 60)
        print("Step 1: Generating network variants (parallel)")
        print("=" * 60)

        variants_output = args.output_dir if args.output_dir else str(
            _SCRIPT_DIR / "data" / f"section_variants_{Path(args.sections_dir).name}"
        )
        variants_dir = Path(variants_output)
        variants_dir.mkdir(parents=True, exist_ok=True)

        sections_dir = Path(args.sections_dir)
        if not sections_dir.is_absolute():
            sections_dir = _SCRIPT_DIR / sections_dir

        # Lazy-import the variant-generation pipeline (pulls geopandas etc.)
        _gen = _import_variants_module()

        # Build list of sections that actually need generation
        section_tasks = []
        for label, filename in _gen["SECTION_FILES"].items():
            section_path = sections_dir / filename
            if not section_path.exists():
                print(f"\nWARNING: Section file not found: {section_path}. Skipping.")
                continue
            # Check which link lengths are missing for this section
            missing = [ll for ll in LINK_LENGTHS
                       if not (variants_dir / f"section_{label}_{ll}m.xml.gz").exists()]
            if missing:
                section_tasks.append((label, str(section_path), missing))
            else:
                print(f"  All variants for {label} already exist — skipping.")

        if section_tasks:
            # Only load heavy data when there is actual work to do
            print("\nLoading script 04 module...")
            script04 = _gen["_import_script04"]()

            print("Loading DTM...")
            dtm = script04.load_dtm(str(_gen["DTM_PATH"]))

            print("Loading simplified gpkg...")
            gdf_nodes_simplified, gdf_edges_simplified = script04.load_local_osm_file(str(_gen["SIMPLIFIED_GPKG"]))

            print("Loading detailed gpkg...")
            gdf_nodes_detailed, gdf_edges_detailed = script04.load_local_osm_file(str(_gen["DETAILED_GPKG"]))

            # Generate variants in parallel (one thread per section)
            all_gen_results = []
            gen_workers = min(len(section_tasks), max_workers)
            missing_total = sum(len(m) for _, _, m in section_tasks)
            print(f"\nGenerating {missing_total} missing variants across "
                  f"{len(section_tasks)} sections with {gen_workers} parallel workers")

            with ThreadPoolExecutor(max_workers=gen_workers) as pool:
                futures = {
                    pool.submit(
                        _gen["generate_variants_for_section"],
                        section_label=label,
                        section_path=spath,
                        link_lengths=missing_lengths,
                        dtm=dtm,
                        script04=script04,
                        gdf_nodes_simplified=gdf_nodes_simplified,
                        gdf_edges_simplified=gdf_edges_simplified,
                        gdf_nodes_detailed=gdf_nodes_detailed,
                        gdf_edges_detailed=gdf_edges_detailed,
                        output_dir=variants_dir,
                    ): label
                    for label, spath, missing_lengths in section_tasks
                }
                for future in as_completed(futures):
                    label = futures[future]
                    try:
                        results = future.result()
                        all_gen_results.extend(results)
                    except Exception as e:
                        print(f"\nERROR generating section {label}: {e}")

            if all_gen_results:
                summary_df = pd.DataFrame(all_gen_results)
                summary_path = variants_dir / "variants_summary.csv"
                summary_df.to_csv(summary_path, index=False)
                print(f"\nSummary saved to: {summary_path}")

            print(f"\nDone! Generated {len(all_gen_results)} network variants in {variants_dir}")
        else:
            print("\n  All network variants already exist — nothing to generate.")

    # --- Step 2: Setup output ---
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = variants_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    sim_results_dir = output_dir / "sim_results"
    sim_results_dir.mkdir(parents=True, exist_ok=True)

    # Look for flat networks alongside the section variants first; only fall back
    # to a private flat_networks/ subdir if they are not provided next to them.
    if (variants_dir / "flat_50m.xml.gz").exists():
        flat_networks_dir = variants_dir
    else:
        flat_networks_dir = output_dir / "flat_networks"
        flat_networks_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 3: Generate flat base-case networks ---
    print("\n" + "=" * 60)
    print("Step 2: Generating flat base-case networks")
    print("=" * 60)

    for max_len in LINK_LENGTHS:
        flat_net_path = flat_networks_dir / f"flat_{max_len}m.xml.gz"
        if not flat_net_path.exists() or not _flat_params_valid(flat_net_path):
            generate_flat_network(max_len, flat_net_path)
            with open(flat_net_path.with_suffix(".params.json"), "w") as f:
                json.dump({"freespeed": FLAT_FREESPEED_MS, "total_length": FLAT_TOTAL_LENGTH_M}, f)
            print(f"  Generated: {flat_net_path.name}")
        else:
            print(f"  Exists:    {flat_net_path.name}")

    # --- Step 4: Run energy simulations (parallel) ---
    print("\n" + "=" * 60)
    print("Step 3: Running MATSim simulations")
    print("=" * 60)

    # Collect all simulation tasks. Each task = one (section, max_len, loading)
    # so empty/loaded vehicles get their own calibration set in separate Java runs.
    tasks = []

    def _vparams_for(loading):
        return [{"id": f"truck_{loading}", **VEHICLE_PARAMS[loading]}]

    # Flat base-case tasks (per loading)
    for max_len in LINK_LENGTHS:
        flat_net_path = flat_networks_dir / f"flat_{max_len}m.xml.gz"
        if not flat_net_path.exists():
            print(f"  SKIP flat/{max_len}m: network not found")
            continue
        for loading in ("empty", "loaded"):
            run_dir = sim_results_dir / f"flat_{max_len}m_{loading}"
            tasks.append(("flat", max_len, loading, str(flat_net_path), str(run_dir),
                           _vparams_for(loading), str(jar_path),
                           CALIBRATION_PER_LOADING[loading], None, None, QSIM_TIMESTEP))

    # Section network tasks — Endpunkte dynamisch aus den Referenz-Sektionen
    ref_dir = Path(args.sections_dir)
    if not ref_dir.is_absolute():
        ref_dir = _SCRIPT_DIR / ref_dir
    for section in SECTIONS:
        ref_file = ref_dir / f"section_{section}_100km.xml.gz"
        try:
            coords = endpoints_from_reference(ref_file)
        except Exception as e:
            print(f"  WARNING: Endpunkte fuer {section} nicht bestimmbar ({e}) — skip")
            continue
        from_coord = f"{coords[0][0]},{coords[0][1]}" if coords else None
        to_coord = f"{coords[1][0]},{coords[1][1]}" if coords else None
        for max_len in LINK_LENGTHS:
            network_file = variants_dir / f"section_{section}_{max_len}m.xml.gz"
            if not network_file.exists():
                print(f"  SKIP: {network_file.name} not found")
                continue
            for loading in ("empty", "loaded"):
                run_dir = sim_results_dir / f"{section}_{max_len}m_{loading}"
                tasks.append((section, max_len, loading, str(network_file), str(run_dir),
                               _vparams_for(loading), str(jar_path),
                               CALIBRATION_PER_LOADING[loading], from_coord, to_coord, QSIM_TIMESTEP))

    print(f"\n  {len(tasks)} simulations to run ({max_workers} workers)\n")

    # Run simulations in parallel.
    # ThreadPool statt ProcessPool: MATSim laeuft ohnehin als java-Subprozess
    # (GIL frei); Spawn-Worker haengen unter Py3.14/Windows sporadisch am
    # numpy-Import (blas_fpe_check, 2x 2026-07-05/06).
    all_results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one_simulation, t): t for t in tasks}
        for future in as_completed(futures):
            section, max_len, loading, results, log_messages = future.result()
            for msg in log_messages:
                print(msg)

            if results:
                # Per-loading task: results dict has exactly one vehicle entry
                for vehicle_id, r in results.items():
                    row = {
                        "section": section,
                        "max_link_length": max_len,
                        "loading": loading,
                        "total_length_m": r["total_length_m"],
                        "total_energy_Wh": r["total_energy_Wh"],
                        "kWh_per_km": r["kWh_per_km"],
                    }
                    if "brakeLossResist_Wh" in r:
                        row["brakeLossResist_Wh"] = r["brakeLossResist_Wh"]
                    if "brakeLossKinHyp_Wh" in r:
                        row["brakeLossKinHyp_Wh"] = r["brakeLossKinHyp_Wh"]
                    all_results.append(row)

    if not all_results:
        print("\nERROR: No energy results computed. Cannot generate plot.")
        sys.exit(1)

    # Debug: print driven distance per scenario
    print("\n  --- Driven distance summary (forward-filtered) ---")
    for r in sorted(all_results, key=lambda x: (x["section"], x["loading"], x["max_link_length"])):
        print(f"    {r['section']:5s} / {r['max_link_length']:5d}m / {r['loading']:6s}: "
              f"total_length = {r['total_length_m']:.1f} m")

    # --- Step 5: Save results and plot ---
    print("\n" + "=" * 60)
    print("Step 4: Generating plots")
    print("=" * 60)

    results_df = pd.DataFrame(all_results)
    results_csv = output_dir / "energy_results_summary.csv"
    results_df.to_csv(results_csv, index=False)
    print(f"\nResults saved to: {results_csv}")

    generate_plot(results_df, output_dir)

    # --- Step 6: Generate interactive elevation profile HTML ---
    print("\n" + "=" * 60)
    print("Step 5: Generating interactive elevation profiles HTML")
    print("=" * 60)

    try:
        # plot_elevation_profiles liegt (wie Skript 04) im Netzgen-Worktree ->
        # Laufzeit-Pfad-Fallback statt Branch-Merge (Entscheidung 2026-07-01).
        _netgen_dir = Path(__file__).parent.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
        if not (Path(__file__).parent / "plot_elevation_profiles.py").exists() and _netgen_dir.is_dir():
            sys.path.insert(0, str(_netgen_dir))
        from plot_elevation_profiles import main as plot_elevation_main
        plot_elevation_main(data_dir=str(output_dir))
    except Exception as e:
        print(f"WARNING: Could not generate elevation_profiles.html: {e}")

    print(f"\nDone! All outputs in: {output_dir}")


if __name__ == "__main__":
    main()
