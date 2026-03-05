"""
Generiert MATSim-Netzwerke direkt aus VECTO-Fahrzyklus-Dateien (.vdri).

Jede Zeile der .vdri-Datei wird ein Node; aufeinanderfolgende Nodes werden durch
einen Link verbunden. Die Ausgabe wird zeilenweise in eine gzip-Datei gestreamt
(kein ElementTree, zu langsam bei ~100k Elementen).

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
            r"\VECTO_Longhaul\longhaul_network.xml.gz"
        ),
        "name": "LongHaul",
    },
    {
        "vdri":   os.path.join(KALIBRIERUNGSPROJEKT, "data", "RegionalDelivery.vdri"),
        "ausgabe": (
            r"C:\Users\Tobias\IdeaProjects\MATSim-MPM\scenarios"
            r"\VECTO_RegionalDelivery\regional_delivery_network.xml.gz"
        ),
        "name": "RegionalDelivery",
    },
]

# Minimale Freigeschwindigkeit für Haltepunkte (v=0) in m/s
FREESPEED_MIN = 0.5


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def lade_vdri(pfad: str) -> tuple[list[float], list[float], list[float]]:
    """Liest eine .vdri-Datei und gibt (s_m, v_kmh, grad_pct) zurück."""
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
    Berechnet die Höhe jedes Nodes via kumulative Summe.
    z[0] = 0; z[i] = z[i-1] + grad[i-1]/100 * (s[i] - s[i-1])
    """
    n = len(s_m)
    z_m = [0.0] * n
    for i in range(1, n):
        delta_s = s_m[i] - s_m[i - 1]
        z_m[i] = z_m[i - 1] + grad_pct[i - 1] / 100.0 * delta_s
    return z_m


def schreibe_netzwerk(
    ausgabepfad: str,
    s_m: list[float],
    v_kmh: list[float],
    z_m: list[float],
    name: str,
) -> None:
    """Schreibt das MATSim-Netzwerk zeilenweise in eine gzip-Datei."""
    n_nodes = len(s_m)
    n_links = n_nodes - 1

    os.makedirs(os.path.dirname(ausgabepfad), exist_ok=True)

    with gzip.open(ausgabepfad, "wt", encoding="utf-8") as f:
        # XML-Kopf
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(
            '<!DOCTYPE network SYSTEM '
            '"http://www.matsim.org/files/dtd/network_v2.dtd">\n'
        )
        f.write("<network>\n")

        # Nodes
        f.write("\t<nodes>\n")
        for i in range(n_nodes):
            f.write(
                f'\t\t<node id="{i}" x="{s_m[i]}" y="0" z="{z_m[i]:.6f}"/>\n'
            )
        f.write("\t</nodes>\n")

        # Links
        f.write(
            '\t<links capperiod="01:00:00" '
            'effectivecellsize="7.5" effectivelanewidth="3.75">\n'
        )
        for i in range(n_links):
            laenge = s_m[i + 1] - s_m[i]
            freespeed = max(v_kmh[i] / 3.6, FREESPEED_MIN)
            f.write(
                f'\t\t<link id="{i}" from="{i}" to="{i + 1}"'
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
        f" -> {ausgabepfad}"
    )


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    for szenario in SZENARIEN:
        name = szenario["name"]
        print(f"[{name}] Lese {szenario['vdri']} …")

        s_m, v_kmh, grad_pct = lade_vdri(szenario["vdri"])

        print(f"[{name}] {len(s_m)} Einträge geladen.")
        print(
            f"[{name}] Distanz: {s_m[0]:.0f} m – {s_m[-1]:.0f} m"
            f" ({s_m[-1] / 1000:.3f} km)"
        )

        z_m = berechne_elevation(s_m, grad_pct)

        schreibe_netzwerk(szenario["ausgabe"], s_m, v_kmh, z_m, name)


if __name__ == "__main__":
    main()
