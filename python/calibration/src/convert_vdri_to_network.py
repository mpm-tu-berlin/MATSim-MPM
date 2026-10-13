"""
Konvertiert eine VECTO-Fahrzyklus-Datei (.vdri) in ein MATSim-Netzwerk (.xml.gz).

Aufbau des Netzwerks:
  - Jeder Messpunkt wird als Node gespeichert (x=Distanz, y=0, z=kumulative Höhe).
  - Aufeinanderfolgende Messpunkte werden zu Segmenten zusammengefasst, bis die
    Mindestlinklänge MIN_LINK_LENGTH_M erreicht ist.
  - Pro Segment entsteht ein Link; Nodes werden nur an Segmentgrenzen erzeugt.

Hintergrund: MATSim QSim diskretisiert Zeit auf ganze Sekunden. Ein 1m-Link bei
83 km/h würde theoretisch 0.043 s dauern — QSim rundet auf 1 Sekunde, die effektive
Geschwindigkeit fällt auf 1 m/s = 3.6 km/h. Mit MIN_LINK_LENGTH_M = 250 m ergibt
sich bei 83 km/h eine Fahrzeit von ~10.7 s (10 Zeitschritte) und ein Fehler < 10 %.

Aufruf:
    python -m src.convert_vdri_to_network [VDRI_PATH] [OUTPUT_PATH]
    # Default: data/longhaul.vdri → data/longhaul_network.xml.gz
"""

import csv
import gzip
import sys
from pathlib import Path

# Standard-Pfade (ueberschreibbar via CLI-Argumente)
VDRI_PATH = Path("data/longhaul.vdri")
OUTPUT_PATH = Path("data/longhaul_network.xml.gz")

# Netzwerk-Parameter
CAPACITY = 9999
PERMLANES = 1
MODES = "car"
MIN_FREESPEED_MS = 0.28  # ~1 km/h Minimum, damit Links passierbar bleiben

# Mindestlinklänge [m].
# Muss groß genug sein, damit die QSim-Zeitdiskretisierung (1s-Schritte) den
# Geschwindigkeitsfehler unter ~10 % haelt:
#   t = length / freespeed  →  mind. ~10 Zeitschritte anstreben
#   Bei 85 km/h = 23.6 m/s: 250 m / 23.6 m/s = 10.6 s  (Fehler < 10 %)
MIN_LINK_LENGTH_M = 250


def read_vdri(path: Path) -> list[dict]:
    """Liest die .vdri-Datei und gibt eine Liste von Datenpunkten zurück."""
    points = []
    with open(path, newline="", encoding="utf-8-sig") as f:  # utf-8-sig entfernt BOM
        reader = csv.reader(f)
        next(reader)  # Header überspringen: <s>,<v>,<grad>,<stop>,HW
        for row in reader:
            points.append({
                "s":    float(row[0]),   # Distanz [m]
                "v":    float(row[1]),   # Sollgeschwindigkeit [km/h]
                "grad": float(row[2]),   # Steigung [%]
            })
    return points


def compute_z_coordinates(points: list[dict]) -> list[float]:
    """Berechnet kumulative Höhenkoordinaten aus den Gradienten."""
    z = [0.0]
    for i in range(len(points) - 1):
        ds = points[i + 1]["s"] - points[i]["s"]
        dz = points[i]["grad"] / 100.0 * ds
        z.append(z[-1] + dz)
    return z


def build_segments(points: list[dict], min_length: float) -> list[tuple[int, int]]:
    """
    Fasst aufeinanderfolgende Messpunkte zu Segmenten zusammen.

    Ein neues Segment beginnt, sobald die akkumulierte Streckenlänge
    >= min_length ist. Das letzte Segment kann kürzer sein.

    Returns:
        Liste von (start_idx, end_idx)-Paaren (beide inklusive).
    """
    segments = []
    start = 0
    for i in range(1, len(points)):
        length = points[i]["s"] - points[start]["s"]
        if length >= min_length:
            segments.append((start, i))
            start = i
    # Letztes (moeglicherweise kuerzeres) Segment
    if start < len(points) - 1:
        segments.append((start, len(points) - 1))
    return segments


