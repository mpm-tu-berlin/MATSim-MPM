"""
Erzeugt einen eigenstaendigen HTML-Report mit den Auswertungs-Erkenntnissen zum
Energiemodell (Geschwindigkeitsprofil, Widerstandsanteile, Hoehenprofil,
Rekuperation und Verluste).

Datenquelle: bester Trial der gemeinsamen 'all'-Studie eines 1m-Laufs
(ein Parametersatz fuer beide Missionen).

Aufruf:
    .venv/Scripts/python analysis/report_summary.py
    .venv/Scripts/python analysis/report_summary.py <run_dir> <trial_nr>

Ausgabe:
    results/auswertung_energiemodell.html
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import optuna

from src.error_computation import load_reference
from src.config import ACTIVE_VEHICLE_GROUP, STUDIES
from analysis.convergence_errors import convergence_fig

_CALIB_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = _CALIB_ROOT / "results" / "auswertung_energiemodell.html"
VECTO_LH = _CALIB_ROOT / "data" / "LongHaul.vdri"
VECTO_RD = _CALIB_ROOT / "data" / "RegionalDelivery.vdri"

# Fahrzeug-Hoechstleistung (alle BET_G5) [W]
P_MOTOR_W = 407_000.0


def find_default_run() -> tuple[Path, int]:
    """Neuesten 1m-Run + Trial-Nr der besten 'all'-Studie automatisch finden."""
    runs = sorted((_CALIB_ROOT / "results" / "runs").glob("*_1m"),
                  key=lambda p: p.stat().st_mtime)
    if not runs:
        raise SystemExit("Kein 1m-Run unter results/runs gefunden.")
    run = runs[-1]
    allmr = run / "all" / "matsim_runs"
    trial_dirs = list(allmr.glob("trial_*_LongHaul"))
    if not trial_dirs:
        raise SystemExit(f"Kein all-Trial in {allmr} gefunden.")
    trial_nr = int(trial_dirs[0].name.split("_")[1])
    return run, trial_nr


def load_params(run: Path, trial: int) -> dict:
    p = run / "all" / "matsim_runs" / f"trial_{trial}_params.properties"
    out = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = float(v.strip())
    return out


def energy_breakdown(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Pro Fahrzeug: Widerstandsenergien + Traktions-/Rekuperations-Bilanz [kWh]."""
    eta_t = params["tractionEfficiency"]
    eta_r = params["recupEfficiency"]
    p_recup = params["maxRecupPowerFraction"] * P_MOTOR_W
    j2k = 1.0 / 3.6e6

    rows = []
    for vid, g in df.groupby("vehicleId"):
        t = g["tPhysical_s"].values
        pres = (g["pRoll_W"] + g["pAero_W"] + g["pGrav_W"]).values
        dKE = (g["pKin_W"] * g["tPhysical_s"]).values

        e_roll = (g["pRoll_W"] * t).sum() * j2k
        e_aero = (g["pAero_W"] * t).sum() * j2k
        e_grav = g["pGrav_W"] * t
        e_gup = e_grav.clip(lower=0).sum() * j2k
        e_gdn = e_grav.clip(upper=0).sum() * j2k
        e_kin = g["pKin_W"] * t
        e_aup = e_kin.clip(lower=0).sum() * j2k
        e_adn = e_kin.clip(upper=0).sum() * j2k

        # Traktion (positiv) -> Batterie liefert
        mech_tr = (np.where(pres > 0, pres * t, 0).sum()
                   + np.where(dKE > 0, dKE, 0).sum())
        batt_tr = (np.where(pres > 0, np.minimum(pres / eta_t, P_MOTOR_W) * t, 0).sum()
                   + np.where(dKE > 0, dKE / eta_t, 0).sum())
        loss_tr = batt_tr - mech_tr

        # Bremsen (negativ) -> Rekuperation, Cap nur auf Resist-Pfad
        pbatt_res = np.where(pres < 0, np.maximum(pres * eta_r, -p_recup), 0)
        batt_rec_res = (-pbatt_res * t).sum()
        mech_in_res = (-pbatt_res * t / eta_r).sum()
        fric = np.where(pres < 0, -pres * t, 0).sum() - mech_in_res
        batt_rec_kin = np.where(dKE < 0, -dKE * eta_r, 0).sum()
        mech_in_kin = np.where(dKE < 0, -dKE, 0).sum()
        batt_rec = batt_rec_res + batt_rec_kin
        recup_loss = (mech_in_res + mech_in_kin) - batt_rec
        mech_br = (np.where(pres < 0, -pres * t, 0).sum()
                   + np.where(dKE < 0, -dKE, 0).sum())

        dist = g["length_m"].sum() / 1000.0
        net = g["energy_Wh"].sum() / 1000.0
        rows.append(dict(
            vehicleId=vid, dist_km=dist,
            E_roll=e_roll, E_aero=e_aero, E_gup=e_gup, E_gdn=e_gdn,
            E_aup=e_aup, E_adn=e_adn,
            mech_tr=mech_tr * j2k, batt_tr=batt_tr * j2k, loss_tr=loss_tr * j2k,
            mech_br=mech_br * j2k, batt_rec=batt_rec * j2k,
            recup_loss=recup_loss * j2k, fric=fric * j2k,
            net=net, ekm=net / dist,
        ))
    return pd.DataFrame(rows).set_index("vehicleId").sort_index()


