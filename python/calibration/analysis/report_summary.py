"""
Erzeugt einen eigenstaendigen HTML-Report mit den Auswertungs-Erkenntnissen zum
Energiemodell (Geschwindigkeitsprofil, Widerstandsanteile, Rekuperation und
Verluste, Parameter-Konsistenz, Diskretisierungs-Konvergenz).

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
from src.config import ACTIVE_VEHICLE_GROUP, STUDIES, PARAM_BOUNDS
from analysis.convergence_errors import convergence_fig

_CALIB_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = _CALIB_ROOT / "results" / "auswertung_energiemodell.html"
MD_OUTPUT = _CALIB_ROOT / "results" / "paper_findings.md"
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
    """Pro Fahrzeug: Widerstandsenergien + Traktions-/Rekuperations-Bilanz [kWh].

    Traktions-/Rekuperations-Zerlegung mit gekoppelter Effizienz je Link (ein eta
    ueber das Vorzeichen der Netto-Mechanikenergie), modelltreu zur neuen
    MpmDynamicBetDriveEnergyConsumption. Die pRoll_W/pAero_W/pGrav_W-Spalten der
    Debug-CSV enthalten bereits die cos(theta)-Rollwiderstandskorrektur der JAR.
    """
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

        # --- Gekoppelte Effizienz je Link (wie MpmDynamicBetDriveEnergyConsumption) ---
        # Ein eta pro Link ueber das Vorzeichen der Netto-Mechanikenergie
        # eMech = pResist*t + dKE: >=0 -> Traktion (1/eta_t), <0 -> Rekuperation (eta_r).
        # Dasselbe eta gilt fuer Widerstands- UND Kinetik-Anteil und verhindert die
        # Doppelbesteuerung des alten, anteilsweise entkoppelten Ansatzes.
        e_res = pres * t                                  # Widerstandsenergie je Link [J]
        eMech = e_res + dKE
        traction = eMech >= 0.0
        eta = np.where(traction, 1.0 / eta_t, eta_r)

        # Resist-Pfad mit Leistungs-Cap [-p_recup, P_MOTOR_W]; Kinetik-Pfad ohne Cap.
        e_batt_res = np.clip(pres * eta, -p_recup, P_MOTOR_W) * t
        e_batt_kin = dKE * eta
        e_batt = e_batt_res + e_batt_kin                  # Batterieenergie je Link [J]

        # Traktion (eMech>=0) -> Batterie liefert
        mech_tr = eMech[traction].sum()                  # mechanisch gefordert [J]
        batt_tr = e_batt[traction].sum()                 # Batterie geliefert (>0) [J]
        loss_tr = batt_tr - mech_tr

        # Bremsen (eMech<0) -> Rekuperation
        brake = ~traction
        mech_br = (-eMech[brake]).sum()                  # mechanisch verfuegbar (>0) [J]
        batt_rec = (-e_batt[brake]).sum()                # Batterie zurueckgewonnen (>0) [J]
        # Friktion = durch den Resist-Cap nicht aufgenommene Bremsmechanik (nur Resist-Pfad).
        recov = brake & (pres < 0.0)
        mech_res_recov = np.where(recov, -e_res, 0.0).sum()       # rueckgewinnbar [J]
        batt_res_recov = np.where(recov, -e_batt_res, 0.0).sum()  # tatsaechlich [J]
        fric = mech_res_recov - batt_res_recov / eta_r
        recup_loss = (mech_br - batt_rec) - fric

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
    """Best-Parameter + RMSE aller fuenf Studien aus den Optuna-DBs.

    Je Parameterzelle zusaetzlich die Optuna-Parameterwichtigkeit fuer diesen Run
    in Klammern [%] (fANOVA, summiert pro Studie = 100 %) – zeigt, welcher Parameter
    das RMSE-Ziel der jeweiligen Studie getrieben hat.
    """
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    order = ["tractionEfficiency", "inertiaC", "recupEfficiency",
             "maxRecupPowerFraction", "cdXA", "rollingC"]
    labels = ["RMSE [%]", "η_traction", "inertiaC", "η_recup",
              "maxRecupFrac", "cdXA", "rollingC"]

    def fmt(k, v, imp):
        base = f"{v:.5f}" if k == "rollingC" else f"{v:.3f}"
        return f"{base} ({imp * 100:.0f}%)"

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
        try:
            imp = optuna.importance.get_param_importances(st)
        except Exception:
            imp = {}
        rows[name] = [f"{bt.value:.2f}"] + [fmt(k, bt.params[k], imp.get(k, 0.0)) for k in order]

    df = pd.DataFrame(rows, index=labels).T
    df.index.name = "Studie"
    return df


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


def df_to_md(df: pd.DataFrame, fmt: str = "{:.2f}") -> str:
    """GitHub-Markdown-Tabelle inkl. Index (ohne tabulate-Abhaengigkeit)."""
    cols = [str(c) for c in df.columns]
    head = "| " + " | ".join([df.index.name or ""] + cols) + " |"
    sep = "| " + " | ".join(["---"] * (len(cols) + 1)) + " |"
    lines = [head, sep]
    for idx, row in df.iterrows():
        cells = []
        for c in df.columns:
            v = row[c]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append("" if pd.isna(v) else fmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join([str(idx)] + cells) + " |")
    return "\n".join(lines)


def _real_validation_section() -> str:
    """Optionaler Realfahrten-Validierungsabschnitt (Abschnitt 10).

    Robust/defensiv: Logik + private Daten liegen in scripts/analyze_real_validation.py
    (gitignored). Ohne diese Dateien (z.B. frischer Clone) wird der Abschnitt einfach
    ausgelassen, damit dieser getrackte Report lauffaehig bleibt.
    """
    try:
        scripts_dir = str(_CALIB_ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from analyze_real_validation import real_validation_markdown
        return real_validation_markdown()
    except Exception as e:
        print(f"[report_summary] Realfahrten-Validierung (Abschnitt 10) uebersprungen: {e}")
        return ""


def write_paper_markdown(run, trial, cons_tbl, res_tbl, loss_tbl, spd_tbl, bd_conv) -> None:
    """Schreibt eine kompakte, an Claude uebergebbare Markdown-Faktenbasis fuers Paper."""
    bounds_md = "\n".join(
        f"- `{k}`: [{lo}, {hi}]" for k, (lo, hi) in PARAM_BOUNDS.items())

    # Konvergenz-Tabellen je Szenario (rrc48), Schluessel-Aufloesungen
    conv_md = "_Kein Sweep gefunden._"
    if bd_conv is not None and "rrc" in bd_conv.columns:
        d = bd_conv[bd_conv["rrc"] == "rrc48"].copy()
        keyres = [r for r in [1, 25, 50, 100, 200, 400, 500, 750]
                  if r in set(d["resolution_m"])]
        blocks = []
        for scen in ["lh_low", "lh_high", "rd_low", "rd_high"]:
            s = d[d["scenario"] == scen].set_index("resolution_m")
            t = pd.DataFrame({
                "diff% vs VECTO": [s.loc[r, "diff_pct"] for r in keyres],
                "Aero-Fehler %":  [s.loc[r, "aero_err_pct"] for r in keyres],
                "Grade-Fehler %": [s.loc[r, "grade_err_pct"] for r in keyres],
            }, index=keyres).rename_axis("Linklänge [m]")
            blocks.append(f"**{scen} (rrc48):**\n\n" + df_to_md(t, "{:+.2f}"))
        conv_md = "\n\n".join(blocks)

    md = f"""# Energiemodell BET_G5 — Faktenbasis fuer das Paper

