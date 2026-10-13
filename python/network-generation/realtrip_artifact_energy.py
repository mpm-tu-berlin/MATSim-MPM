# -*- coding: utf-8 -*-
"""
Korrekturpotenzial der Netz-Hoehenartefakte: Hub- UND Beschleunigungsanteil.

Die reine m*g*dh-Rechnung unterschaetzt den Effekt, weil sie den
Leistungsbegrenzer ignoriert. Auf einer SCHEIN-Steigung wird der Sim-Truck unter
die gemessene Realgeschwindigkeit gebremst und muss danach wieder beschleunigen.
Diese Wiederbeschleunigung kostet 1/eta_t, waehrend das vorangehende Schein-
Gefaelle nur eta_recup zurueckgibt — dieselbe Asymmetrie wie beim Hubterm, aber
zusaetzlich.

Zwei Anteile, beide als Differenz Netz-gegen-Messung:
  Hub:   m*g*(dh_netz - dh_mess) * (1/eta_t - eta_recup)
  Kin.:  Zyklierkosten der Geschwindigkeitsfolge, Sim gegen Real
         mit v_sim = min(freespeed, v_limit(Netzsteigung)), v_real = freespeed
         (Freespeed IST die gemessene Realgeschwindigkeit in diesen Netzen)

Aufruf:
  python realtrip_artifact_energy.py --all
  python realtrip_artifact_energy.py --trip 43t

VERTRAULICH: Messdaten nur aggregiert; Ausgabe unter ignoriertem Pfad.
"""

import argparse
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

from importlib.util import spec_from_file_location, module_from_spec

_SCRIPT_DIR = Path(__file__).parent
_DATA = _SCRIPT_DIR / "data"

G = 9.81
RHO = 1.2
CDXA = 5.79          # BET_G5, aus vehicles.xml
ROLLING_C = 0.0048
P_MOTOR_W = 600_000.0
V_MAX = 27.778       # 100 km/h
ETA_T = 0.87
ETA_R = 0.75


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def link_freespeeds(net_path, chain):
    """Freespeed [m/s] je Link in Kettenreihenfolge."""
    with gzip.open(net_path, "rb") as f:
        root = ET.parse(f).getroot()
    fs = {l.get("id"): float(l.get("freespeed")) for l in root.find("links").findall("link")}
    return np.array([fs.get(c["id"], V_MAX) for c in chain])


def power_limited_speed(grade, mass, p_wheel_w, v_cap=V_MAX):
    """Gleichgewichtsgeschwindigkeit bei Leistungsgrenze (vektorisiert, Bisektion).

    P = [m*g*(sin + c_rr*cos) + 0.5*rho*cdXA*v^2] * v
    """
    theta = np.arctan(grade)
    f_static = mass * G * (np.sin(theta) + ROLLING_C * np.cos(theta))
    lo = np.full_like(grade, 0.1)
    hi = np.full_like(grade, v_cap)

    def need(v):
        return (f_static + 0.5 * RHO * CDXA * v ** 2) * v

    # Wo schon bei v_cap genug Leistung bleibt: keine Begrenzung
    unlimited = need(hi) <= p_wheel_w
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        too_much = need(mid) > p_wheel_w
        hi = np.where(too_much, mid, hi)
        lo = np.where(too_much, lo, mid)
    v = 0.5 * (lo + hi)
    return np.where(unlimited, v_cap, v)


def cycling_cost(v, mass, eta_t=ETA_T, eta_r=ETA_R):
    """Nicht rueckgewonnene kinetische Energie einer Geschwindigkeitsfolge [J]."""
    d_ke = 0.5 * mass * np.diff(v ** 2)
    return float(np.sum(np.maximum(d_ke, 0.0)) / eta_t
                 - eta_r * np.sum(np.maximum(-d_ke, 0.0)))


