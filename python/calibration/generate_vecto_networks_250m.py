"""
Generiert MATSim-Netzwerke direkt aus VECTO-Fahrzyklus-Dateien (.vdri).

Im Unterschied zu generate_vecto_networks.py werden die Messpunkte zu
Segmenten von mindestens MIN_SEGMENT_LENGTH_M Metern zusammengefasst:

  - Freigeschwindigkeit: harmonisches Mittel (Strecke / Zeit), erhaelt
    die Gesamtfahrzeit und ist damit physikalisch korrekt.
  - Steigung: Netto-Hoehendifferenz geteilt durch Segmentlaenge (effektive
    Durchschnittssteigung), identisch zur Berechnung in der Java-Simulation.

Hintergrund: 1-m-Links fuehren bei QSim-Zeitdiskretisierung (timeStepSize
= 0.04 s) zu einem Rundungsartefakt: jede freespeed zwischen 45 km/h und
90 km/h wird auf genau 45 km/h gerundet (2 Zeitschritte a 0.04 s = 0.08 s,
1 m / 0.08 s = 12.5 m/s = 45 km/h). Mit 250-m-Segmenten betraegt der
Rundungsfehler bei 83 km/h noch < 0.2 % (10.84 s / 0.04 s = 271 Schritte).

Ausgabepfade: direkt in die jeweiligen Szenarioverzeichnisse von MATSim-MPM.
"""

import csv
import gzip
import os

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

KALIBRIERUNGSPROJEKT = r"C:\Users\Tobias\PycharmProjects\MATSim-VECTO-Calibration"

SZENARIEN = [
    {
        "vdri":   os.path.join(KALIBRIERUNGSPROJEKT, "data", "LongHaul.vdri"),
        "ausgabe": (
            r"C:\Users\Tobias\IdeaProjects\MATSim-MPM\scenarios"
            r"\VECTO_Longhaul\longhaul_network_250m.xml.gz"
        ),
        "name": "LongHaul",
    },
    {
        "vdri":   os.path.join(KALIBRIERUNGSPROJEKT, "data", "RegionalDelivery.vdri"),
        "ausgabe": (
            r"C:\Users\Tobias\IdeaProjects\MATSim-MPM\scenarios"
            r"\VECTO_RegionalDelivery\regional_delivery_network_250m.xml.gz"
        ),
        "name": "RegionalDelivery",
    },
]

# Mindestlaenge eines Segments [m].
# 250 m bei 83 km/h = 23.06 m/s → Fahrzeit 10.84 s = ~271 Zeitschritte → Fehler < 0.2 %.
MIN_SEGMENT_LENGTH_M = 250

# Minimale Freigeschwindigkeit fuer Haltepunkte (v = 0) in m/s
FREESPEED_MIN = 0.5


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def lade_vdri(pfad: str) -> tuple[list[float], list[float], list[float]]:
    """Liest eine .vdri-Datei und gibt (s_m, v_kmh, grad_pct) zurueck."""
    s_m: list[float] = []
    v_kmh: list[float] = []
    grad_pct: list[float] = []

    with open(pfad, newline="", encoding="utf-8-sig") as f:
        leser = csv.DictReader(f)
        for zeile in leser:
            s_m.append(float(zeile["<s>"]))
            v_kmh.append(float(zeile["<v>"]))
            grad_pct.append(float(zeile["<grad>"]))

    return s_m, v_kmh, grad_pct


def berechne_elevation(s_m: list[float], grad_pct: list[float]) -> list[float]:
    """
    Berechnet kumulative Hoehenkoordinaten aus den Gradienten.
    z[0] = 0; z[i] = z[i-1] + grad[i-1]/100 * (s[i] - s[i-1])
    """
    n = len(s_m)
    z_m = [0.0] * n
    for i in range(1, n):
        delta_s = s_m[i] - s_m[i - 1]
        z_m[i] = z_m[i - 1] + grad_pct[i - 1] / 100.0 * delta_s
    return z_m


def baue_segmente(s_m: list[float], min_laenge: float) -> list[tuple[int, int]]:
    """
    Fasst aufeinanderfolgende Messpunkte zu Segmenten zusammen.

    Ein neues Segment beginnt, sobald die akkumulierte Streckenlaenge
    >= min_laenge ist. Das letzte Segment kann kuerzer sein.

    Returns:
        Liste von (start_idx, end_idx)-Paaren (beide inklusive).
    """
    segmente = []
    start = 0
    for i in range(1, len(s_m)):
        if s_m[i] - s_m[start] >= min_laenge:
            segmente.append((start, i))
            start = i
    if start < len(s_m) - 1:
        segmente.append((start, len(s_m) - 1))
    return segmente