def consistency_table(run: Path) -> pd.DataFrame:
    """Best-Parameter + RMSE aller fuenf Studien aus den Optuna-DBs."""
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    order = ["tractionEfficiency", "inertiaC", "recupEfficiency",
             "maxRecupPowerFraction", "cdXA", "rollingC"]
    labels = ["RMSE [%]", "η_traction", "inertiaC", "η_recup",
              "maxRecupFrac", "cdXA", "rollingC"]

    def fmt(k, v):
        return f"{v:.5f}" if k == "rollingC" else f"{v:.3f}"

    rows = {}
    for s in STUDIES:
        name = s["name"]
        db = run / name / "optuna_study.db"
        if not db.exists():
            continue
        st = optuna.load_study(
            study_name=f"matsim-vecto-{ACTIVE_VEHICLE_GROUP}-{name}",
            storage=f"sqlite:///{db}")
        bt = st.best_trial
        rows[name] = [f"{bt.value:.2f}"] + [fmt(k, bt.params[k]) for k in order]

    df = pd.DataFrame(rows, index=labels).T
    df.index.name = "Studie"
    return df


def elevation_stats(df: pd.DataFrame) -> dict:
    g = df[df["vehicleId"] == df["vehicleId"].iloc[0]].copy()
    dz = g["grade_pct"] / 100.0 * g["length_m"]
    return dict(asc=dz.clip(lower=0).sum(), desc=dz.clip(upper=0).sum(),
                net=dz.sum(), maxgrade=g["grade_pct"].abs().max())


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def fig_speed(df: pd.DataFrame, mission: str, vecto_path: Path, tag: str) -> go.Figure:
    """Geschwindigkeitsprofil low+high (rrc48 stellv.) gegen die Missions-VECTO-Referenz."""
    fig = go.Figure()
    v = pd.read_csv(vecto_path)
    v.columns = ["s_m", "v_kmh", "grad", "stop", "hw"]
    fig.add_trace(go.Scatter(x=v["s_m"] / 1000, y=v["v_kmh"], mode="lines",
                             name="VECTO Soll", line=dict(color="black", width=1.4, dash="dash")))
    for vid, label, color in [(f"truck_g5_rrc48_{tag}_low", "MATSim low", "#1f77b4"),
                              (f"truck_g5_rrc48_{tag}_high", "MATSim high", "#d62728")]:
        g = df[df["vehicleId"] == vid].copy()
        if g.empty:
            continue
        g["cum"] = g["length_m"].cumsum() / 1000
        fig.add_trace(go.Scatter(x=g["cum"], y=g["vExit_kmh"], mode="lines",
                                 name=label, line=dict(color=color, width=1)))
    fig.update_layout(title=f"Geschwindigkeitsprofil MATSim vs. VECTO ({mission})",
                      xaxis_title="Streckenposition [km]", yaxis_title="v [km/h]",
                      template="plotly_white", hovermode="x unified", height=400)
    return fig


