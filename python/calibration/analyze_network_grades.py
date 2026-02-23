"""
Steigungsanalyse der MATSim-Netzwerke fuer LH und RD.

Fragestellung: Ist maxGradeAbs=0.15 das Problem bei der Energieueberschaetzung?

Aufruf:
    .venv/Scripts/python analyze_network_grades.py
"""

import gzip
import xml.etree.ElementTree as ET
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

from src.config import MATSIM_MPM_DIR, RESULTS_DIR

# === Fahrzeugparameter fuer Energieabschaetzung ===
SCENARIOS = {
    "LongHaul": {
        "network": MATSIM_MPM_DIR /
                   /sstop   "scenarios" / "VECTO_Longhaul" / "longhaul_network.xml.gz",
        "mSum": 18166 + 10950,   # Mittel aus low+high Payload [kg]
        "v_avg": 22.2,           # ~80 km/h Reisegeschwindigkeit [m/s]
        "ref_kwh_per_km": 1.137, # Mittel LH low+high
    },
    "RegionalDelivery": {
        "network": MATSIM_MPM_DIR / "scenarios" / "VECTO_RegionalDelivery" / "regional_delivery_network.xml.gz",
        "mSum": 18166 + 7750,    # Mittel aus low+high Payload [kg]
        "v_avg": 7.5,            # ~27 km/h Stadtverkehr [m/s]
        "ref_kwh_per_km": 1.065, # Mittel RD low+high
    },
}

G = 9.81   # [m/s^2]
J_TO_KWH = 1 / 3_600_000


def parse_network(path: Path) -> tuple[dict, list]:
    """Liest MATSim-Netzwerk (ggf. gzip) und gibt Knoten-z-Dict und Link-Liste zurueck."""
    print(f"  Lade Netzwerk: {path.name} ...", end=" ", flush=True)

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as f:
        tree = ET.parse(f)

    root = tree.getroot()
    print("OK")

    # Knoten: id -> z (None falls kein z-Attribut)
    nodes = {}
    for node in root.iter("node"):
        z = node.get("z")
        nodes[node.get("id")] = float(z) if z is not None else None

    # Links: (from_id, to_id, length)
    links = []
    for link in root.iter("link"):
        try:
            length = float(link.get("length", 0))
            if length > 0:
                links.append((link.get("from"), link.get("to"), length))
        except (ValueError, TypeError):
            pass

    return nodes, links


def compute_grades(nodes: dict, links: list) -> np.ndarray:
    """Berechnet Steigung [m/m] pro Link. Links ohne z-Koordinaten werden uebersprungen."""
    grades = []
    skipped = 0
    for from_id, to_id, length in links:
        z_from = nodes.get(from_id)
        z_to = nodes.get(to_id)
        if z_from is None or z_to is None:
            skipped += 1
            continue
        grades.append((z_to - z_from) / length)

    if skipped > 0:
        print(f"    Uebersprungen (kein z): {skipped}/{len(links)} Links")

    return np.array(grades)


def grade_energy_kwh_per_km(grades: np.ndarray, lengths: np.ndarray,
                             mSum: float, v: float,
                             recup_eff: float = 0.6,
                             max_grade: float = None) -> float:
    """
    Schaetzt den mittleren Netto-Steigungsenergieverbrauch [kWh/km].

    Bergauf:  E = m*g*grade*dist / spr  (spr=0.92)
    Bergab:   E = -m*g*|grade|*dist * recup_eff
    max_grade: kapped die Steigung auf +/- max_grade (simuliert maxGradeAbs)
    """
    g = grades.copy()
    if max_grade is not None:
        g = np.clip(g, -max_grade, max_grade)

    total_dist_m = lengths.sum()
    if total_dist_m == 0:
        return 0.0

    uphill = g > 0
    downhill = g < 0

    e_up   = np.sum(mSum * G * g[uphill]  * lengths[uphill] / 0.92)   # [J]
    e_down = np.sum(mSum * G * np.abs(g[downhill]) * lengths[downhill] * recup_eff)  # [J] rueckgewonnen

    netto_j = e_up - e_down
    netto_kwh_per_km = netto_j * J_TO_KWH / (total_dist_m / 1000)
    return netto_kwh_per_km