> Maschinen-/Claude-lesbare Zusammenfassung der Kalibrierungs- und Diskretisierungs-
> studie. Quelle: Kalibrierungslauf `{run.name}`, gemeinsamer Trial #{trial} (1 m).
> Generiert aus `analysis/report_summary.py` (Single Source of Truth, Zahlen live).

## 1. Kontext & Ziel

- **System:** MATSim-MPM — physikbasiertes Verbrauchsmodell fuer batterie-elektrische
  Schwerlast-LKW (BET), kalibriert gegen **VECTO**-Referenzzyklen.
- **Fahrzeuggruppe:** VECTO Group 5 (Sattelzug), `BET_G5`.
- **Missionen:** Long Haul (LH) und Regional Delivery (RD), je ~100 km Route.
- **Beladung:** je Mission `low` und `high`.
- **Reifenvarianten:** `rrc48` und `rrc53` (Rollwiderstandsklassen).
- **Koordinatensystem:** EPSG:4839; **kein Stau** simuliert.
- **Paper-Beitrag:** Herleitung einer **sinnvollen Netzauflösung** (Linklänge) fuer
  VECTO-aequivalente MATSim-Simulationen, getrennt nach Mission.

## 2. Energiemodell (pro Link)

Gesamtenergie je Link: `E = E_widerstand + ΔE_kinetisch`.

