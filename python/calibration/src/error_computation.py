"""Fehlerberechnung: Vergleich MATSim-Energieverbrauch vs. VECTO-Referenzwerte.

Unterstuetzt Dual-Cycle-Kalibrierung (Long Haul + Regional Delivery).
"""

import csv
import math
from pathlib import Path

from src import config as _cfg
from src.config import REFERENCE_CONSUMPTION_FILE, SCENARIOS


def load_reference(mission: str | None = None,
                   vehicle_group: str | None = None) -> dict[str, dict]:
    """Laedt die VECTO-Referenzwerte pro Fahrzeug-ID.

    Args:
        mission: Filtert nach Mission ("LongHaul", "RegionalDelivery"). None = alle.
        vehicle_group: Filtert nach Fahrzeuggruppe (z.B. "Volvo_FH42TE").
                       None = aktive Gruppe aus config.ACTIVE_VEHICLE_GROUP.

    Returns:
        Dict {vehicle_id: {"ee_kwh_per_km": float, "route_km": float, "mission": str}}
    """
    group_filter = vehicle_group or _cfg.ACTIVE_VEHICLE_GROUP
    ref = {}
    with open(REFERENCE_CONSUMPTION_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(
            (row for row in f if not row.startswith("#")),
        )
        for row in reader:
            if row["vehicle_group"] != group_filter:
                continue
            if mission and row["mission"] != mission:
                continue
            ref[row["vehicle_id"]] = {
                "ee_kwh_per_km": float(row["ee_kwh_per_km"]),
                "route_km": float(row["route_km"]),
                "mission": row["mission"],
            }
    return ref


def parse_charge_profiles(output_dir: Path) -> dict[str, float]:
    """Liest individual_charge_time_profiles.txt und berechnet den
    Gesamtverbrauch (kWh) pro Fahrzeug als Differenz Start- zu End-SoC.

    Args:
        output_dir: MATSim-Output-Verzeichnis (enthaelt ITERS/it.0/).

    Returns:
        Dict {vehicle_id: verbrauch_kwh}
    """
    profile_file = output_dir / "ITERS" / "it.0" / "0.individual_charge_time_profiles.txt"
    if not profile_file.exists():
        raise FileNotFoundError(f"Charge-Profil nicht gefunden: {profile_file}")

    with open(profile_file, encoding="utf-8") as f:
        lines = f.readlines()

    if len(lines) < 2:
        raise ValueError("Charge-Profil ist leer")

    # Header: time\tvehicle1\tvehicle2\t...
    header = lines[0].strip().split("\t")
    vehicle_ids = header[1:]  # Erste Spalte ist 'time'

    # Erste Datenzeile (Start-SoC)
    first_data = lines[1].strip().split("\t")
    initial_soc = {vid: float(val) for vid, val in zip(vehicle_ids, first_data[1:])}

    # Letzte Datenzeile (End-SoC)
    last_data = lines[-1].strip().split("\t")
    final_soc = {vid: float(val) for vid, val in zip(vehicle_ids, last_data[1:])}

    # Verbrauch = Initial - Final [kWh]
    consumption = {}
    for vid in vehicle_ids:
        consumption[vid] = initial_soc[vid] - final_soc[vid]

    return consumption


def compute_scenario_error(output_dir: Path, mission: str,
                           route_km: float) -> list[float]:
    """Berechnet die quadratischen Fehler fuer ein einzelnes Szenario.

    Args:
        output_dir: MATSim-Output-Verzeichnis.
        mission: Name der Mission ("LongHaul" oder "RegionalDelivery").
        route_km: Streckenlaenge in km.

    Returns:
        Liste der quadratischen Fehler [(matsim - ref)² in (kWh/km)²].
    """
    reference = load_reference(mission)
    consumption = parse_charge_profiles(output_dir)

    squared_errors = []
    for vid, ref_data in reference.items():
        if vid not in consumption:
            raise KeyError(
                f"Fahrzeug '{vid}' nicht in MATSim-Output gefunden. "
                f"Vorhandene Fahrzeuge: {list(consumption.keys())}"
            )
        # MATSim-Verbrauch: kWh gesamt -> kWh/km
        matsim_ee = consumption[vid] / route_km
        ref_ee = ref_data["ee_kwh_per_km"]
        squared_errors.append((matsim_ee - ref_ee) ** 2)

    return squared_errors


def compute_error(output_dir: Path, route_km: float = 100.185) -> float:
    """Berechnet den RMSE fuer ein einzelnes Szenario (abwaertskompatibel).

    Args:
        output_dir: MATSim-Output-Verzeichnis.
        route_km: Streckenlaenge in km.

    Returns:
        RMSE [kWh/km] ueber alle Fahrzeuge.
    """
    consumption = parse_charge_profiles(output_dir)
    reference = load_reference()

    squared_errors = []
    for vid, ref_data in reference.items():
        if vid not in consumption:
            continue  # Fahrzeuge aus anderem Szenario ueberspringen
        matsim_ee = consumption[vid] / route_km
        ref_ee = ref_data["ee_kwh_per_km"]
        squared_errors.append((matsim_ee - ref_ee) ** 2)

    if not squared_errors:
        raise ValueError("Keine Fahrzeuge zum Vergleichen gefunden")

    return math.sqrt(sum(squared_errors) / len(squared_errors))


def format_trial_report(scenario_outputs: dict[str, Path], trial_number: int,
                        params: dict[str, float]) -> str:
    """Gibt einen formatierten Bericht eines Trials zurueck (Terminal + Log).

    Args:
        scenario_outputs: Dict {scenario_name: output_dir}
        trial_number: Optuna-Trial-Nummer.
        params: Kalibrierungsparameter dieses Trials.

    Returns:
        Formatierten Bericht als mehrzeiliger String.
    """
    lines = []
    lines.append(f"\n{'='*65}")
    lines.append(f"  Trial {trial_number}")
    for name, val in params.items():
        lines.append(f"    {name} = {val:.6f}")

    all_sq_errors = []
    for scenario_name, out_dir in sorted(scenario_outputs.items()):
        route_km = SCENARIOS[scenario_name]["route_km"]
        consumption = parse_charge_profiles(out_dir)
        reference = load_reference(scenario_name)

        lines.append(f"\n  [{scenario_name}]  Strecke: {route_km} km")
        lines.append(f"  {'Fahrzeug':<35} {'MATSim':>9} {'VECTO':>9} {'Diff':>9}")
        lines.append(f"  {'-'*63}")
        for vid, ref in sorted(reference.items()):
            matsim = consumption[vid] / route_km
            vecto = ref["ee_kwh_per_km"]
            diff = matsim - vecto
            all_sq_errors.append(diff ** 2)
            sign = "+" if diff >= 0 else ""
            lines.append(
                f"  {vid:<35} {matsim:>8.4f}  {vecto:>8.4f}  {sign}{diff:>7.4f}  kWh/km"
            )

    rmse = math.sqrt(sum(all_sq_errors) / len(all_sq_errors))
    lines.append(f"\n  => RMSE: {rmse:.6f} kWh/km")
    lines.append(f"{'='*65}")
    return "\n".join(lines)


def compute_combined_error(scenario_outputs: dict[str, Path]) -> float:
    """Berechnet den kombinierten RMSE ueber alle Szenarien.

    Sammelt die quadratischen Fehler aus allen Szenarien und berechnet
    daraus einen Gesamt-RMSE. Dadurch werden alle 12 Fahrzeuge
    (6 Long Haul + 6 Regional Delivery) gleichgewichtet.

    Args:
        scenario_outputs: Dict {scenario_name: output_dir}

    Returns:
        Gesamt-RMSE [kWh/km] ueber alle Szenarien und Fahrzeuge.
    """
    all_squared_errors = []

    for scenario_name, output_dir in scenario_outputs.items():
        if scenario_name not in SCENARIOS:
            raise ValueError(f"Unbekanntes Szenario: {scenario_name}")

        route_km = SCENARIOS[scenario_name]["route_km"]
        errors = compute_scenario_error(output_dir, scenario_name, route_km)
        all_squared_errors.extend(errors)

    if not all_squared_errors:
        raise ValueError("Keine Fahrzeuge zum Vergleichen gefunden")

    return math.sqrt(sum(all_squared_errors) / len(all_squared_errors))