def segment_freespeed(points: list[dict], start: int, end: int) -> float:
    """
    Berechnet die harmonische Mittgeschwindigkeit eines Segments.

    Die harmonische Mittelung (v = Strecke / Zeit) erhält die Gesamtfahrzeit
    des Segments und ist damit physikalisch korrekt für die Energieberechnung.

    Nullgeschwindigkeiten (Stoppstellen) werden durch MIN_FREESPEED_MS ersetzt,
    damit das Segment passierbar bleibt.
    """
    total_length = points[end]["s"] - points[start]["s"]
    if total_length <= 0:
        return MIN_FREESPEED_MS

    total_time = 0.0
    for i in range(start, end):
        ds = points[i + 1]["s"] - points[i]["s"]
        v_ms = max(points[i]["v"] / 3.6, MIN_FREESPEED_MS)
        total_time += ds / v_ms

    if total_time <= 0:
        return MIN_FREESPEED_MS

    return max(total_length / total_time, MIN_FREESPEED_MS)


def write_network(points: list[dict], z_coords: list[float], output: Path,
                  min_link_length: float = MIN_LINK_LENGTH_M):
    """Schreibt das aggregierte MATSim-Netzwerk als .xml.gz."""
    segments = build_segments(points, min_link_length)

    # Nodes nur an Segmentgrenzen (eindeutige Indizes)
    node_indices = []
    for start, end in segments:
        if not node_indices or node_indices[-1] != start:
            node_indices.append(start)
        node_indices.append(end)
    # Abbildung: Messpunkt-Index → fortlaufende Node-ID
    idx_to_node = {idx: nid for nid, idx in enumerate(node_indices)}

    with gzip.open(output, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write('<network>\n')

        # Nodes
        f.write('\t<nodes>\n')
        for idx in node_indices:
            nid = idx_to_node[idx]
            f.write(f'\t\t<node id="{nid}" x="{points[idx]["s"]:.1f}" y="0.0" z="{z_coords[idx]:.6f}"/>\n')
        f.write('\t</nodes>\n')

        # Links (ein Link pro Segment)
        f.write(f'\t<links capperiod="01:00:00" effectivecellsize="7.5" effectivelanewidth="3.75">\n')
        for seg_id, (start, end) in enumerate(segments):
            from_node = idx_to_node[start]
            to_node   = idx_to_node[end]
            length    = points[end]["s"] - points[start]["s"]
            freespeed = segment_freespeed(points, start, end)
            f.write(
                f'\t\t<link id="{seg_id}" from="{from_node}" to="{to_node}" '
                f'length="{length:.2f}" freespeed="{freespeed:.4f}" '
                f'capacity="{CAPACITY}" permlanes="{PERMLANES}" modes="{MODES}"/>\n'
            )
        f.write('\t</links>\n')
        f.write('</network>\n')

    return len(node_indices), len(segments)


def main():
    vdri_path   = Path(sys.argv[1]) if len(sys.argv) > 1 else VDRI_PATH
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT_PATH

    print(f"Lese {vdri_path}...")
    points = read_vdri(vdri_path)
    print(f"  {len(points)} Datenpunkte (s: 0 - {points[-1]['s']:.0f} m)")

    print("Berechne Hoehenkoordinaten...")
    z_coords = compute_z_coordinates(points)
    print(f"  Hoehenbereich: {min(z_coords):.2f} m bis {max(z_coords):.2f} m")

    print(f"Aggregiere zu Segmenten (MIN_LINK_LENGTH = {MIN_LINK_LENGTH_M} m)...")
    n_nodes, n_links = write_network(points, z_coords, output_path)
    print(f"  {n_nodes} Nodes, {n_links} Links (vorher: {len(points)} Nodes, {len(points)-1} Links)")
    print(f"  Reduktion: {(len(points)-1)/n_links:.0f}x weniger Links")
    print(f"Schreibe {output_path}... fertig!")


if __name__ == "__main__":
    main()
