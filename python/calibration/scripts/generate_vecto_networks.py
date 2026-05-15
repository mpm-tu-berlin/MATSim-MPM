"""
Generiert MATSim-Netzwerke aus VECTO-Fahrzyklus-Dateien (.vdri).

Pro Aufruf werden eine oder mehrere Netzauflösungen erzeugt. Segmente werden
per harmonischem Mittel zu Links aggregiert (physikalisch korrekte Gesamtfahrzeit).

Dateikonvention (einheitlich): `<stem>_<N>m.xml.gz` und `plans_<N>m.xml` —
auch fuer 1m. Die alte unsuffixierte 1m-Datei wird nicht mehr erzeugt.

QSim-Zeitdiskretisierung — passende timeStepSize pro Auflösung (timeStepSize=1):
  1m:   effektive Geschwindigkeit = 1 m/s, Trip auf 100km nicht moeglich
        -> erfordert timeStepSize=0.04 (CLI-Override im MATSim-Aufruf)
  100m: bei 1s ca. 13 % zu langsam, bei 0.04s vernachlaessigbar
  250m: bei 1s nur ~1.5 % zu langsam -> ok fuer Standard-Kalibrierung

Plans-Varianten: Pro Netzauflösung wird je Szenario eine `plans_<N>m.xml`
neben der bestehenden plans.xml abgelegt (scenarios/VECTO_<Mission>*/plans_1m.xml
etc.). Die urspruengliche plans.xml bleibt unveraendert (Default-250m-Setup
nicht brechen). Im MATSim-Aufruf waehlt man die passende Variante per
--config:plans.inputPlansFile. Mit --skip-plans deaktivierbar.

Ausgabe: direkt in die MATSim-Szenarioverzeichnisse (relativ zum Projektroot).

Aufruf:
    .venv/Scripts/python scripts/generate_vecto_networks.py                       # all-Preset (1, 100, 250)
    .venv/Scripts/python scripts/generate_vecto_networks.py --resolution 250
    .venv/Scripts/python scripts/generate_vecto_networks.py --resolution 1 100 500
    .venv/Scripts/python scripts/generate_vecto_networks.py --resolution all
    .venv/Scripts/python scripts/generate_vecto_networks.py --resolution 1 --skip-plans
"""

import argparse
import csv
import gzip
import re
import sys
from pathlib import Path

# Sicherstellt dass src-Paket aus python/calibration/ gefunden wird
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR, MATSIM_MPM_DIR

SCENES_DIR = MATSIM_MPM_DIR / "scenarios"

FREESPEED_MIN_MS = 0.5  # Mindest-Freispeed fuer Stoppstellen [m/s]

# Default-Preset bei `--resolution all`.
DEFAULT_RESOLUTIONS_M: list[int] = [1, 100, 250]

# Konfiguration: VDRI-Datei -> Ausgabeverzeichnis + Dateinamen-Stamm
VDRI_CONFIGS = [
    {
        "name":     "LongHaul",
        "vdri":     DATA_DIR / "LongHaul.vdri",
        "out_dir":  SCENES_DIR / "VECTO_Longhaul",
        "out_stem": "longhaul_network",
    },
    {
        "name":     "RegionalDelivery",
        "vdri":     DATA_DIR / "RegionalDelivery.vdri",
        "out_dir":  SCENES_DIR / "VECTO_RegionalDelivery",
        "out_stem": "regional_delivery_network",
    },
]


def network_suffix(resolution_m: int) -> str:
    """Einheitlicher Suffix `_<N>m` fuer Netzwerk- und Plans-Dateien."""
    return f"_{resolution_m}m"


# ---------------------------------------------------------------------------
# VDRI lesen und Elevation berechnen
# ---------------------------------------------------------------------------