def speed_metrics(df: pd.DataFrame, vid: str, vecto: pd.DataFrame) -> dict:
    g = df[df["vehicleId"] == vid].copy()
    g["cum"] = g["length_m"].cumsum() / 1000
    w = g["length_m"].values
    vint = np.interp(g["cum"] * 1000, vecto["s_m"], vecto["v_kmh"])
    rmse = float(np.sqrt(np.sum(w * (g["vExit_kmh"].values - vint) ** 2) / np.sum(w)))
    return dict(vmax=float(g["vExit_kmh"].max()),
                vmean=float(np.average(g["vExit_kmh"], weights=w)), rmse=rmse)


def fig_resistance(bd: pd.DataFrame) -> go.Figure:
    comps = [("Rollwiderstand", "E_roll", "#4C72B0"),
             ("Luftwiderstand", "E_aero", "#DD8452"),
             ("Steigung ↑", "E_gup", "#55A868"),
             ("Beschleunigung", "E_aup", "#C44E52"),
             ("Verzögerung", "E_adn", "#9AC0D3"),
             ("Gefälle ↓", "E_gdn", "#91C98A")]
    fig = go.Figure()
    x = list(bd.index)
    for label, col, color in comps:
        fig.add_trace(go.Bar(name=label, x=x, y=bd[col], marker_color=color))
    fig.add_trace(go.Scatter(name="Netto (Batterie)", x=x, y=bd["net"],
                             mode="markers", marker=dict(symbol="diamond", size=9, color="black")))
    fig.update_layout(barmode="relative", title="Energiebilanz je Fahrzeug [kWh / ~100 km]",
                      yaxis_title="Energie [kWh]", template="plotly_white", height=480,
                      hovermode="x unified")
    return fig


def fig_losses(bd: pd.DataFrame) -> go.Figure:
    x = list(bd.index)
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Antriebsverlust", x=x, y=bd["loss_tr"], marker_color="#C44E52"))
    fig.add_trace(go.Bar(name="Rekup-Verlust", x=x, y=bd["recup_loss"], marker_color="#DD8452"))
    fig.add_trace(go.Bar(name="Reibbremse", x=x, y=bd["fric"], marker_color="#8C8C8C"))
    fig.add_trace(go.Bar(name="Rekuperiert → Batterie", x=x, y=bd["batt_rec"], marker_color="#55A868"))
    fig.update_layout(barmode="group", title="Verluste & Rekuperation je Fahrzeug [kWh]",
                      yaxis_title="Energie [kWh]", template="plotly_white", height=440,
                      hovermode="x unified")
    return fig


# ---------------------------------------------------------------------------
# HTML zusammensetzen
# ---------------------------------------------------------------------------

def df_to_html(df: pd.DataFrame, fmt: str = "{:.2f}") -> str:
    return df.to_html(float_format=lambda x: fmt.format(x), border=0,
                      classes="tbl", justify="center")


