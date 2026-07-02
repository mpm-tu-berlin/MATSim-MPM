"""Fehlerberechnung: Vergleich MATSim-Energieverbrauch vs. VECTO-Referenzwerte.

Unterstuetzt Dual-Cycle-Kalibrierung (Long Haul + Regional Delivery).
"""

import csv
import gzip
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from src import config as _cfg
from src.config import REFERENCE_CONSUMPTION_FILE

# Joule -> kWh
_J_TO_KWH = 1.0 / 3_600_000.0


# =====================================================================
# Trip-End-KE-Korrektur (nur Vergleichsseite, siehe config.TRIP_END_KE_CORRECTION)
# =====================================================================

def load_vehicle_masses(mission: str) -> dict[str, tuple[float, float]]:
    """Liest mass/payload pro Fahrzeug-ID aus der vehicles.xml des Szenarios.

    Returns:
        Dict {vehicle_id: (mass_kg, payload_kg)}
    """
    vehicles_file = _cfg.SCENARIOS[mission]["config"].parent / "vehicles.xml"
    root = ET.parse(vehicles_file).getroot()

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    type_masses: dict[str, tuple[float, float]] = {}
    vehicle_masses: dict[str, tuple[float, float]] = {}
    for el in root.iter():
        if _local(el.tag) == "vehicleType":
            mass = payload = None
            for attr in el.iter():
                if _local(attr.tag) == "attribute":
                    if attr.get("name") == "mass":
                        mass = float(attr.text)
                    elif attr.get("name") == "payload":
                        payload = float(attr.text)
            if mass is not None:
                type_masses[el.get("id")] = (mass, payload or 0.0)
        elif _local(el.tag) == "vehicle":
            t = el.get("type")
            if t in type_masses:
                vehicle_masses[el.get("id")] = type_masses[t]
    return vehicle_masses


def parse_trip_end_speeds(output_dir: Path) -> dict[str, float]:
    """Liest die End-Geschwindigkeit (vExit des letzten QSim-Links) pro Fahrzeug
    aus resistance_debug.csv.

    Annahme: Router-Schaetzrows (C1-Befund) stehen VOR den QSim-Rows, da das
    Routing vor der Mobsim laeuft — die letzte gueltige Zeile pro Fahrzeug ist
    damit das echte Trip-Ende. Unparsbare (interleavte) Zeilen werden uebersprungen.

    Returns:
        Dict {vehicle_id: v_end_m_per_s}
    """
    csv_path = output_dir / "resistance_debug.csv"
    if not csv_path.exists():
        return {}
    v_end: dict[str, float] = {}
    with open(csv_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) < 14 or parts[0] == "vehicleId":
                continue
            try:
                v_end[parts[0]] = float(parts[5]) / 3.6  # vExit_kmh -> m/s
            except ValueError:
                continue
    return v_end


def trip_end_ke_corrections_kwh(output_dir: Path, mission: str,
                                params: dict[str, float] | None,
                                boundary: str | None = None) -> dict[str, float]:
    """Trip-End-KE-Korrektur [kWh] pro Fahrzeug (Vergleichsseite, siehe config).

    Das Modell endet bei vEnd > 0 und bremst nie. Bewertung nach Randbedingung
    der Referenz:
      "stop"    (VECTO-Zyklen, enden im Stillstand): dem Modell fehlt die
                Rekuperation der End-Bremsung -> Gutschrift * recupEfficiency.
      "rolling" (Realfahrt-Fenster, enden rollend): Storno der gebuchten
                Anfahr-KE -> / tractionEfficiency.

    Returns:
        Dict {vehicle_id: korrektur_kwh} (leer, wenn deaktiviert oder Daten fehlen).
    """
    if not getattr(_cfg, "TRIP_END_KE_CORRECTION", False) or not params:
        return {}
    boundary = boundary or getattr(_cfg, "TRIP_END_KE_BOUNDARY", "stop")
    inertia_c = params.get("inertiaC")
    eta_t = params.get("tractionEfficiency")
    eta_r = params.get("recupEfficiency")
    if not inertia_c or not eta_t or (boundary == "stop" and not eta_r):
        return {}
    factor = eta_r if boundary == "stop" else 1.0 / eta_t
    v_end = parse_trip_end_speeds(output_dir)
    if not v_end:
        print(f"[trip-end-KE] WARNUNG: keine resistance_debug.csv in {output_dir} "
              f"— Korrektur entfaellt.")
        return {}
    masses = load_vehicle_masses(mission)
    corrections = {}
    for vid, v in v_end.items():
        if vid not in masses:
            continue
        mass, payload = masses[vid]
        m_inertia = mass * inertia_c + payload
        corrections[vid] = 0.5 * m_inertia * v * v * factor * _J_TO_KWH
    return corrections


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

    Hinweis: Bei kleinem qsim.timeStepSize (z.B. 0.04s) enthaelt diese Datei
    nur wenige bis eine Zeile — der SoC-Sampler scheint mit der Sample-Rate
    nicht klarzukommen. Fuer robuste Verbrauchsmessung quer ueber alle
    Auflösungen `parse_events_consumption` verwenden.

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


