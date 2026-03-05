"""
MATSim-Geschwindigkeit vs. VECTO-Sollprofil (Long Haul).

Aufruf:
    .venv/Scripts/python analyse_resistance_v2.py <resistance_debug.csv>

Ausgabe:
    results/speed_comparison_lh.html
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

PROJECT_ROOT  = Path(__file__).resolve().parent
OUTPUT        = PROJECT_ROOT / "results" / "speed_comparison_lh.html"
VECTO_LH_VDRI = PROJECT_ROOT / "data" / "LongHaul.vdri"


def main() -> None:
    if len(sys.argv) >= 2:
        csv_path = Path(sys.argv[1])
    else:
        csv_path = PROJECT_ROOT / "resistance_debug.csv"

    if not csv_path.exists():
        print(f"Datei nicht gefunden: {csv_path}")
        sys.exit(1)

    # --- MATSim-Daten laden ---
    df = pd.read_csv(csv_path, on_bad_lines='warn')
    if "vExit_kmh" in df.columns:
        df = df.rename(columns={"vExit_kmh": "speed_kmh"})

    # Nur LH-Fahrzeuge
    df_lh = df[df["vehicleId"].str.contains("_lh_", na=False)].copy()

    # --- VECTO-Sollprofil laden ---
    vdri = pd.read_csv(VECTO_LH_VDRI)
    vdri.columns = ["s_m", "v_kmh", "grad_pct", "stop", "hw"]

    # --- Plot ---
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=vdri["s_m"] / 1000.0,
        y=vdri["v_kmh"],
        mode="lines",
        name="VECTO Soll",
        line=dict(color="black", width=1.5, dash="dash"),
        opacity=0.7,
    ))

    for vid, grp in df_lh.groupby("vehicleId", sort=True):
        grp = grp.copy()
        grp["cum_dist_km"] = grp["length_m"].cumsum() / 1000.0
        fig.add_trace(go.Scatter(
            x=grp["cum_dist_km"],
            y=grp["speed_kmh"],
            mode="lines",
            name=str(vid),
            line=dict(width=1),
        ))

    fig.update_layout(
        title="MATSim-Geschwindigkeit vs. VECTO-Sollprofil (Long Haul)",
        xaxis_title="Streckenposition [km]",
        yaxis_title="Geschwindigkeit [km/h]",
        hovermode="x unified",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(OUTPUT), include_plotlyjs="cdn")
    print(f"Gespeichert: {OUTPUT}")


if __name__ == "__main__":
    main()