def main():
    if len(sys.argv) >= 3:
        run, trial = Path(sys.argv[1]), int(sys.argv[2])
    else:
        run, trial = find_default_run()

    params = load_params(run, trial)
    lh_csv = run / "all" / "matsim_runs" / f"trial_{trial}_LongHaul" / "resistance_debug.csv"
    rd_csv = run / "all" / "matsim_runs" / f"trial_{trial}_RegionalDelivery" / "resistance_debug.csv"

    df_lh = pd.read_csv(lh_csv, on_bad_lines="warn")
    df_rd = pd.read_csv(rd_csv, on_bad_lines="warn")

    bd_lh = energy_breakdown(df_lh, params)
    bd_rd = energy_breakdown(df_rd, params)
    bd_all = pd.concat([bd_lh, bd_rd])

    ref = {}
    for m in ("LongHaul", "RegionalDelivery"):
        ref.update(load_reference(m, "BET_G5"))
    bd_all["VECTO"] = [ref.get(v, {}).get("ee_kwh_per_km", float("nan")) for v in bd_all.index]
    bd_all["Diff_%"] = (bd_all["ekm"] - bd_all["VECTO"]) / bd_all["VECTO"] * 100

    elev_lh = elevation_stats(df_lh)
    elev_rd = elevation_stats(df_rd)
    cons_tbl = consistency_table(run)

    # --- Diskretisierungs-Konvergenz (neuester Sweep, falls vorhanden) ---
    disc_tbl_html = ""
    conv_fig_html = ""
    conv_dir = _CALIB_ROOT / "results" / "convergence"
    conv_csvs = (sorted(conv_dir.glob("*/error_breakdown.csv"), key=lambda p: p.stat().st_mtime)
                 if conv_dir.exists() else [])
    if conv_csvs:
        bd_conv = pd.read_csv(conv_csvs[-1])
        if "rrc" in bd_conv.columns:
            avail = set(bd_conv["resolution_m"])
            keyres = [r for r in [100, 250, 400, 500, 750] if r in avail]
            sub = bd_conv[bd_conv["resolution_m"].isin(keyres)].copy()
            sub["Fahrzeug"] = sub["scenario"] + " " + sub["rrc"]
            piv = sub.pivot_table(index="Fahrzeug", columns="resolution_m", values="diff_pct")
            piv = piv[keyres]
            piv.columns = [f"{c} m" for c in piv.columns]
            disc_tbl_html = df_to_html(piv, "{:+.2f}")
            conv_fig_html = (
                "<h3>Konvergenz &ndash; logarithmische x-Achse</h3>"
                + convergence_fig(bd_conv, "log").to_html(full_html=False, include_plotlyjs=False)
                + "<h3>Konvergenz &ndash; lineare x-Achse</h3>"
                + convergence_fig(bd_conv, "linear").to_html(full_html=False, include_plotlyjs=False))

    # --- Speed-Kennwerte fuer alle vier Profile (rrc48 stellv.) ---
    v_lh = pd.read_csv(VECTO_LH); v_lh.columns = ["s_m", "v_kmh", "grad", "stop", "hw"]
    v_rd = pd.read_csv(VECTO_RD); v_rd.columns = ["s_m", "v_kmh", "grad", "stop", "hw"]
    metrics = {
        "LH low":  speed_metrics(df_lh, "truck_g5_rrc48_lh_low",  v_lh),
        "LH high": speed_metrics(df_lh, "truck_g5_rrc48_lh_high", v_lh),
        "RD low":  speed_metrics(df_rd, "truck_g5_rrc48_rd_low",  v_rd),
        "RD high": speed_metrics(df_rd, "truck_g5_rrc48_rd_high", v_rd),
    }
    spd_tbl = pd.DataFrame(metrics).T
    spd_tbl.columns = ["Vmax [km/h]", "v_mittel [km/h]", "Speed-RMSE vs VECTO [km/h]"]
    spd_tbl = spd_tbl.rename_axis("Profil")
    vmax = metrics["LH low"]["vmax"]
    vmean = metrics["LH low"]["vmean"]
    rmse_v = metrics["LH low"]["rmse"]

    # --- Tabellen ---
    par_tbl = pd.DataFrame({"Wert": params}).rename_axis("Parameter")

    res_tbl = bd_all[["E_roll", "E_aero", "E_gup", "E_gdn", "E_aup", "E_adn", "ekm", "VECTO", "Diff_%"]].copy()
    res_tbl["Roll%"] = bd_all["E_roll"] / (bd_all["E_roll"] + bd_all["E_aero"]) * 100
    res_tbl["Aero%"] = 100 - res_tbl["Roll%"]
    res_tbl.columns = ["Roll", "Aero", "Steig↑", "Gefälle↓", "Beschl↑",
                       "Verz↓", "kWh/km", "VECTO", "Diff%", "Roll%", "Aero%"]

    loss_tbl = bd_all[["mech_tr", "batt_tr", "loss_tr", "mech_br", "batt_rec",
                       "recup_loss", "fric", "net"]].copy()
    loss_tbl.columns = ["Mech-Trakt", "Batt-Trakt", "Antriebsverl.", "Mech-Brems",
                        "Rekup→Batt", "Rekup-Verl.", "Reibbremse", "Netto"]

    elev_tbl = pd.DataFrame({
        "Σ aufwärts [m]": [elev_lh["asc"], elev_rd["asc"]],
        "Σ abwärts [m]": [elev_lh["desc"], elev_rd["desc"]],
        "Netto [m]": [elev_lh["net"], elev_rd["net"]],
        "max |Steig.| [%]": [elev_lh["maxgrade"], elev_rd["maxgrade"]],
    }, index=["LongHaul", "RegionalDelivery"]).rename_axis("Mission")

    # --- Figuren ---
    figs = [fig_speed(df_lh, "Long Haul", VECTO_LH, "lh"),
            fig_speed(df_rd, "Regional Delivery", VECTO_RD, "rd"),
            fig_resistance(bd_all), fig_losses(bd_all)]
    fig_html = []
    for i, f in enumerate(figs):
        fig_html.append(f.to_html(full_html=False,
                                  include_plotlyjs=("cdn" if i == 0 else False)))

    css = """
    body{font-family:Segoe UI,Arial,sans-serif;margin:30px auto;max-width:1100px;color:#222;line-height:1.5}
    h1{border-bottom:3px solid #1f77b4;padding-bottom:6px}
    h2{color:#1f77b4;margin-top:36px;border-bottom:1px solid #ddd;padding-bottom:4px}
    table.tbl{border-collapse:collapse;margin:14px 0;font-size:13px}
    table.tbl th,table.tbl td{border:1px solid #ccc;padding:5px 9px;text-align:right}
    table.tbl th{background:#1f77b4;color:#fff}
    table.tbl tr:nth-child(even){background:#f4f7fb}
    .note{background:#fff8e1;border-left:4px solid #f0b429;padding:10px 14px;margin:14px 0}
    .key{background:#eef6ee;border-left:4px solid #55A868;padding:10px 14px;margin:14px 0}
    """

    html = f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<title>Auswertung Energiemodell BET_G5</title><style>{css}</style></head><body>