def segment_freespeed(
    s_m: list[float], v_kmh: list[float], start: int, end: int
) -> float:
    """
    Berechnet die harmonische Mittgeschwindigkeit eines Segments.

    Harmonisches Mittel (v = Strecke / Zeit) erhaelt die Gesamtfahrzeit
    und ist damit physikalisch korrekt fuer die Energieberechnung.
    Nullgeschwindigkeiten werden durch FREESPEED_MIN ersetzt.
    """
    total_length = s_m[end] - s_m[start]
    if total_length <= 0:
        return FREESPEED_MIN

    total_time = 0.0
    for i in range(start, end):
        ds = s_m[i + 1] - s_m[i]
        v_ms = max(v_kmh[i] / 3.6, FREESPEED_MIN)
        total_time += ds / v_ms

    if total_time <= 0:
        return FREESPEED_MIN

    return max(total_length / total_time, FREESPEED_MIN)


def schreibe_netzwerk(
    ausgabepfad: str,
    s_m: list[float],
    v_kmh: list[float],
    z_m: list[float],
    name: str,
) -> None:
    """Schreibt das aggregierte MATSim-Netzwerk zeilenweise in eine gzip-Datei."""
    segmente = baue_segmente(s_m, MIN_SEGMENT_LENGTH_M)

    # Nodes nur an Segmentgrenzen (eindeutige Indizes)
    node_indizes: list[int] = []
    for start, end in segmente:
        if not node_indizes or node_indizes[-1] != start:
            node_indizes.append(start)
        node_indizes.append(end)

    # Abbildung Messpunkt-Index -> fortlaufende Node-ID
    idx_zu_node = {idx: nid for nid, idx in enumerate(node_indizes)}

    n_nodes = len(node_indizes)
    n_links = len(segmente)

    os.makedirs(os.path.dirname(ausgabepfad), exist_ok=True)

    with gzip.open(ausgabepfad, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(
            '<!DOCTYPE network SYSTEM '
            '"http://www.matsim.org/files/dtd/network_v2.dtd">\n'
        )
        f.write("<network>\n")

        # Nodes
        f.write("\t<nodes>\n")
        for idx in node_indizes:
            nid = idx_zu_node[idx]
            f.write(
                f'\t\t<node id="{nid}" x="{s_m[idx]}" y="0" z="{z_m[idx]:.6f}"/>\n'
            )
        f.write("\t</nodes>\n")

        # Links (ein Link pro Segment)
        f.write(
            '\t<links capperiod="01:00:00" '
            'effectivecellsize="7.5" effectivelanewidth="3.75">\n'
        )
        for seg_id, (start, end) in enumerate(segmente):
            from_node = idx_zu_node[start]
            to_node   = idx_zu_node[end]
            laenge    = s_m[end] - s_m[start]
            freespeed = segment_freespeed(s_m, v_kmh, start, end)
            f.write(
                f'\t\t<link id="{seg_id}" from="{from_node}" to="{to_node}"'
                f' length="{laenge:.2f}"'
                f' freespeed="{freespeed:.4f}"'
                f' capacity="9999"'
                f' permlanes="1"'
                f' modes="car"/>\n'
            )
        f.write("\t</links>\n")
        f.write("</network>\n")

    print(
        f"[{name}] Netzwerk geschrieben: {n_nodes} Nodes, {n_links} Links"
        f" (MIN_SEGMENT_LENGTH = {MIN_SEGMENT_LENGTH_M} m)"
        f" -> {ausgabepfad}"
    )


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    for szenario in SZENARIEN:
        name = szenario["name"]
        print(f"[{name}] Lese {szenario['vdri']} ...")

        s_m, v_kmh, grad_pct = lade_vdri(szenario["vdri"])

        print(f"[{name}] {len(s_m)} Eintraege geladen.")
        print(
            f"[{name}] Distanz: {s_m[0]:.0f} m – {s_m[-1]:.0f} m"
            f" ({s_m[-1] / 1000:.3f} km)"
        )
        print(f"[{name}] Geschwindigkeit max: {max(v_kmh):.1f} km/h")

        z_m = berechne_elevation(s_m, grad_pct)

        schreibe_netzwerk(szenario["ausgabe"], s_m, v_kmh, z_m, name)


if __name__ == "__main__":
    main()