def analyse(name: str, cfg: dict):
    print(f"\n{'='*60}")
    print(f"  Szenario: {name}")
    print(f"{'='*60}")

    nodes, links = parse_network(cfg["network"])

    # Laengen parallel zu grades berechnen
    lengths_all = []
    grades_raw = []
    for from_id, to_id, length in links:
        z_from = nodes.get(from_id)
        z_to = nodes.get(to_id)
        if z_from is None or z_to is None:
            continue
        grades_raw.append((z_to - z_from) / length)
        lengths_all.append(length)

    grades = np.array(grades_raw)
    lengths = np.array(lengths_all)

    if len(grades) == 0:
        print("  KEINE z-Koordinaten im Netzwerk vorhanden! Steigungsterm = 0 fuer alle Links.")
        print("  -> maxGradeAbs ist irrelevant, da keine Steigungen berechnet werden koennen.")
        return

    abs_grades = np.abs(grades)
    total_links = len(grades)
    total_dist_km = lengths.sum() / 1000

    print(f"\n  Netzwerk: {total_links} Links mit z-Koordinaten | {total_dist_km:.1f} km Gesamtlaenge")

    # --- Statistische Verteilung ---
    print("\n  Steigungsverteilung (Absolutwerte):")
    for p in [50, 75, 90, 95, 99, 100]:
        val = np.percentile(abs_grades, p)
        print(f"    P{p:3d}: {val*100:6.2f}%")

    print("\n  Anteil Links mit |Steigung| ueber Schwellwert:")
    for thresh in [0.02, 0.05, 0.10, 0.15, 0.20]:
        n_over = np.sum(abs_grades > thresh)
        pct_links = 100 * n_over / total_links
        dist_over = lengths[abs_grades > thresh].sum() / 1000
        pct_dist = 100 * dist_over / total_dist_km
        print(f"    > {thresh*100:4.1f}%: {n_over:5d} Links ({pct_links:5.1f}%) | "
              f"{dist_over:6.1f} km ({pct_dist:5.1f}% der Strecke)")

    # --- Energieabschaetzung bei verschiedenen maxGradeAbs ---
    mSum = cfg["mSum"]
    v = cfg["v_avg"]
    ref = cfg["ref_kwh_per_km"]

    print(f"\n  Netto-Steigungsenergie bei verschiedenen maxGradeAbs")
    print(f"  (Fahrzeug: {mSum} kg | v_avg: {v*3.6:.0f} km/h | Referenzverbrauch gesamt: {ref:.3f} kWh/km)")
    print(f"  {'maxGradeAbs':>12} | {'E_Steigung':>12} | {'Anteil an Ref.':>15}")
    print(f"  {'-'*45}")
    for cap in [None, 0.15, 0.10, 0.05, 0.03, 0.02, 0.01, 0.0]:
        label = f"{cap*100:.1f}%" if cap is not None else "unbegrenzt"
        e = grade_energy_kwh_per_km(grades, lengths, mSum, v, max_grade=cap)
        anteil = 100 * e / ref if ref > 0 else float("nan")
        print(f"  {label:>12} | {e:>10.4f}   | {anteil:>13.1f}%")

    # --- Plot ---
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle(f"Steigungsverteilung — {name}", fontsize=13)

    # Histogramm (Haeufigkeit, gewichtet nach Streckenlaenge)
    ax1 = axes[0]
    bins = np.linspace(-0.30, 0.30, 61)
    weights = lengths / lengths.sum()
    ax1.hist(grades, bins=bins, weights=weights, color="steelblue", edgecolor="white", linewidth=0.3)
    for cap, color in [(0.15, "red"), (0.05, "orange"), (0.02, "green")]:
        ax1.axvline(cap, color=color, linestyle="--", linewidth=1.2, label=f"±{cap*100:.0f}%")
        ax1.axvline(-cap, color=color, linestyle="--", linewidth=1.2)
    ax1.set_xlabel("Steigung [m/m]")
    ax1.set_ylabel("Anteil Strecke (gewichtet)")
    ax1.set_title("Verteilung (laengengewichtet)")
    ax1.legend(title="maxGradeAbs", fontsize=8)

    # CDF der Absolutsteigung
    ax2 = axes[1]
    sorted_abs = np.sort(abs_grades)
    cdf = np.cumsum(lengths[np.argsort(abs_grades)]) / lengths.sum()
    ax2.plot(sorted_abs * 100, cdf * 100, color="steelblue", linewidth=1.5)
    for cap, color, lbl in [(0.15, "red", "15%"), (0.05, "orange", "5%"), (0.02, "green", "2%")]:
        ax2.axvline(cap * 100, color=color, linestyle="--", linewidth=1.2, label=lbl)
        idx = np.searchsorted(sorted_abs, cap)
        covered = cdf[min(idx, len(cdf)-1)] * 100
        ax2.annotate(f"{covered:.1f}%", xy=(cap*100, covered),
                     xytext=(cap*100 + 0.5, covered - 5), fontsize=7, color=color)
    ax2.set_xlabel("Absolutsteigung [%]")
    ax2.set_ylabel("Kumul. Streckenanteil [%]")
    ax2.set_title("Kumulierte Verteilung")
    ax2.legend(title="maxGradeAbs", fontsize=8)
    ax2.set_xlim(0, 30)

    plt.tight_layout()
    out_path = RESULTS_DIR / f"grade_distribution_{name}.png"
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"\n  Plot gespeichert: {out_path}")


# === Hauptprogramm ===
for name, cfg in SCENARIOS.items():
    analyse(name, cfg)

print("\nFertig.")