<h1>Auswertung dynamisches Energiemodell &ndash; BET_G5</h1>
<p><b>Lauf:</b> {run.name} &nbsp;|&nbsp; <b>gemeinsame Kalibrierung (all), Trial #{trial}</b>
&nbsp;|&nbsp; 1&nbsp;m-Aufl&ouml;sung &nbsp;|&nbsp; Strecke je ~100&nbsp;km.</p>

<h2>1. Kalibrierte Parameter</h2>
{df_to_html(par_tbl, "{:.5f}")}
<div class="key">Optuna zieht <b>cdXA an die obere A15-Klassengrenze</b> (mehr Luftwiderstand n&ouml;tig),
w&auml;hrend <b>rollingC praktisch am Standard</b> (Mittel 0,005025) bleibt. auxPowerW ist fix bei 4000&nbsp;W.</div>

<h2>2. Geschwindigkeitsprofile (Wirkung der Ma&szlig;nahmen)</h2>
{fig_html[0]}
{fig_html[1]}
<p>Kennwerte aller vier Profile (rrc48 stellvertretend, rrc53 nahezu identisch):</p>
{df_to_html(spd_tbl, "{:.2f}")}
<table class="tbl"><tr><th>Metrik (LH low)</th><th>vorher (83-Cap, alte Beschl.)</th><th>nachher</th></tr>
<tr><td style="text-align:left">Vmax</td><td>82,6 (Cap 83)</td><td>{vmax:.1f} km/h</td></tr>
<tr><td style="text-align:left">Mittelgeschwindigkeit</td><td>82,6</td><td>{vmean:.1f} km/h</td></tr>
<tr><td style="text-align:left">Speed-RMSE vs VECTO</td><td>2,29</td><td>{rmse_v:.2f} km/h</td></tr>
<tr><td style="text-align:left">Anlauf 1. Link</td><td>~1 &rarr; 23 km/h Sprung</td><td>0,4 &rarr; ~14 km/h, dann graduell</td></tr></table>
<div class="key">85-km/h-Cap behoben (Fahrzeug-maximumVelocity 83&rarr;85). Anlauf aus dem Stillstand
durch korrekte Konstante-Leistungs-Integration (cbrt) jetzt physikalisch &uuml;ber mehrere Links verteilt.
RD liegt durch h&auml;ufige Stopps deutlich unter dem LH-Marschniveau.</div>