Leistungskomponenten bei mittlerer Linkgeschwindigkeit `v`:

- Rollwiderstand: `pRoll = ft · m · g · v`   (`ft` = Rollwiderstandsbeiwert RRC)
- Luftwiderstand: `pAero = fa · v³`,  mit `fa = 0.5 · ρ · cdXA`, `ρ = 1.225 kg/m³`
  (Jensen-korrekt ueber den Link via `vSqMean = (v0²+vExit²)/2`)
- Steigung: `pGrav = m · g · grade · v`   (positiv bergauf, negativ bergab; `|grade| ≤ 0.15`)

**Batterie-Verschaltung (entscheidend):** `pResist = pRoll + pAero + pGrav`.
- `pResist ≥ 0` → Traktion: `pBatt = pResist / η_traction`, gecappt bei `maxMotorPower` (407 kW).
- `pResist < 0` → Rekuperation: `pBatt = pResist · η_recup`, gecappt bei `maxRecupPower`
  (`= maxRecupPowerFraction · 407 kW`); nicht rekuperierbarer Rest → Reibbremse.

Kinetik (Gesamtaenderung, nicht zeitbasiert): `ΔKE = 0.5·m_inertia·(vExit²−v0²)`,
bei `ΔKE ≥ 0` ÷ `η_traction`, sonst × `η_recup`.

Beschleunigung physikalisch ueber die Strecke integriert (konstante Leistung):
`vExit³ = v0³ + 3·pKin·L / m_inertia` (cbrt) → kein unphysikalischer Sprung aus dem Stand.

**Wichtig fuer die Diskretisierung:** Die QSim-Zeitschrittweite ist fuer die Energie
**irrelevant** — das Modell rechnet rein aus `L`, `v0`, `vExit`.

## 3. Kalibrierung (Optuna)

- **Anker:** 1-m-Netz (feinste Auflösung, Diskretisierungsfehler minimal).
- **6 freie Parameter** (Bounds):
{bounds_md}
- `auxPowerW` fix bei **4000 W** (sehr kleiner Effekt).
- **5 Studien:** 4 Einzelszenarien (lh_low/high, rd_low/high) + 1 gemeinsame (`all`).
- Ziel: RMSE des Verbrauchs (kWh/km) gegen VECTO minimieren.

### Best-Parameter, RMSE und Optuna-Wichtigkeit je Studie

In Klammern: fANOVA-Parameterwichtigkeit dieser Studie (summiert = 100 %).

{df_to_md(cons_tbl)}

**Lesart:** η_traction dominiert die Wichtigkeit (~90 %+) und streut 0,84–0,94 *ohne*
beladungskonsistente Richtung → er saugt den Rest-Modellfehler auf, nicht nur die echte
Antriebseffizienz. η_recup/maxRecupFrac: groesste Streuung, kleinste Wirkung → praktisch
**unidentifiziert**. cdXA ist der einzige robust bestimmte Widerstand (nahe oberer A15-Grenze).
Einzelstudien treffen 0,55–0,80 %, joint nur 1,42 % (ein globaler Satz kann die unterschiedlich
beladenen Szenarien nicht gleichzeitig erfuellen).

## 4. Geschwindigkeitsprofile (rrc48 stellvertretend)

{df_to_md(spd_tbl, "{:.2f}")}

## 5. Energiebilanz / Widerstandsanteile (rrc48, kWh je ~100 km)

{df_to_md(res_tbl, "{:.2f}")}

Luftwiderstand dominiert (bei geringer Beladung bis ~68 % Aero); bei voller Beladung naehert
sich Roll:Aero 50:50 (Rollwiderstand waechst mit der Masse).

## 6. Rekuperation & Verluste (rrc48, kWh)