def read_vdri(path: Path) -> tuple[list[float], list[float], list[float]]:
    """Liest .vdri-Datei; gibt (s_m, v_kmh, grad_pct) zurueck."""
    s_m, v_kmh, grad_pct = [], [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            s_m.append(float(row["<s>"]))
            v_kmh.append(float(row["<v>"]))
            grad_pct.append(float(row["<grad>"]))
    return s_m, v_kmh, grad_pct


def compute_elevation(s_m: list[float], grad_pct: list[float]) -> list[float]:
    """Berechnet kumulative Hoehenkoordinaten [m] aus Gradienten."""
    z = [0.0]
    for i in range(len(s_m) - 1):
        dz = grad_pct[i] / 100.0 * (s_m[i + 1] - s_m[i])
        z.append(z[-1] + dz)
    return z


# ---------------------------------------------------------------------------
# Segment-Aggregation
# ---------------------------------------------------------------------------

def build_segments(s_m: list[float], min_length_m: float) -> list[tuple[int, int]]:
    """
    Fasst VDRI-Messpunkte zu Segmenten mit >= min_length_m zusammen.

    Returns:
        Liste von (start_idx, end_idx)-Paaren (beide inklusive).
    """
    segments = []
    start = 0
    for i in range(1, len(s_m)):
        if s_m[i] - s_m[start] >= min_length_m:
            segments.append((start, i))
            start = i
    if start < len(s_m) - 1:
        segments.append((start, len(s_m) - 1))
    return segments


def segment_freespeed(s_m: list[float], v_kmh: list[float],
                      start: int, end: int) -> float:
    """
    Harmonisches Mittel der Segmentgeschwindigkeit [m/s].

    v_harm = Streckenlänge / Gesamtfahrzeit — erhält die physikalische
    Gesamtfahrzeit des Segments und ist damit korrekt für die Energieberechnung.
    """
    total_length = s_m[end] - s_m[start]
    if total_length <= 0:
        return FREESPEED_MIN_MS

    total_time = sum(
        (s_m[i + 1] - s_m[i]) / max(v_kmh[i] / 3.6, FREESPEED_MIN_MS)
        for i in range(start, end)
    )

    if total_time <= 0:
        return FREESPEED_MIN_MS
    return max(total_length / total_time, FREESPEED_MIN_MS)


# ---------------------------------------------------------------------------
# Netzwerk schreiben
# ---------------------------------------------------------------------------

def write_network(output_path: Path, s_m: list[float], v_kmh: list[float],
                  z_m: list[float], min_segment_m: float) -> tuple[int, int]:
    """Schreibt MATSim-Netzwerk als .xml.gz."""
    segments   = build_segments(s_m, min_segment_m)

    # Eindeutige Node-Indizes an Segmentgrenzen
    node_indices: list[int] = []
    for start, end in segments:
        if not node_indices or node_indices[-1] != start:
            node_indices.append(start)
        node_indices.append(end)
    idx_to_nid = {idx: nid for nid, idx in enumerate(node_indices)}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write('<network>\n\t<nodes>\n')
        for idx in node_indices:
            nid = idx_to_nid[idx]
            f.write(f'\t\t<node id="{nid}" x="{s_m[idx]:.1f}" y="0.0" z="{z_m[idx]:.6f}"/>\n')
        f.write('\t</nodes>\n')
        f.write('\t<links capperiod="01:00:00" effectivecellsize="7.5" effectivelanewidth="3.75">\n')
        for seg_id, (start, end) in enumerate(segments):
            length    = s_m[end] - s_m[start]
            freespeed = segment_freespeed(s_m, v_kmh, start, end)
            f.write(
                f'\t\t<link id="{seg_id}" from="{idx_to_nid[start]}" to="{idx_to_nid[end]}"'
                f' length="{length:.2f}" freespeed="{freespeed:.4f}"'
                f' capacity="9999" permlanes="1" modes="car"/>\n'
            )
        f.write('\t</links>\n</network>\n')

    return len(node_indices), len(segments)


# ---------------------------------------------------------------------------
# Plans an Netzauflösung anpassen
# ---------------------------------------------------------------------------

# Mission -> Glob fuer alle Szenarien, deren plans.xml auf diesem Netz fahren.
# Die Basis-Szenarien (VECTO_Longhaul, VECTO_RegionalDelivery) werden mit
# erfasst, da ihr plans.xml ebenfalls vom End-Link der gewaehlten Auflösung
# abhaengt.
_MISSION_SCENARIO_GLOBS: dict[str, str] = {
    "LongHaul":         "VECTO_Longhaul*",
    "RegionalDelivery": "VECTO_RegionalDelivery*",
}

_END_LINK_RE = re.compile(
    r'(<activity\s+type="end"[^/]*?\blink=")[^"]+(")'
)


def write_plans_variant(src_plans: Path, dst_plans: Path, last_link_id: int) -> int:
    """Erzeugt eine resolutionsspezifische Plans-Variante aus src_plans.

    Patcht nur das link-Attribut aller Trip-Endaktivitaeten.
    Returns Anzahl der gepatchten Aktivitaeten.
    """
    text = src_plans.read_text(encoding="utf-8")
    new_text, n_subs = _END_LINK_RE.subn(
        lambda m: f'{m.group(1)}{last_link_id}{m.group(2)}',
        text,
    )
    dst_plans.write_text(new_text, encoding="utf-8")
    return n_subs


def patch_plans_for_mission(mission: str, resolution_m: int, last_link_id: int) -> None:
    glob = _MISSION_SCENARIO_GLOBS.get(mission)
    if glob is None:
        return
    suffix = network_suffix(resolution_m)
    for scenario_dir in sorted(SCENES_DIR.glob(glob)):
        src = scenario_dir / "plans.xml"
        if not src.exists():
            continue
        dst = scenario_dir / f"plans{suffix}.xml"
        n_subs = write_plans_variant(src, dst, last_link_id)
        print(
            f"    plans: {scenario_dir.name}/{dst.name}"
            f"  end_link={last_link_id}  ({n_subs} Aktivitaeten gepatcht)"
        )


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def generate(resolution_m: int, patch_plans: bool = True) -> None:
    """Generiert Netzwerke fuer alle VDRI-Konfigurationen mit gegebener Auflösung."""
    if resolution_m <= 0:
        raise ValueError(f"resolution_m muss positiv sein, war: {resolution_m}")

    suffix = network_suffix(resolution_m)
    print(f"\n[Auflösung {resolution_m}m]  Suffix: '{suffix}'")

    for cfg in VDRI_CONFIGS:
        name = cfg["name"]
        vdri_path = cfg["vdri"]
        output_path = cfg["out_dir"] / f"{cfg['out_stem']}{suffix}.xml.gz"

        if not vdri_path.exists():
            print(f"  [{name}] WARNUNG: VDRI-Datei nicht gefunden: {vdri_path}")
            continue

        print(f"  [{name}] Lese {vdri_path.name} ...")
        s_m, v_kmh, grad_pct = read_vdri(vdri_path)
        z_m = compute_elevation(s_m, grad_pct)

        n_nodes, n_links = write_network(output_path, s_m, v_kmh, z_m, float(resolution_m))
        print(
            f"  [{name}] {n_nodes} Nodes, {n_links} Links"
            f"  (war: {len(s_m)} Punkte)  -> {output_path.name}"
        )

        if patch_plans:
            patch_plans_for_mission(name, resolution_m, last_link_id=n_links - 1)


def _parse_resolutions(values: list[str]) -> list[int]:
    """`['all']` -> Preset; sonst Liste positiver Integer."""
    if values == ["all"]:
        return list(DEFAULT_RESOLUTIONS_M)
    out: list[int] = []
    for v in values:
        if v == "all":
            raise argparse.ArgumentTypeError("'all' darf nur allein stehen.")
        try:
            n = int(v)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Keine gueltige Auflösung: {v}") from exc
        if n <= 0:
            raise argparse.ArgumentTypeError(f"Auflösung muss positiv sein: {n}")
        out.append(n)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--resolution",
        nargs="+",
        default=["all"],
        metavar="N",
        help=("Netzauflösung(en) in Metern (Mindest-Segmentlänge). Ein oder mehrere "
              "positive Integer, oder 'all' fuer das Preset "
              f"{DEFAULT_RESOLUTIONS_M}. Default: all."),
    )
    parser.add_argument(
        "--skip-plans",
        action="store_true",
        help="plans_<N>m.xml-Varianten nicht erzeugen (nur Netze schreiben).",
    )
    args = parser.parse_args()

    resolutions = _parse_resolutions(args.resolution)

    for res in resolutions:
        generate(res, patch_plans=not args.skip_plans)

    print("\nFertig.")


if __name__ == "__main__":
    main()