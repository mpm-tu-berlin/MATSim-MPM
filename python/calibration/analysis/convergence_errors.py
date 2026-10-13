"""
Zerlegt den Diskretisierungsfehler des Sweeps (run_convergence_sweep.py) in
analytische Komponenten und erzeugt Konvergenzplots.

Komponenten je aggregiertem Link (Segment der VDRI-Punkte bei Auflösung N):
  * Aero-v³ (Jensen): fa * (v_link³·L - Σ v_i³·ds_i)
        v_link = harmonisches Mittel (wie im Netzgenerator). Da v³ konvex ist,
        ist v_link³·L <= Σ v_i³·ds_i -> negativer Beitrag (Unterschaetzung).
  * Grade-v (Diagnose): m·g · (grad_link·v_link·L - Σ grad_i·v_i·ds_i)
        Hinweis: die reine Grade-ENERGIE = m·g·Δz ist diskretisierungsinvariant
        (Endhoehe bleibt erhalten); diese Metrik zeigt nur die verlorene
        grad×v-Korrelation, energetisch ~0.
  * Speed-RMSE: streckengewichteter RMSE von v_link gegen das VDRI-Profil [m/s].

Aufruf:
    ../../.venv/Scripts/python analysis/convergence_errors.py
    ../../.venv/Scripts/python analysis/convergence_errors.py <convergence_run_dir>

Ausgabe (im Sweep-Verzeichnis):
    error_breakdown.csv
    convergence.html   (diff% gegen VECTO + Aero-% + Grade-% je Auflösung, log+linear)

Aero- und Grade-Fehler stehen in % des Gesamtverbrauchs (VECTO-Referenzenergie).
Der Grade-Fehler ist die Netto-Verbrauchsaenderung durch das geglaettete Hoehenprofil
(Effizienz-Asymmetrie bergauf/bergab); siehe grade_battery_energy.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from scripts.generate_vecto_networks import (
    read_vdri, compute_elevation, build_segments, segment_freespeed)
from src.config import DATA_DIR, RESULTS_DIR, MATSIM_MPM_DIR

G = 9.81
RHO = 1.225
J2KWH = 1.0 / 3.6e6
P_MOTOR_W = 407000.0   # VECTO Group-5 Motor-Spec (wie report_summary.py)

# Szenario -> (Mission, VDRI, Tag LH/RD, Payload-Klasse)
SCEN = {
    "lh_low":  ("LongHaul",         "LongHaul.vdri",         "LH", "low"),
    "lh_high": ("LongHaul",         "LongHaul.vdri",         "LH", "high"),
    "rd_low":  ("RegionalDelivery", "RegionalDelivery.vdri", "RD", "low"),
    "rd_high": ("RegionalDelivery", "RegionalDelivery.vdri", "RD", "high"),
}
RRC_VARIANTS = ["rrc48", "rrc53"]
PAPER_RESOLUTION_M = 400  # Ziel-Linklänge fuer das Paper (Markierung im Plot)
VEHICLES_XML = {
    "LongHaul":         MATSIM_MPM_DIR / "scenarios" / "VECTO_Longhaul_BET_G5" / "vehicles.xml",
    "RegionalDelivery": MATSIM_MPM_DIR / "scenarios" / "VECTO_RegionalDelivery_BET_G5" / "vehicles.xml",
}


def vehicle_mass_payload(xml_path: Path, type_id: str) -> tuple[float, float]:
    """Liest mass und payload eines vehicleType aus der vehicles.xml."""
    txt = xml_path.read_text(encoding="utf-8")
    block = re.search(rf'id="{re.escape(type_id)}".*?</vehicleType>', txt, re.S)
    seg = block.group(0) if block else txt
    mass = float(re.search(r'name="mass"[^>]*>([0-9.]+)', seg).group(1))
    payload = float(re.search(r'name="payload"[^>]*>([0-9.]+)', seg).group(1))
    return mass, payload


def read_params(params_file: Path) -> dict:
    d = {}
    for line in params_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = float(v.strip())
    return d


def component_errors(s_m, v_kmh, grad_pct, resolution_m, fa, mg):
    """Analytische Aero-/Grade-/Speed-Fehler bei gegebener Auflösung."""
    s = np.asarray(s_m)
    v = np.asarray(v_kmh) / 3.6      # m/s
    grad = np.asarray(grad_pct) / 100.0
    ds = np.diff(s)                  # Laenge je VDRI-Intervall

    segments = build_segments(s_m, float(resolution_m))
    aero_err = 0.0      # J
    grade_err = 0.0     # J
    sq_w = 0.0          # fuer streckengewichteten Speed-RMSE
    tot_L = 0.0
    for a, b in segments:
        L = s[b] - s[a]
        if L <= 0:
            continue
        seg_ds = ds[a:b]
        seg_v = v[a:b]
        seg_grad = grad[a:b]
        v_link = segment_freespeed(s_m, v_kmh, a, b)   # harmonisches Mittel [m/s]

        true_v3 = np.sum(seg_v ** 3 * seg_ds)
        aero_err += fa * (v_link ** 3 * L - true_v3)

        true_gv = np.sum(seg_grad * seg_v * seg_ds)
        grad_link = (grad[a:b] * seg_ds).sum() / L     # streckengemittelte Steigung
        grade_err += mg * (grad_link * v_link * L - true_gv)

        sq_w += np.sum((v_link - seg_v) ** 2 * seg_ds)
        tot_L += L

    speed_rmse = float(np.sqrt(sq_w / tot_L)) if tot_L > 0 else float("nan")
    return aero_err * J2KWH, grade_err * J2KWH, speed_rmse


def grade_battery_energy(s_m, v_kmh, grad_pct, resolution_m,
                         ft, fa, m_sum, eta_t, eta_r, p_recup_w) -> float:
    """Netto-Batterieenergie [J] aus der Steigung bei gegebener Auflösung.

    Modelltreu zu MpmDynamicBetDriveEnergyConsumption: pGrav geht in
    pResist = pRoll + pAero + pGrav ein, dessen Vorzeichen Traktion (÷eta_t,
    gecappt bei P_MOTOR_W) vs. Rekuperation (×eta_r, gecappt bei -p_recup_w)
    bestimmt. Der grade-EIGENE Anteil wird per Kontrafaktum isoliert:
    E_batt(mit pGrav) - E_batt(ohne pGrav). resolution_m=0 -> Roh-Intervalle
    (1s-Profil = "Wahrheit").

    Rollwiderstand mit cos(theta)-Korrektur (ftEff = ft*sqrt(1-grade^2)), wie im
    Java-Modell. Die gekoppelte Effizienz-Entscheidung des Modells (ein eta je
    Vorzeichen der Netto-Mechanikenergie eMech = pResist*t + dKE) reduziert sich
    hier auf sign(pResist): dieses per-Link-stationaere Modell kennt kein
    v0/vExit-Chaining, also dKE=0 und sign(eMech)=sign(pResist).
    """
    s = np.asarray(s_m)
    v = np.asarray(v_kmh) / 3.6
    grad = np.asarray(grad_pct) / 100.0
    ds = np.diff(s)

    def batt(p, t):
        if p >= 0.0:
            return min(p / eta_t, P_MOTOR_W) * t
        return max(p * eta_r, -p_recup_w) * t

    e_grade = 0.0   # J
    for a, b in build_segments(s_m, float(resolution_m)):
        L = s[b] - s[a]
        if L <= 0:
            continue
        v_link = segment_freespeed(s_m, v_kmh, a, b)        # harm. Mittel [m/s]
        grad_link = (grad[a:b] * ds[a:b]).sum() / L          # = (z[b]-z[a])/L
        t = L / v_link
        cos_grade = np.sqrt(1.0 - grad_link * grad_link)     # Normalkraft-Korrektur
        p_roll = ft * cos_grade * m_sum * G * v_link
        p_aero = fa * v_link ** 3
        p_grav = m_sum * G * grad_link * v_link
        e_grade += batt(p_roll + p_aero + p_grav, t) - batt(p_roll + p_aero, t)
    return e_grade


COLORS = {"lh_low": "#1f77b4", "lh_high": "#17386b",
          "rd_low": "#ff7f0e", "rd_high": "#a85405"}
DASH = {"rrc48": "solid", "rrc53": "dot"}


def convergence_fig(bd: pd.DataFrame, xaxis_type: str = "log"):
    """3-zeiliger Konvergenzplot (diff% + Aero-% + Grade-%) mit waehlbarer x-Achse."""
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        subplot_titles=[
            "Abweichung gegen VECTO [%]",
            "Aero-v³-Diskretisierungsfehler [% des Gesamtverbrauchs]",
            "Grade-Diskretisierungsfehler (geglättetes Höhenprofil) [% des Gesamtverbrauchs]"])
    for scen in SCEN:
        for rrc in RRC_VARIANTS:
            d = bd[(bd["scenario"] == scen) & (bd["rrc"] == rrc)].sort_values("resolution_m")
            fig.add_trace(go.Scatter(x=d["resolution_m"], y=d["diff_pct"],
                                     name=f"{scen} {rrc}", mode="lines+markers",
                                     line=dict(color=COLORS[scen], dash=DASH[rrc]),
                                     legendgroup=f"{scen}_{rrc}"), row=1, col=1)
        # Aero/Grade sind rrc-unabhaengig (gleiches cdXA, ~gleiche Masse) -> rrc48 stellv.
        d48 = bd[(bd["scenario"] == scen) & (bd["rrc"] == "rrc48")].sort_values("resolution_m")
        fig.add_trace(go.Scatter(x=d48["resolution_m"], y=d48["aero_err_pct"], name=scen,
                                 mode="lines+markers", line=dict(color=COLORS[scen]),
                                 showlegend=False), row=2, col=1)
        fig.add_trace(go.Scatter(x=d48["resolution_m"], y=d48["grade_err_pct"], name=scen,
                                 mode="lines+markers", line=dict(color=COLORS[scen]),
                                 showlegend=False), row=3, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", row=1, col=1)
    for r in (1, 2, 3):
        ann = dict(annotation_text=f"{PAPER_RESOLUTION_M} m", annotation_position="top") if r == 1 else {}
        fig.add_vline(x=PAPER_RESOLUTION_M, line_dash="dot", line_color="red", row=r, col=1, **ann)
    suffix = "log" if xaxis_type == "log" else "linear"
    fig.update_xaxes(type=xaxis_type, title_text=f"Linklänge [m] ({suffix})", row=3, col=1)
    fig.update_yaxes(title_text="Diff [%]", row=1, col=1)
    fig.update_yaxes(title_text="Aero-Fehler [%]", row=2, col=1)
    fig.update_yaxes(title_text="Grade-Fehler [%]", row=3, col=1)
    fig.update_layout(title=f"Diskretisierungs-Konvergenz BET_G5 — x-Achse {suffix}",
                      template="plotly_white", height=960, hovermode="x unified")
    return fig


def main() -> None:
    if len(sys.argv) >= 2:
        run = Path(sys.argv[1])
    else:
        runs = sorted((RESULTS_DIR / "convergence").glob("*"),
                      key=lambda p: p.stat().st_mtime)
        if not runs:
            raise SystemExit("Kein Sweep unter results/convergence gefunden.")
        run = runs[-1]
    print(f"Sweep: {run.name}")

    cons = pd.read_csv(run / "consumption.csv")
    resolutions = sorted(cons["resolution_m"].unique())

    rows = []
    for scen, (mission, vdri_name, tag, payload) in SCEN.items():
        s_m, v_kmh, grad_pct = read_vdri(DATA_DIR / vdri_name)
        params = read_params(run / "params" / f"{scen}.properties")
        fa = 0.5 * RHO * params["cdXA"]
        eta_t = params["tractionEfficiency"]
        eta_r = params["recupEfficiency"]
        p_recup_w = params["maxRecupPowerFraction"] * P_MOTOR_W
        ft = params["rollingC"]
        for rrc in RRC_VARIANTS:
            type_id = f"BET_G5_{rrc.upper()}_{tag}_{payload}"
            vehicle_id = f"truck_g5_{rrc}_{tag.lower()}_{payload}"
            mass, pay = vehicle_mass_payload(VEHICLES_XML[mission], type_id)
            m_sum = mass + pay
            mg = m_sum * G
            # Netto-Grade-Batterieenergie ueber das rohe 1s-Profil = "Wahrheit"
            grade_true = grade_battery_energy(s_m, v_kmh, grad_pct, 0,
                                              ft, fa, m_sum, eta_t, eta_r, p_recup_w)
            for res in resolutions:
                aero, grade_diag, srmse = component_errors(s_m, v_kmh, grad_pct, res, fa, mg)
                grade_res = grade_battery_energy(s_m, v_kmh, grad_pct, res,
                                                 ft, fa, m_sum, eta_t, eta_r, p_recup_w)
                grade_err = (grade_res - grade_true) * J2KWH
                sub = cons[(cons["scenario"] == scen) & (cons["resolution_m"] == res)
                           & (cons["vehicle_id"] == vehicle_id)]
                diff_pct = float(sub["diff_pct"].iloc[0]) if len(sub) else float("nan")
                ekm = float(sub["ee_kwh_per_km"].iloc[0]) if len(sub) else float("nan")
                if len(sub):
                    vecto_energy = float(sub["vecto_kwh_per_km"].iloc[0]) * float(sub["route_km"].iloc[0])
                else:
                    vecto_energy = float("nan")
                aero_pct = aero / vecto_energy * 100 if vecto_energy else float("nan")
                grade_pct_err = grade_err / vecto_energy * 100 if vecto_energy else float("nan")
                rows.append(dict(scenario=scen, rrc=rrc, mission=mission, resolution_m=res,
                                 aero_err_kwh=round(aero, 4), aero_err_pct=round(aero_pct, 4),
                                 grade_err_kwh=round(grade_err, 4), grade_err_pct=round(grade_pct_err, 4),
                                 speed_rmse_ms=round(srmse, 4),
                                 matsim_kwh_per_km=ekm, diff_pct=diff_pct))

    bd = pd.DataFrame(rows)
    csv_path = run / "error_breakdown.csv"
    bd.to_csv(csv_path, index=False)
    print(f"Gespeichert: {csv_path}")

    # --- Plot: log- und lineare x-Achse in derselben Datei ---
    fig_log = convergence_fig(bd, "log")
    fig_lin = convergence_fig(bd, "linear")
    html = run / "convergence.html"
    body = (
        "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>"
        "<title>Diskretisierungs-Konvergenz BET_G5</title>"
        "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}"
        "h1{border-bottom:3px solid #1f77b4;padding-bottom:6px}h2{color:#1f77b4}</style>"
        "</head><body><h1>Diskretisierungs-Konvergenz BET_G5 (Einzelszenarien)</h1>"
        "<h2>Logarithmische x-Achse</h2>"
        + fig_log.to_html(full_html=False, include_plotlyjs="cdn")
        + "<h2>Lineare x-Achse</h2>"
        + fig_lin.to_html(full_html=False, include_plotlyjs=False)
        + "</body></html>"
    )
    html.write_text(body, encoding="utf-8")
    print(f"Gespeichert: {html}")


if __name__ == "__main__":
    main()