{df_to_md(loss_tbl, "{:.1f}")}

Groesster Verlust ist der Antriebsstrang (konstant 1−η_traction ≈ 14 % der Traktionsenergie).
Bergab wird der Grossteil schon von Roll+Aero aufgezehrt; echtes Bremsen ist klein, davon ~84 %
(= η_recup) rekuperiert, Reibbremse nahezu null.

## 7. Diskretisierungs-Konvergenz

**Methodik:** Sweep ueber Linklängen [1…750 m] × 4 Einzelszenarien (joint ausgeschlossen),
je eigener Parametersatz. Zwei analytisch isolierte Fehlerquellen, jeweils **in % des
Gesamtverbrauchs** (VECTO-Referenzenergie):

- **Aero-v³ (Jensen):** `(mittl. v)³ < mittl. v³` — das auf den Link-Freispeed kollabierte
  Profil unterschaetzt den konvexen v³-Term.
- **Grade (geglättetes Höhenprofil):** Die Höhenenergie `m·g·Δz` selbst ist
  diskretisierungsinvariant; der Verbrauch aendert sich nur durch die **Effizienz-Asymmetrie**
  bergauf (÷η_traction) vs. bergab (×η_recup, gecappt, Rest gebremst). Modelltreu per
  Kontrafaktum `E_batt(mit pGrav) − E_batt(ohne pGrav)` isoliert; "Wahrheit" = rohes 1s-VECTO-Profil.

Beide Fehler sind negativ (Unterschätzung), monoton in der Linklänge, und im Gesamtverbrauch
teils durch Leistungsbegrenzung an groben Links kompensiert → **nicht additiv** zum Gesamt-Δ.

### Konvergenztabellen (rrc48)

{conv_md}

## 8. Paper-Befund (Konvergenzaussage)

- **Kernaussage:** Der Gesamt-Diskretisierungsfehler (Δ vs. 1-m-Referenz) faellt **monoton**
  mit kuerzerer Linklänge — kein Sweet-Spot, sondern Konvergenz.
- **Empfehlung @ 400 m (Paper-Zielauflösung):**
  - Long Haul: Δ ≈ **1,6–3,0 %** → praktisch verlustfrei.
  - Regional Delivery: Δ ≈ **6,0–6,5 %** → prinzipielle Untergrenze bei realen Netzen.
- **Fehlerursachen @ 400 m:** Aero-v³ LH −5…−6 %, RD −8,5…−10 %; Grade LH −0,2…−0,35 %, RD −1,2 %.
- **Schwellen fuer <1 % Gesamtfehler:** LH bereits ~100–200 m; RD theoretisch ≤25–50 m
  (mit realen Netzen nicht erreichbar).
- **Faustformel:** Fehler skaliert mit der **v-/Steigungs-Varianz pro Link** — LH (gleichfoermige
  Autobahnfahrt) verzeiht grobe Netze, RD (Stop-and-Go) nicht.
- **Limitation:** Reale Netze sind unterhalb ~300–400 m rauschdominiert (Höhen-/Geometrierauschen);
  feiner als 400 m ist keine echte Mehrinformation. 400 m ist die feinste *sinnvoll* auflösbare Skala.
- **RRC-Hinweis:** rrc48 vs. rrc53 unterscheiden sich nur im konstanten Kalibrierungs-Offset
  (~1,1 pp), **nicht** im diskretisierungsbedingten Anteil.

## 9. Visualisierungen (Verweise)

