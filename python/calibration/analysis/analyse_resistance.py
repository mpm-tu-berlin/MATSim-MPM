"""
Analysiert resistance_debug.csv: Energiebilanz pro Fahrzeug und Fehlerdiagnose
gegenueber VECTO-Referenzwerten.

Da LongHaul- und RegionalDelivery-Lauf jeweils eine eigene resistance_debug.csv
ins Arbeitsverzeichnis schreiben (und sich dabei gegenseitig ueberschreiben wuerden),
werden die Dateien manuell umbenannt und dann gemeinsam uebergeben:

Aufruf:
    # Einzelne Datei (nur ein Szenario):
    .venv/Scripts/python analysis/analyse_resistance.py resistance_debug_lh.csv

    # Beide Szenarien kombiniert:
    .venv/Scripts/python analysis/analyse_resistance.py resistance_debug_lh.csv resistance_debug_rd.csv

Ausgabe:
    results/resistance_analysis.html  — drei interaktive Subplots:
      1. Energieaufschluesselung pro Fahrzeug (Roll, Luft, Steigung, Kinetik, Rekup)
      2. Verbrauch MATSim vs. VECTO [kWh/km]
      3. Fehler vs. Gesamtmasse (Fehlerkorrelation mit Beladung)
"""

import subprocess
import sys
import tempfile
from pathlib import Path

# Sicherstellt dass src-Paket aus python/calibration/ gefunden wird
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.config import VEHICLE_GROUPS
from src.error_computation import load_reference

_CALIB_ROOT   = Path(__file__).resolve().parent.parent
OUTPUT         = _CALIB_ROOT / "results" / "resistance_analysis.html"
OUTPUT_SPEED   = _CALIB_ROOT / "results" / "speed_profile.html"
VECTO_LH_VDRI  = _CALIB_ROOT / "data" / "LongHaul.vdri"
VECTO_RD_VDRI  = _CALIB_ROOT / "data" / "RegionalDelivery.vdri"

# Fahrzeugmetadaten direkt aus vehicles.xml aller Szenarien.
# vehicle_id -> {mass_kg, payload_kg}
VEHICLE_META: dict[str, dict] = {
    # Volvo FH 42T E
    "truck_fh42te_lh_low":       {"mass": 18166, "payload":  2600},
    "truck_fh42te_lh_high":      {"mass": 18166, "payload": 19300},
    "truck_fh42te_rd_low":       {"mass": 18166, "payload":  2600},
    "truck_fh42te_rd_high":      {"mass": 18166, "payload": 12900},
    # IVECO S-eWay
    "truck_iveco_seway_lh_low":  {"mass": 19282, "payload":  2600},
    "truck_iveco_seway_lh_high": {"mass": 19282, "payload": 19300},
    "truck_iveco_seway_rd_low":  {"mass": 19282, "payload":  2600},
    "truck_iveco_seway_rd_high": {"mass": 19282, "payload": 12900},
}

# vehicle_id -> Mission (fuer Referenzdaten-Lookup und Farbkodierung)
VEHICLE_MISSION: dict[str, str] = {
    "truck_fh42te_lh_low":       "LongHaul",
    "truck_fh42te_lh_high":      "LongHaul",
    "truck_fh42te_rd_low":       "RegionalDelivery",
    "truck_fh42te_rd_high":      "RegionalDelivery",
    "truck_iveco_seway_lh_low":  "LongHaul",
    "truck_iveco_seway_lh_high": "LongHaul",
    "truck_iveco_seway_rd_low":  "RegionalDelivery",
    "truck_iveco_seway_rd_high": "RegionalDelivery",
}


# ---------------------------------------------------------------------------
# Daten laden und aggregieren
# ---------------------------------------------------------------------------