# Regex auf XML-Eventzeilen — schnell, ohne XML-Parser.
_EV_VEHICLE_RE = re.compile(r'vehicle="([^"]+)"')
_EV_ENERGY_RE  = re.compile(r'energy="([0-9.eE+\-]+)"')


def parse_events_consumption(output_dir: Path) -> dict[str, float]:
    """Summiert den Gesamt-Batterieverbrauch pro Fahrzeug aus
    output_events.xml.gz.

    Wichtig: Das `drivingEnergyConsumption`-Event in MATSim-EV (2024.0)
    enthaelt bereits **Drive + Aux**. `DriveDischargingHandler.dischargeVehicle`
    addiert intern `driveEnergyConsumption + auxEnergyConsumption` und
    emittiert ein einziges Event mit dem Gesamtwert. Daher hier KEINE
    Aux-Addition mehr — das waere Doppelzaehlung.

    Robust gegen kleine timeStepSize-Werte (im Gegensatz zu
    parse_charge_profiles, das auf SoC-Sampling im 5-min-Raster basiert und
    bei spaeten Trip-Enden den Schwanz verpasst).

    Args:
        output_dir: MATSim-Output-Verzeichnis (output_events.xml.gz darin).

    Returns:
        Dict {vehicle_id: verbrauch_kwh}.
    """
    events_path = output_dir / "output_events.xml.gz"
    if not events_path.exists():
        raise FileNotFoundError(f"Events-Datei nicht gefunden: {events_path}")

    consumption_j: dict[str, float] = {}
    with gzip.open(events_path, "rt", encoding="utf-8") as f:
        for line in f:
            if "drivingEnergyConsumption" not in line:
                continue
            v = _EV_VEHICLE_RE.search(line)
            e = _EV_ENERGY_RE.search(line)
            if v and e:
                consumption_j[v.group(1)] = (
                    consumption_j.get(v.group(1), 0.0) + float(e.group(1))
                )

    if not consumption_j:
        raise ValueError(
            f"Keine drivingEnergyConsumption-Events in {events_path} gefunden."
        )

    return {vid: e_j * _J_TO_KWH for vid, e_j in consumption_j.items()}


def compute_scenario_error(output_dir: Path, mission: str, route_km: float,
                           params: dict[str, float] | None = None) -> list[tuple[float, float]]:
    """Berechnet die relativen Fehler fuer ein einzelnes Szenario.

    Args:
        output_dir: MATSim-Output-Verzeichnis.
        mission: Name der Mission ("LongHaul" oder "RegionalDelivery").
        route_km: Streckenlaenge in km.
        params: Trial-Parameter (fuer die Trip-End-KE-Korrektur; None = keine).

    Returns:
        Liste von (rel_squared_error, abs_rel_error) pro Fahrzeug [-].
        Fehler relativ zum VECTO-Referenzwert.
    """
    reference = load_reference(mission)
    consumption = parse_events_consumption(output_dir)
    ke_corr = trip_end_ke_corrections_kwh(output_dir, mission, params)

    payload_class = _cfg.ACTIVE_PAYLOAD_CLASS  # "low", "high" oder "all"

    errors = []
    for vid, ref_data in reference.items():
        if payload_class != "all" and not vid.endswith(f"_{payload_class}"):
            continue
        if vid not in consumption:
            raise KeyError(
                f"Fahrzeug '{vid}' nicht in MATSim-Output gefunden. "
                f"Vorhandene Fahrzeuge: {list(consumption.keys())}"
            )
        matsim_ee = (consumption[vid] - ke_corr.get(vid, 0.0)) / route_km
        ref_ee = ref_data["ee_kwh_per_km"]
        rel = (matsim_ee - ref_ee) / ref_ee
        errors.append((rel ** 2, abs(rel)))

    return errors


def compute_error(output_dir: Path, route_km: float = 100.185) -> float:
    """Berechnet den RMSE fuer ein einzelnes Szenario (abwaertskompatibel).

    Args:
        output_dir: MATSim-Output-Verzeichnis.
        route_km: Streckenlaenge in km.

    Returns:
        RMSE [kWh/km] ueber alle Fahrzeuge.
    """
    consumption = parse_events_consumption(output_dir)
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