- Konvergenzplots (diff% / Aero% / Grade%, log + linear): `results/convergence/<ts>/convergence.html`
- Vollständiger HTML-Report: `results/auswertung_energiemodell.html`
"""
    md += _real_validation_section()   # Abschnitt 10 (privat, optional)
    MD_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUTPUT.write_text(md, encoding="utf-8")
    print(f"Gespeichert: {MD_OUTPUT}")


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
    # Widerstands-/Verlustuebersicht (Abschnitt 3 + 4): nur rrc48 stellvertretend
    bd_all = bd_all[bd_all.index.str.contains("_rrc48_")]

    ref = {}
    for m in ("LongHaul", "RegionalDelivery"):
        ref.update(load_reference(m, "BET_G5"))
    bd_all["VECTO"] = [ref.get(v, {}).get("ee_kwh_per_km", float("nan")) for v in bd_all.index]
    bd_all["Diff_%"] = (bd_all["ekm"] - bd_all["VECTO"]) / bd_all["VECTO"] * 100

    cons_tbl = consistency_table(run)

    # --- Diskretisierungs-Konvergenz (neuester Sweep, falls vorhanden) ---
    disc_tbl_html = ""
    conv_fig_html = ""
    bd_conv = None
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

    # --- Tabellen ---
    res_tbl = bd_all[["E_roll", "E_aero", "E_gup", "E_gdn", "E_aup", "E_adn", "ekm", "VECTO", "Diff_%"]].copy()
    res_tbl["Roll%"] = bd_all["E_roll"] / (bd_all["E_roll"] + bd_all["E_aero"]) * 100
    res_tbl["Aero%"] = 100 - res_tbl["Roll%"]
    res_tbl.columns = ["Roll", "Aero", "Steig↑", "Gefälle↓", "Beschl↑",
                       "Verz↓", "kWh/km", "VECTO", "Diff%", "Roll%", "Aero%"]

    loss_tbl = bd_all[["mech_tr", "batt_tr", "loss_tr", "mech_br", "batt_rec",
                       "recup_loss", "fric", "net"]].copy()
    loss_tbl.columns = ["Mech-Trakt", "Batt-Trakt", "Antriebsverl.", "Mech-Brems",
                        "Rekup→Batt", "Rekup-Verl.", "Reibbremse", "Netto"]

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

<h2>1. Geschwindigkeitsprofile</h2>
{fig_html[0]}
{fig_html[1]}
<p>Kennwerte aller vier Profile (rrc48 stellvertretend, rrc53 nahezu identisch):</p>
{df_to_html(spd_tbl, "{:.2f}")}

<h2>2. Widerstandsanteile</h2>
{fig_html[2]}
{df_to_html(res_tbl, "{:.2f}")}
<div class="key"><b>Luftwiderstand dominiert</b> &ndash; bei geringer Beladung bis ~68&nbsp;% (Aero),
bei voller Beladung n&auml;hert sich Roll:Aero 50:50, da der Rollwiderstand mit der Masse w&auml;chst.</div>

<h2>3. Rekuperation &amp; Verluste</h2>
{fig_html[3]}
{df_to_html(loss_tbl, "{:.1f}")}
<div class="note">Die ~50&nbsp;kWh Gravitations-Energie sind <b>nicht</b> der Rekup-Pool: bergab wird der Gro&szlig;teil
schon von Roll+Aero aufgezehrt. Echtes Bremsen (Mech-Brems) ist deutlich kleiner; davon werden ~84&nbsp;%
(= &eta;_recup) zur&uuml;ckgewonnen, die <b>Reibbremse ist nahezu null</b> (Rekup-Leistungsgrenze bindet kaum).</div>
<div class="key">Gr&ouml;&szlig;ter Verlust ist der <b>Antriebsstrang</b> (Batterie&rarr;Rad, konstant 1&minus;&eta;_traction
&asymp; 14&nbsp;% der Traktionsenergie). Rekup-Verluste sind klein bei LH, h&ouml;her bei RD (mehr Stop-and-Go).</div>

<h2>4. Parameter-Konsistenz (Einzelstudien vs. joint)</h2>
{df_to_html(cons_tbl)}
<p style="font-size:13px;color:#555">In Klammern je Zelle: <b>Optuna-Parameterwichtigkeit</b> dieser Studie
(fANOVA, summiert = 100&nbsp;%) &ndash; wie stark der Parameter das RMSE-Ziel des jeweiligen Runs getrieben hat.</p>
<div class="key">Die <b>Einzelstudien treffen mit 0,55&ndash;0,80&nbsp;% nahezu perfekt</b>, die joint-Studie nur 1,42&nbsp;%
&ndash; ein globaler Parametersatz kann die unterschiedlich beladenen Szenarien nicht gleichzeitig erf&uuml;llen.</div>
<div class="note"><b>&eta;_traction</b> ist dominant, streut aber 0,84&ndash;0,94 <i>ohne</i> konsistente Beladungsrichtung
(LH steigt low&rarr;high, RD f&auml;llt) &ndash; er saugt also den <b>Rest-Modellfehler</b> auf, nicht nur die echte
Antriebseffizienz. <b>&eta;_recup / maxRecupFrac</b> haben die gr&ouml;&szlig;te Streuung bei kleinster Wirkung &rarr;
praktisch <b>unidentifiziert</b> (mit Vorsicht zu interpretieren). <b>cdXA</b> ist der einzige robust bestimmte
Widerstand (konsistent nahe der oberen A15-Grenze).</div>

<h2>5. Diskretisierungs-Konvergenz &amp; Limitation</h2>
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

<h2>6. Paper-Befund (Konvergenzaussage)</h2>
<div class="key"><ul>
<li><b>Kernaussage:</b> Der Gesamt-Diskretisierungsfehler (&Delta; Verbrauch vs. 1-m-Referenz)
f&auml;llt <b>monoton</b> mit k&uuml;rzerer Linkl&auml;nge &ndash; kein Sweet-Spot, sondern Konvergenz.
Die Frage lautet daher „ab welcher Aufl&ouml;sung ist der Restfehler vernachl&auml;ssigbar".</li>
<li><b>Empfehlung @ 400&nbsp;m (Paper-Zielaufl&ouml;sung):</b>
  <ul>
  <li><b>Long Haul:</b> &Delta;&nbsp;&asymp;&nbsp;1,6&ndash;3,0&nbsp;% &rarr; praktisch verlustfrei.</li>
  <li><b>Regional Delivery:</b> &Delta;&nbsp;&asymp;&nbsp;6,0&ndash;6,5&nbsp;% &rarr; prinzipielle Untergrenze
  bei realen (rauschdominierten) Netzen, keine behebbare Schw&auml;che.</li>
  </ul></li>
<li><b>Fehlerursachen (analytisch isoliert, @ 400&nbsp;m, in % des Gesamtverbrauchs):</b>
  <ul>
  <li><b>Aero-v³ (Jensen):</b> LH &minus;5&hellip;&minus;6&nbsp;%, RD &minus;8,5&hellip;&minus;10&nbsp;%
  &ndash; dominanter, monoton wachsender Anteil.</li>
  <li><b>Grade (gegl&auml;ttetes H&ouml;henprofil):</b> LH &minus;0,2&hellip;&minus;0,35&nbsp;%, RD &minus;1,2&nbsp;%
  &ndash; eine Gr&ouml;&szlig;enordnung kleiner, bei RD aber nicht vernachl&auml;ssigbar.</li>
  <li>Beide <b>untersch&auml;tzen</b> (negatives Vorzeichen); im Gesamtverbrauch teils durch
  Leistungsbegrenzung an groben Links kompensiert &rarr; Komponenten-Betr&auml;ge sind <b>nicht additiv</b>
  zum Gesamt-&Delta;.</li>
  </ul></li>
<li><b>Schwellen f&uuml;r &lt;1&nbsp;% Gesamtfehler:</b> LH bereits ~100&ndash;200&nbsp;m; RD theoretisch
&le;25&ndash;50&nbsp;m &ndash; mit realen Netzen nicht erreichbar.</li>
<li><b>Faustformel / &Uuml;bertrag:</b> Der Fehler skaliert mit der v-/Steigungs-<b>Varianz pro Link</b>.
LH (gleichf&ouml;rmige Autobahnfahrt) verzeiht grobe Netze, RD (Stop-and-Go) nicht &rarr; Aufl&ouml;sung
an die Geschwindigkeitsdynamik des Profils koppeln, nicht pauschal w&auml;hlen.</li>
<li><b>RRC-Hinweis:</b> rrc48 vs. rrc53 unterscheiden sich nur im konstanten Kalibrierungs-Offset
(~1,1&nbsp;pp), <b>nicht</b> im diskretisierungsbedingten Anteil.</li>
</ul></div>

<p style="margin-top:40px;color:#888;font-size:12px">Erzeugt mit analysis/report_summary.py</p>
</body></html>"""

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Gespeichert: {OUTPUT}")

    write_paper_markdown(run, trial, cons_tbl, res_tbl, loss_tbl, spd_tbl, bd_conv)


if __name__ == "__main__":
    main()