<h2>3. Widerstandsanteile</h2>
{fig_html[2]}
{df_to_html(res_tbl, "{:.2f}")}
<div class="key"><b>Luftwiderstand dominiert</b> &ndash; bei geringer Beladung bis ~68&nbsp;% (Aero),
bei voller Beladung n&auml;hert sich Roll:Aero 50:50, da der Rollwiderstand mit der Masse w&auml;chst.</div>

<h2>4. H&ouml;henprofil (kumuliert)</h2>
{df_to_html(elev_tbl, "{:.1f}")}
<div class="note">Steigungs- und Gef&auml;lleenergie (&plusmn;~50&nbsp;kWh) heben sich nahezu auf,
weil die Route <b>h&ouml;henneutral</b> beginnt/endet (Netto ~&plusmn;wenige Meter) &ndash; trotz ~520&ndash;610&nbsp;m
kumulierter Hubarbeit. Die Werte sind &uuml;ber alle Links summiert, nicht Start/Ende.</div>

<h2>5. Rekuperation &amp; Verluste</h2>
{fig_html[3]}
{df_to_html(loss_tbl, "{:.1f}")}
<div class="note">Die ~50&nbsp;kWh Gravitations-Energie sind <b>nicht</b> der Rekup-Pool: bergab wird der Gro&szlig;teil
schon von Roll+Aero aufgezehrt. Echtes Bremsen (Mech-Brems) ist deutlich kleiner; davon werden ~84&nbsp;%
(= &eta;_recup) zur&uuml;ckgewonnen, die <b>Reibbremse ist nahezu null</b> (Rekup-Leistungsgrenze bindet kaum).</div>
<div class="key">Gr&ouml;&szlig;ter Verlust ist der <b>Antriebsstrang</b> (Batterie&rarr;Rad, konstant 1&minus;&eta;_traction
&asymp; 14&nbsp;% der Traktionsenergie). Rekup-Verluste sind klein bei LH, h&ouml;her bei RD (mehr Stop-and-Go).</div>

<h2>6. Parameter-Konsistenz (Einzelstudien vs. joint)</h2>
{df_to_html(cons_tbl)}
<div class="key">Die <b>Einzelstudien treffen mit 0,55&ndash;0,80&nbsp;% nahezu perfekt</b>, die joint-Studie nur 1,42&nbsp;%
&ndash; ein globaler Parametersatz kann die unterschiedlich beladenen Szenarien nicht gleichzeitig erf&uuml;llen.</div>
<div class="note"><b>&eta;_traction</b> ist dominant, streut aber 0,84&ndash;0,94 <i>ohne</i> konsistente Beladungsrichtung
(LH steigt low&rarr;high, RD f&auml;llt) &ndash; er saugt also den <b>Rest-Modellfehler</b> auf, nicht nur die echte
Antriebseffizienz. <b>&eta;_recup / maxRecupFrac</b> haben die gr&ouml;&szlig;te Streuung bei kleinster Wirkung &rarr;
praktisch <b>unidentifiziert</b> (mit Vorsicht zu interpretieren). <b>cdXA</b> ist der einzige robust bestimmte
Widerstand (konsistent nahe der oberen A15-Grenze).</div>