def format_final_report(scenario_outputs: dict[str, Path], trial_number: int,
                        params: dict[str, float]) -> str:
    """Detaillierter Abschlussbericht: Verbrauchsvergleich MATSim vs. VECTO.

    Args:
        scenario_outputs: Dict {scenario_name: output_dir}
        trial_number: Optuna-Trial-Nummer des besten Trials.
        params: Kalibrierungsparameter des besten Trials.

    Returns:
        Formatierten Bericht als mehrzeiliger String.
    """
    lines = []
    lines.append(f"\n{'='*75}")
    lines.append(f"  Bestes Trial: #{trial_number}")
    for name, val in params.items():
        lines.append(f"    {name} = {val:.6f}")

    sq_errors: list[float] = []
    abs_errors: list[float] = []

    for scenario_name, out_dir in sorted(scenario_outputs.items()):
        route_km = _cfg.SCENARIOS[scenario_name]["route_km"]
        consumption = parse_events_consumption(out_dir)
        reference = load_reference(scenario_name)
        ke_corr = trip_end_ke_corrections_kwh(out_dir, scenario_name, params)

        lines.append(f"\n  [{scenario_name}]  Strecke: {route_km} km")
        if ke_corr:
            corr_str = "  ".join(f"{vid}: -{c:.2f} kWh" for vid, c in sorted(ke_corr.items()))
            lines.append(f"  Trip-End-KE-Korrektur (Storno Anfahr-Buchung): {corr_str}")
        lines.append(
            f"  {'Fahrzeug':<35} {'MATSim':>9} {'VECTO':>9} {'Diff':>9} {'Diff%':>7}"
        )
        lines.append(f"  {'-'*73}")

        payload_class = _cfg.ACTIVE_PAYLOAD_CLASS
        for vid, ref in sorted(reference.items()):
            if payload_class != "all" and not vid.endswith(f"_{payload_class}"):
                continue
            matsim = (consumption[vid] - ke_corr.get(vid, 0.0)) / route_km
            vecto = ref["ee_kwh_per_km"]
            diff = matsim - vecto
            rel_pct = diff / vecto * 100.0
            sq_errors.append((diff / vecto) ** 2)
            abs_errors.append(abs(diff / vecto))
            sign = "+" if diff >= 0 else ""
            rel_sign = "+" if rel_pct >= 0 else ""
            lines.append(
                f"  {vid:<35} {matsim:>8.4f}  {vecto:>8.4f}  "
                f"{sign}{diff:>7.4f}  {rel_sign}{rel_pct:>5.2f}%  kWh/km"
            )

    rmse_pct = math.sqrt(sum(sq_errors) / len(sq_errors)) * 100.0
    mae_pct = (sum(abs_errors) / len(abs_errors)) * 100.0
    lines.append(f"\n  => RMSE: {rmse_pct:.2f}%    MAE: {mae_pct:.2f}%")
    lines.append(f"{'='*75}")
    return "\n".join(lines)


def compute_combined_errors(scenario_outputs: dict[str, Path],
                            params: dict[str, float] | None = None) -> tuple[float, float]:
    """Berechnet RMSE und MAE in Prozent ueber alle Szenarien.

    Args:
        scenario_outputs: Dict {scenario_name: output_dir}
        params: Trial-Parameter (fuer die Trip-End-KE-Korrektur; None = keine).

    Returns:
        (rmse_pct, mae_pct): Relativer RMSE und MAE in % gegenueber VECTO.
    """
    sq_errors: list[float] = []
    abs_errors: list[float] = []

    for scenario_name, output_dir in scenario_outputs.items():
        if scenario_name not in _cfg.SCENARIOS:
            raise ValueError(f"Unbekanntes Szenario: {scenario_name}")
        route_km = _cfg.SCENARIOS[scenario_name]["route_km"]
        for sq, ab in compute_scenario_error(output_dir, scenario_name, route_km, params):
            sq_errors.append(sq)
            abs_errors.append(ab)

    if not sq_errors:
        raise ValueError("Keine Fahrzeuge zum Vergleichen gefunden")

    rmse_pct = math.sqrt(sum(sq_errors) / len(sq_errors)) * 100.0
    mae_pct = (sum(abs_errors) / len(abs_errors)) * 100.0
    return rmse_pct, mae_pct


def compute_combined_error(scenario_outputs: dict[str, Path],
                           params: dict[str, float] | None = None) -> float:
    """Gibt den RMSE in % zurueck (Optuna-Zielfunktion).

    Args:
        scenario_outputs: Dict {scenario_name: output_dir}
        params: Trial-Parameter (fuer die Trip-End-KE-Korrektur; None = keine).

    Returns:
        Relativer RMSE in % gegenueber VECTO-Referenzwerten.
    """
    rmse_pct, _ = compute_combined_errors(scenario_outputs, params)
    return rmse_pct