def load_debug_csv(path: Path) -> pd.DataFrame:
    """Laedt und validiert die resistance_debug.csv.

    Unterstuetzt beide Spaltenbezeichnungen:
      - Neu: tPhysical_s, vEntry_kmh, vExit_kmh
      - Alt: travelTime_s, speed_kmh (rueckwaertskompatibel)
    """
    df = pd.read_csv(path, on_bad_lines='warn')

    # Spalten-Aliase: neues Format -> altes Format normalisieren
    if "tPhysical_s" in df.columns and "travelTime_s" not in df.columns:
        df = df.rename(columns={"tPhysical_s": "travelTime_s"})
    if "vExit_kmh" in df.columns and "speed_kmh" not in df.columns:
        df = df.rename(columns={"vExit_kmh": "speed_kmh"})

    required = {
        "vehicleId", "length_m", "travelTime_s", "speed_kmh",
        "pRoll_W", "pAero_W", "pGrav_W", "pKin_W",
        "pMechTotal_W", "pBattery_W", "energy_Wh",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Fehlende Spalten in {path.name}: {missing}")
    return df


def aggregate_per_vehicle(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregiert Energiebilanz pro Fahrzeug.

    Alle Energiewerte in kWh.  Vorzeichen:
      + = Energie wird aufgewendet (Roll, Luft, bergauf, beschleunigen)
      - = Energie wird zurueckgewonnen (bergab, bremsen, Rekuperation)
    """
    df = df.copy()

    # Energie je Link [Wh] = Leistung [W] * Zeit [s] / 3600
    df["E_roll_Wh"] = df["pRoll_W"] * df["travelTime_s"] / 3600.0
    df["E_aero_Wh"] = df["pAero_W"] * df["travelTime_s"] / 3600.0
    df["E_grade_Wh"] = df["pGrav_W"] * df["travelTime_s"] / 3600.0
    df["E_acc_Wh"]  = df["pKin_W"]  * df["travelTime_s"] / 3600.0

    # Aufwaerts- und Abwaerts-Anteile separat (fuer stacked bar)
    df["E_grade_up_Wh"]   = df["E_grade_Wh"].clip(lower=0)
    df["E_grade_down_Wh"] = df["E_grade_Wh"].clip(upper=0)
    df["E_acc_up_Wh"]    = df["E_acc_Wh"].clip(lower=0)
    df["E_acc_down_Wh"]  = df["E_acc_Wh"].clip(upper=0)

    # Rekuperations-Links: pMechTotal_W < 0
    df["E_recup_Wh"] = df["energy_Wh"].where(df["pMechTotal_W"] < 0, 0.0)

    agg = (
        df.groupby("vehicleId")
        .agg(
            dist_km        = ("length_m",       lambda x: x.sum() / 1000.0),
            E_roll_Wh      = ("E_roll_Wh",      "sum"),
            E_aero_Wh      = ("E_aero_Wh",      "sum"),
            E_grade_up_Wh   = ("E_grade_up_Wh",   "sum"),
            E_grade_down_Wh = ("E_grade_down_Wh", "sum"),
            E_acc_up_Wh    = ("E_acc_up_Wh",    "sum"),
            E_acc_down_Wh  = ("E_acc_down_Wh",  "sum"),
            E_recup_Wh     = ("E_recup_Wh",     "sum"),
            E_bat_Wh       = ("energy_Wh",       "sum"),
        )
        .reset_index()
    )

    # Einheit Wh -> kWh
    wh_cols = [c for c in agg.columns if c.endswith("_Wh")]
    for col in wh_cols:
        agg[col.replace("_Wh", "_kWh")] = agg[col] / 1000.0
    agg.drop(columns=wh_cols, inplace=True)

    # Nettowerte fuer die Zusammenfasstung
    agg["E_grade_net_kWh"] = agg["E_grade_up_kWh"] + agg["E_grade_down_kWh"]
    agg["E_acc_net_kWh"]  = agg["E_acc_up_kWh"]  + agg["E_acc_down_kWh"]

    # Verbrauch pro km
    agg["E_bat_per_km"] = agg["E_bat_kWh"] / agg["dist_km"]

    return agg.set_index("vehicleId")


def enrich_with_meta_and_ref(agg: pd.DataFrame) -> pd.DataFrame:
    """Ergaenzt Fahrzeugmetadaten und VECTO-Referenzwerte."""
    agg["mass_kg"]    = agg.index.map(lambda v: VEHICLE_META.get(v, {}).get("mass",    0))
    agg["payload_kg"] = agg.index.map(lambda v: VEHICLE_META.get(v, {}).get("payload", 0))
    agg["total_t"]    = (agg["mass_kg"] + agg["payload_kg"]) / 1000.0
    agg["mission"]    = agg.index.map(VEHICLE_MISSION)

    # VECTO-Referenzwerte: alle Fahrzeuggruppen und Missionen laden
    ref_all: dict[str, dict] = {}
    for group in VEHICLE_GROUPS:
        for mission in ("LongHaul", "RegionalDelivery"):
            try:
                ref_all.update(load_reference(mission, vehicle_group=group))
            except Exception:
                pass

    agg["vecto_kwh_per_km"] = agg.index.map(
        lambda v: ref_all[v]["ee_kwh_per_km"] if v in ref_all else float("nan")
    )
    agg["diff_pct"] = (
        (agg["E_bat_per_km"] - agg["vecto_kwh_per_km"])
        / agg["vecto_kwh_per_km"] * 100.0
    )
    return agg


# ---------------------------------------------------------------------------
# Konsolenausgabe
# ---------------------------------------------------------------------------

def print_table(agg: pd.DataFrame) -> None:
    """Gibt eine Zusammenfassungstabelle auf der Konsole aus."""
    col_w = 35
    header1 = (
        f"{'Fahrzeug':<{col_w}} {'Masse':>6} "
        f"{'E_roll':>7} {'E_aero':>7} {'E_grade':>7} {'E_acc':>7} "
        f"{'E_recup':>7} "
        f"{'E/km':>7} {'VECTO':>7} {'Diff':>8}"
    )
    header2 = (
        f"{'':>{col_w}} {'[t]':>6} "
        f"{'[kWh]':>7} {'[kWh]':>7} {'[kWh]':>7} {'[kWh]':>7} "
        f"{'[kWh]':>7} "
        f"{'[kWh/km]':>7} {'[kWh/km]':>7} {'[%]':>8}"
    )
    sep = "-" * len(header1)
    print(f"\n{header1}\n{header2}\n{sep}")
    for vid, row in agg.iterrows():
        vecto = f"{row['vecto_kwh_per_km']:7.4f}" if pd.notna(row["vecto_kwh_per_km"]) else "       ?"
        diff  = f"{row['diff_pct']:+8.2f}" if pd.notna(row["diff_pct"]) else "        ?"
        print(
            f"{vid:<{col_w}} {row['total_t']:6.1f} "
            f"{row['E_roll_kWh']:7.2f} {row['E_aero_kWh']:7.2f} "
            f"{row['E_grade_net_kWh']:7.2f} {row['E_acc_net_kWh']:7.2f} "
            f"{row['E_recup_kWh']:7.2f} "
            f"{row['E_bat_per_km']:7.4f} {vecto} {diff}"
        )
    print()


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

_MISSION_COLOR = {"LongHaul": "#1f77b4", "RegionalDelivery": "#ff7f0e"}


def load_vecto_lh_cycle() -> pd.DataFrame | None:
    """Laedt das VECTO-LongHaul-Fahrprofil (LongHaul.vdri) als DataFrame.

    Gibt None zurueck, wenn die Datei nicht vorhanden ist.
    Spalten im Ergebnis: dist_km [km], v_kmh [km/h].
    """
    if not VECTO_LH_VDRI.exists():
        return None
    vdri = pd.read_csv(VECTO_LH_VDRI)
    vdri.columns = ["s_m", "v_kmh", "grad_pct", "stop", "hw"]
    vdri["dist_km"] = vdri["s_m"] / 1000.0
    return vdri[["dist_km", "v_kmh"]]


def load_vecto_rd_cycle() -> pd.DataFrame | None:
    """Laedt das VECTO-RegionalDelivery-Fahrprofil (RegionalDelivery.vdri) als DataFrame.

    Gibt None zurueck, wenn die Datei nicht vorhanden ist.
    Spalten im Ergebnis: dist_km [km], v_kmh [km/h].
    """
    if not VECTO_RD_VDRI.exists():
        return None
    vdri = pd.read_csv(VECTO_RD_VDRI)
    vdri.columns = ["s_m", "v_kmh", "grad_pct", "stop", "hw"]
    vdri["dist_km"] = vdri["s_m"] / 1000.0
    return vdri[["dist_km", "v_kmh"]]


def make_speed_figure(df: pd.DataFrame) -> go.Figure:
    """Effektive Geschwindigkeit, Hoehenprofile und Leistungskomponenten pro Link."""
    df = df.copy()

    # 1) Hoehenunterschied je Link [m]
    df["dz_m"] = df["grade_pct"] / 100.0 * df["length_m"]

    # 2) Kumulierte Distanz und Hoehe je Fahrzeug
    df["cum_dist_km"] = df.groupby("vehicleId", sort=False)["length_m"].cumsum() / 1000.0
    df["cum_elev_m"]  = df.groupby("vehicleId", sort=False)["dz_m"].cumsum()

    # 3) Zweireihige Figure: Zeile 1 = Geschwindigkeit + Hoehe, Zeile 2 = Leistung
    fig = make_subplots(
        rows=2, cols=1,
        specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.5, 0.5],
        subplot_titles=["Geschwindigkeit und Hoehenprofil", "Leistung pro Link"],
    )

    # --- Zeile 1: VECTO-LH-Sollprofil (nur wenn LH-Fahrzeuge vorhanden) ---
    has_lh = df["vehicleId"].str.contains("_lh_", na=False).any()
    if has_lh:
        vecto = load_vecto_lh_cycle()
        if vecto is not None:
            fig.add_trace(
                go.Scatter(
                    x=vecto["dist_km"], y=vecto["v_kmh"],
                    mode="lines", name="VECTO LH Soll",
                    line=dict(color="black", width=1.5, dash="dash"),
                    opacity=0.6,
                    legendgroup="vecto",
                ),
                row=1, col=1, secondary_y=False,
            )

    # --- Zeile 1: VECTO-RD-Sollprofil (nur wenn RD-Fahrzeuge vorhanden) ---
    has_rd = df["vehicleId"].str.contains("_rd_", na=False).any()
    if has_rd:
        vecto_rd = load_vecto_rd_cycle()
        if vecto_rd is not None:
            fig.add_trace(
                go.Scatter(
                    x=vecto_rd["dist_km"], y=vecto_rd["v_kmh"],
                    mode="lines", name="VECTO RD Soll",
                    line=dict(color="gray", width=1.5, dash="dash"),
                    opacity=0.6,
                    legendgroup="vecto_rd",
                ),
                row=1, col=1, secondary_y=False,
            )

    # --- Zeile 1: Effektive Geschwindigkeit je Fahrzeug ---
    for vid, grp in df.groupby("vehicleId", sort=False):
        fig.add_trace(
            go.Scatter(
                x=grp["cum_dist_km"], y=grp["speed_kmh"],
                mode="lines", name=str(vid),
                legendgroup=vid,
            ),
            row=1, col=1, secondary_y=False,
        )

    # --- Zeile 1: Hoehenprofile je Zyklus (sekundaere Achse) ---
    for cycle_tag, cycle_label, color in [
        ("_lh_", "LongHaul",         "#1f77b4"),
        ("_rd_", "RegionalDelivery", "#ff7f0e"),
    ]:
        cycle_vids = df.loc[df["vehicleId"].str.contains(cycle_tag, na=False), "vehicleId"].unique()
        if len(cycle_vids) == 0:
            continue
        grp = df[df["vehicleId"] == cycle_vids[0]]
        fig.add_trace(
            go.Scatter(
                x=grp["cum_dist_km"], y=grp["cum_elev_m"],
                mode="lines", name=f"Hoehe {cycle_label}",
                line=dict(dash="dot", color=color), opacity=0.5,
                legendgroup=f"elev_{cycle_label}",
            ),
            row=1, col=1, secondary_y=True,
        )

    # --- Zeile 2: Leistungskomponenten je Fahrzeug ---
    # pBattery_W immer sichtbar; Einzelkomponenten standardmaessig ausgeblendet.
    power_traces = [
        ("pBattery_W",   "P_Bat",      True),
        ("pMechTotal_W", "P_Mech",     "legendonly"),
        ("pRoll_W",      "P_Roll",     "legendonly"),
        ("pAero_W",      "P_Aero",     "legendonly"),
        ("pGrav_W",      "P_Steig",    "legendonly"),
        ("pKin_W",       "P_Kin",      "legendonly"),
    ]
    for vid, grp in df.groupby("vehicleId", sort=False):
        for col, label, visible in power_traces:
            fig.add_trace(
                go.Scatter(
                    x=grp["cum_dist_km"],
                    y=grp[col] / 1000.0,
                    mode="lines",
                    name=f"{vid} {label}",
                    legendgroup=f"{vid}_{label}",
                    visible=visible,
                    line=dict(width=1.5 if col == "pBattery_W" else 1),
                ),
                row=2, col=1,
            )

    fig.update_layout(
        title="Fahrwiderstandsanalyse: Geschwindigkeit und Leistung",
        height=900,
        template="plotly_white",
        hovermode="x unified",
        legend=dict(tracegroupgap=4),
    )
    fig.update_xaxes(title_text="Streckenposition [km]", row=2, col=1)
    fig.update_yaxes(title_text="v_eff [km/h]", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Hoehe [m]",    row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Leistung [kW]", row=2, col=1)

    return fig


def make_figure(agg: pd.DataFrame) -> go.Figure:
    """Erstellt interaktive Plotly-Figur mit drei Subplots."""
    has_ref = agg["vecto_kwh_per_km"].notna().any()
    rows = 3 if has_ref else 2

    fig = make_subplots(
        rows=rows, cols=1,
        subplot_titles=[
            "Energieaufschlüsselung pro Fahrzeug [kWh]",
            "Verbrauch: MATSim vs. VECTO [kWh/km]",
            *(["Fehler vs. Gesamtmasse"] if has_ref else []),
        ],
        vertical_spacing=0.09,
        row_heights=([0.44, 0.22, 0.34] if rows == 3 else [0.60, 0.40]),
    )

    vehicles = list(agg.index)

    # --- Subplot 1: Stacked bar ---
    components = [
        ("Rollwiderstand",  "E_roll_kWh",      "#4C72B0"),
        ("Luftwiderstand",  "E_aero_kWh",      "#DD8452"),
        ("Steigung ↑",      "E_grade_up_kWh",   "#55A868"),
        ("Beschleunigung",  "E_acc_up_kWh",    "#C44E52"),
        ("Verzögerung",     "E_acc_down_kWh",  "#9AC0D3"),
        ("Gefälle ↓",       "E_grade_down_kWh", "#91C98A"),
        ("Rekuperation",    "E_recup_kWh",     "#8172B2"),
    ]
    for label, col, color in components:
        fig.add_trace(go.Bar(
            name=label, x=vehicles, y=agg[col],
            marker_color=color, legendgroup=label,
        ), row=1, col=1)

    fig.add_trace(go.Scatter(
        name="E_bat (netto)", x=vehicles, y=agg["E_bat_kWh"],
        mode="markers+lines",
        marker=dict(symbol="diamond", size=9, color="black"),
        line=dict(color="black", dash="dot", width=1.5),
        legendgroup="E_bat",
    ), row=1, col=1)

    # --- Subplot 2: Verbrauch pro km ---
    fig.add_trace(go.Bar(
        name="MATSim", x=vehicles, y=agg["E_bat_per_km"],
        marker_color="#1f77b4", legendgroup="matsim_bar",
        showlegend=True,
    ), row=2, col=1)

    if has_ref:
        fig.add_trace(go.Bar(
            name="VECTO", x=vehicles, y=agg["vecto_kwh_per_km"],
            marker_color="#ff7f0e", legendgroup="vecto_bar",
            showlegend=True,
        ), row=2, col=1)

        # --- Subplot 3: Fehler vs. Gesamtmasse ---
        by_mission: dict[str, dict] = {}
        for vid, row in agg.iterrows():
            m = row["mission"] if pd.notna(row["mission"]) else "unbekannt"
            if m not in by_mission:
                by_mission[m] = {"x": [], "y": [], "text": []}
            by_mission[m]["x"].append(row["total_t"])
            by_mission[m]["y"].append(row["diff_pct"])
            by_mission[m]["text"].append(str(vid))

        for mission, data in by_mission.items():
            fig.add_trace(go.Scatter(
                name=mission,
                x=data["x"], y=data["y"],
                mode="markers+text",
                text=data["text"], textposition="top center",
                textfont=dict(size=10),
                marker=dict(size=11, color=_MISSION_COLOR.get(mission, "#7f7f7f")),
                legendgroup=f"scatter_{mission}",
            ), row=3, col=1)

        fig.add_hline(y=0, line_dash="dash", line_color="gray", row=3, col=1)
        fig.update_xaxes(title_text="Gesamtmasse [t]",             row=3, col=1)
        fig.update_yaxes(title_text="Diff MATSim−VECTO [%]",       row=3, col=1)

    fig.update_layout(
        barmode="relative",
        title="Widerstandsanalyse: Energiebilanz MATSim",
        height=1150 if rows == 3 else 750,
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="v", x=1.02, y=1.0),
    )
    fig.update_yaxes(title_text="Energie [kWh]", row=1, col=1)
    fig.update_yaxes(title_text="kWh/km",         row=2, col=1)

    return fig


# ---------------------------------------------------------------------------
# Dateiauswahl-Dialog
# ---------------------------------------------------------------------------

def find_candidate_csvs() -> list[Path]:
    """Sucht nach resistance_debug*.csv-Dateien im Projektverzeichnis (ohne data/ und .venv/)."""
    candidates: list[Path] = []
    skip = {".venv", "data", "__pycache__"}

    for p in sorted(_CALIB_ROOT.rglob("resistance_debug*.csv")):
        if any(part in skip for part in p.parts):
            continue
        candidates.append(p)

    return candidates


def pick_files_powershell(candidates: list[Path]) -> list[Path]:
    """Dateiauswahl ueber PowerShell Out-GridView (Windows-nativ, kein Extra-Paket)."""
    ps_objects = []
    for p in candidates:
        try:
            rel = str(p.relative_to(_CALIB_ROOT))
        except ValueError:
            rel = str(p)
        kb = p.stat().st_size / 1024
        ps_objects.append(f'[PSCustomObject]@{{Datei="{rel}"; KB=[int]{kb:.0f}}}')

    ps_array = ",\n  ".join(ps_objects)
    ps_script = (
        f'@(\n  {ps_array}\n) | '
        f'Out-GridView -PassThru -Title "resistance_debug CSV auswaehlen  '
        f'(Ctrl+Klick = Mehrfachauswahl)" | '
        f'Select-Object -ExpandProperty Datei'
    )

    tmp = Path(tempfile.mktemp(suffix=".ps1"))
    try:
        tmp.write_text(ps_script, encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-File", str(tmp)],
            capture_output=True, text=True, encoding="utf-8",
        )
    finally:
        tmp.unlink(missing_ok=True)

    if result.returncode != 0 or not result.stdout.strip():
        return []

    selected_names = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    return [_CALIB_ROOT / name for name in selected_names]


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
    candidates = find_candidate_csvs()

    if not candidates:
        print(f"Keine CSV-Dateien gefunden unter: {_CALIB_ROOT}", file=sys.stderr)
        sys.exit(1)

    csv_paths = pick_files_powershell(candidates)

    if not csv_paths:
        print("Abgebrochen.")
        sys.exit(0)

    frames: list[pd.DataFrame] = []
    for csv_path in csv_paths:
        print(f"Lade {csv_path} ...")
        part = load_debug_csv(csv_path)
        print(f"  {len(part):,} Link-Eintraege, {part['vehicleId'].nunique()} Fahrzeuge")
        frames.append(part)

    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if len(frames) > 1:
        print(f"  Gesamt: {len(df):,} Eintraege, {df['vehicleId'].nunique()} Fahrzeuge")

    agg = aggregate_per_vehicle(df)
    agg = enrich_with_meta_and_ref(agg)

    print_table(agg)

    speed_fig = make_speed_figure(df)
    OUTPUT_SPEED.parent.mkdir(parents=True, exist_ok=True)
    speed_fig.write_html(str(OUTPUT_SPEED), include_plotlyjs="cdn")
    print(f"Gespeichert: {OUTPUT_SPEED}")

    # fig = make_figure(agg)
    # OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # fig.write_html(str(OUTPUT), include_plotlyjs="cdn")
    # print(f"Gespeichert: {OUTPUT}")


if __name__ == "__main__":
    main()