<h2>7. Diskretisierungs-Konvergenz &amp; Limitation</h2>
<p>Abweichung gegen VECTO [%] an Schl&uuml;ssel-Auflösungen (Einzelszenarien, je eigener
Parametersatz, joint ausgeschlossen). Voller Verlauf 1&ndash;750&nbsp;m im Sweep-Plot
(results/convergence/&hellip;/convergence.html).</p>
{disc_tbl_html}
<div class="note">Die beiden unteren Panels zerlegen den Fehler analytisch, jeweils in <b>% des
Gesamtverbrauchs</b>: Der <b>Aero-v³-Fehler</b> entsteht durch die Jensen-Ungleichung
(<i>(mittl.&nbsp;v)³ &lt; mittl.&nbsp;v³</i>) beim Kollabieren des Profils auf den Link-Freispeed. Der
<b>Grade-Fehler</b> ist die Netto-Verbrauchs&auml;nderung durch das <b>gegl&auml;ttete H&ouml;henprofil</b>:
die Steigungsenergie <i>m&middot;g&middot;&Delta;z</i> selbst ist diskretisierungsinvariant, aber bergauf
(&divide;&eta;_traction) und bergab (&times;&eta;_recup, gecappt, Rest gebremst) sind asymmetrisch &ndash;
gegl&auml;ttetes Gel&auml;nde verliert die welligen Anstiege/Gef&auml;lle und <b>untersch&auml;tzt</b> daher
den Verbrauch.</div>
{conv_fig_html}
<div class="note"><b>Limitation &ndash; reale Netze sind unterhalb ~300&ndash;400&nbsp;m rauschdominiert.</b>
Die 1-m-VECTO-Referenz ist idealisiert; auf realen Netzen ist feiner als ~400&nbsp;m keine echte
Mehrinformation, sondern &uuml;bertr&auml;gt nur H&ouml;hen-/Geometrierauschen ins Modell. 400&nbsp;m ist
damit die feinste <i>sinnvoll</i> aufl&ouml;sbare Skala.</div>
<div class="key"><b>Long Haul</b> bei 400&nbsp;m: ~1,6&ndash;3&nbsp;% Diskretisierungsfehler (&Delta; vs 1&nbsp;m)
&rarr; praktisch verlustfrei. <b>Regional Delivery</b> bei 400&nbsp;m: ~6&nbsp;%. RD br&auml;uchte f&uuml;r
&lt;1&nbsp;% eigentlich &le;25&ndash;50&nbsp;m &ndash; mit realen (rauschbehafteten) Netzen nicht erreichbar,
daher sind die ~6&nbsp;% eine <b>prinzipielle Untergrenze</b> der Methode (hohe v-Varianz im
Stop-and-Go), keine behebbare Schw&auml;che.</div>
<div class="note">rrc53 liegt durchg&auml;ngig ~1,1&nbsp;pp unter rrc48 &ndash; Kalibrierungs-Offset des
globalen rollingC (gleiche MATSim-Verbr&auml;uche, h&ouml;here VECTO-Referenz), <b>nicht</b>
Diskretisierung. Der diskretisierungsbedingte Anteil (&Delta; vs 1&nbsp;m) ist f&uuml;r beide RRC nahezu gleich.</div>

<p style="margin-top:40px;color:#888;font-size:12px">Erzeugt mit analysis/report_summary.py</p>
</body></html>"""

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Gespeichert: {OUTPUT}")


if __name__ == "__main__":
    main()