def analyze(label, rep, rme, eta_t=ETA_T, eta_r=ETA_R):
    net_path, s_meas, z_meas, mass = rep.resolve_route(label)
    prof = rme.load_chain_profile(net_path)
    s, z_net, chain = prof["s"], prof["z"], prof["chain"]

    direction, offset, scale, corr = rme.align_profiles(s, z_net, s_meas, z_meas)
    if direction < 0:
        s_al = (s_meas[-1] - s_meas[::-1]) * scale + offset
        z_al = z_meas[::-1]
    else:
        s_al = s_meas * scale + offset
        z_al = z_meas

    L = np.maximum(np.diff(s), 1.0)
    z_meas_at = np.interp(s, s_al, z_al)
    g_net = np.diff(z_net) / L
    g_meas = np.diff(z_meas_at) / L

    # --- Hubanteil (Anstiegs-Ueberschuss, Linkbasis) ---
    up_net = np.sum(np.maximum(np.diff(z_net), 0.0))
    up_meas = np.sum(np.maximum(np.diff(z_meas_at), 0.0))
    e_pot = mass * G * (up_net - up_meas) * (1.0 / eta_t - eta_r)

    # --- Beschleunigungsanteil ---
    v_free = np.minimum(link_freespeeds(net_path, chain), V_MAX)
    p_wheel = P_MOTOR_W * eta_t
    v_lim_net = power_limited_speed(g_net, mass, p_wheel)
    v_sim = np.minimum(v_free, v_lim_net)
    e_kin = cycling_cost(v_sim, mass, eta_t, eta_r) - cycling_cost(v_free, mass, eta_t, eta_r)

    km = s[-1] / 1000.0
    limited = v_sim < v_free - 0.1
    return {
        "route": label,
        "masse_t": mass / 1000.0,
        "km": km,
        "align_corr": corr,
        "anstieg_ueberschuss_m": up_net - up_meas,
        "links_leistungsbegrenzt": int(limited.sum()),
        "anteil_links_begrenzt_pct": 100.0 * limited.mean(),
        "v_einbruch_max_kmh": float(np.max((v_free - v_sim)) * 3.6),
        "E_hub_kWh": e_pot / 3.6e6,
        "E_kin_kWh": e_kin / 3.6e6,
        "E_gesamt_kWh": (e_pot + e_kin) / 3.6e6,
        "E_hub_kWh_pro_km": e_pot / 3.6e6 / km,
        "E_kin_kWh_pro_km": e_kin / 3.6e6 / km,
        "E_gesamt_kWh_pro_km": (e_pot + e_kin) / 3.6e6 / km,
    }


def main():
    ap = argparse.ArgumentParser(description="Korrekturpotenzial der Hoehenartefakte.")
    ap.add_argument("--trip", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--eta-t", type=float, default=ETA_T)
    ap.add_argument("--recup-eff", type=float, default=ETA_R)
    ap.add_argument("--outdir", type=str, default=str(_DATA / "realtrip_elevation"))
    args = ap.parse_args()

    rep = _import_module("rep", "realtrip_elevation_profile.py")
    rme = _import_module("rme", "realtrip_measured_eval.py")
    kpa = _import_module("kpa", "knee_point_analysis.py")

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    labels = ([*rep.CLASSIC_SCEN] + rep.telemetry_labels()) if args.all else [args.trip or "43t"]

    rows = []
    for lab in labels:
        try:
            rows.append(analyze(lab, rep, rme, args.eta_t, args.recup_eff))
            print(f"[ok]   {lab}")
        except Exception as exc:
            print(f"[skip] {lab}: {exc}")
    if not rows:
        raise SystemExit("Keine Route ausgewertet.")

    df = pd.DataFrame(rows)
    version = kpa._next_version(outdir, base="artefakt_energie")
    df.to_csv(outdir / f"artefakt_energie_V{version}.csv", index=False, sep=";")

    print()
    cols = ["route", "masse_t", "km", "anstieg_ueberschuss_m", "anteil_links_begrenzt_pct",
            "v_einbruch_max_kmh", "E_hub_kWh_pro_km", "E_kin_kWh_pro_km", "E_gesamt_kWh_pro_km"]
    print(df[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(f"\nCSV: {outdir / f'artefakt_energie_V{version}.csv'}")


if __name__ == "__main__":
    main()
