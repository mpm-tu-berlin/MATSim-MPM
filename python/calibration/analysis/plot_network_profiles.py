"""
Vergleich der Hoehenprofile: VECTO-Fahrzyklus vs. MATSim-Netz.

Erzeugt network_profiles.html mit zwei interaktiven Subplots:
  - Long Haul
  - Regional Delivery

x-Achse: Entfernung vom Start [km] (zoombar)
y-Achse: Hoehe ueber Startpunkt [m]

Aufruf:
    .venv/Scripts/python analysis/plot_network_profiles.py
"""

import gzip
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Sicherstellt dass src-Paket aus python/calibration/ gefunden wird
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.config import DATA_DIR, MATSIM_MPM_DIR

_CALIB_ROOT  = Path(__file__).resolve().parent.parent
MATSIM_DIR   = MATSIM_MPM_DIR / "scenarios"

VDRI_LH = DATA_DIR   / "LongHaul.vdri"
VDRI_RD = DATA_DIR   / "RegionalDelivery.vdri"
NET_LH  = MATSIM_DIR / "VECTO_Longhaul"         / "longhaul_network_1m.xml.gz"
NET_RD  = MATSIM_DIR / "VECTO_RegionalDelivery"  / "regional_delivery_network_1m.xml.gz"

OUTPUT  = _CALIB_ROOT / "results" / "network_profiles.html"


def read_vdri(path: Path) -> tuple[list[float], list[float]]:
    """Liest eine VDRI-Datei und rekonstruiert Distanz- und Hoehenprofile.

    <s>: kumulative Distanz [m] (kein Zeitstempel!), <grad>: Steigung [%].
    Zwischen zwei Eintraegen gelten konstante Steigung und Geschwindigkeit.
    Hoehenänderung: dz = ds * grad / 100.

    Returns:
        (dist_km, elev_m): Distanz [km] und Hoehe [m].
    """
    df = pd.read_csv(path)
    df.columns = [c.strip("<>") for c in df.columns]  # <s> -> s, usw.

    dist_m = list(df["s"].astype(float))
    elev_m = [0.0]
    cum_z  = 0.0

    for i in range(len(df) - 1):
        ds = float(df["s"].iloc[i + 1] - df["s"].iloc[i])
        g  = float(df["grad"].iloc[i])
        cum_z += ds * g / 100.0
        elev_m.append(cum_z)

    return [d / 1000.0 for d in dist_m], elev_m


def read_network(path: Path) -> tuple[list[float], list[float]]:
    """Liest ein MATSim-Netzwerk (XML oder XML.GZ) und gibt Distanz-/Hoehenprofil zurueck.

    Knoten werden nach numerischer ID sortiert.
    x-Koordinate = kumulative Distanz [m], z-Koordinate = Hoehe [m].

    Returns:
        (dist_km, elev_m): Distanz [km] und Hoehe [m] entlang der Route.
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as f:
        tree = ET.parse(f)

    nodes = sorted(
        (
            (int(n.get("id")), float(n.get("x", 0.0)), float(n.get("z", 0.0)))
            for n in tree.findall(".//node")
        ),
        key=lambda t: t[0],
    )

    dist_km = [n[1] / 1000.0 for n in nodes]
    elev_m  = [n[2]           for n in nodes]
    return dist_km, elev_m


def main() -> None:
    scenarios = [
        ("Long Haul",         VDRI_LH, NET_LH),
        ("Regional Delivery", VDRI_RD, NET_RD),
    ]

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=[s[0] for s in scenarios],
        shared_xaxes=False,
        vertical_spacing=0.14,
    )

    COLOR_VECTO  = "#1f77b4"
    COLOR_MATSIM = "#ff7f0e"

    for row, (label, vdri_path, net_path) in enumerate(scenarios, start=1):
        print(f"Lese {label} ...")
        vd_dist, vd_elev = read_vdri(vdri_path)
        ms_dist, ms_elev = read_network(net_path)

        fig.add_trace(
            go.Scatter(
                x=vd_dist, y=vd_elev,
                mode="lines",
                name="VECTO",
                line=dict(color=COLOR_VECTO, width=1.5),
                legendgroup="VECTO",
                showlegend=(row == 1),
            ),
            row=row, col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=ms_dist, y=ms_elev,
                mode="lines",
                name="MATSim",
                line=dict(color=COLOR_MATSIM, width=1.5, dash="dash"),
                legendgroup="MATSim",
                showlegend=(row == 1),
            ),
            row=row, col=1,
        )

    fig.update_xaxes(title_text="Entfernung [km]", showgrid=True, gridcolor="#e0e0e0")
    fig.update_yaxes(title_text="Höhe [m]",        showgrid=True, gridcolor="#e0e0e0")
    fig.update_layout(
        title="Höhenprofil: VECTO vs. MATSim-Netz",
        height=850,
        hovermode="x unified",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(OUTPUT), include_plotlyjs="cdn")
    print(f"Gespeichert: {OUTPUT}")


if __name__ == "__main__":
    main()