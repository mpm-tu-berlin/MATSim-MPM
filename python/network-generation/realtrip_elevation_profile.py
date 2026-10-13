# -*- coding: utf-8 -*-
"""
Höhenlinien-Auswertung der Realfahrt-Routen: Netz-z gegen gemessenes
Höhenprofil — prüft, ob die Routen-Netze künstliche Steigungen/Gefälle
enthalten und wie viel Energie diese kosten.

Deckt beide Datenquellen ab:
  - klassische Fahrten 19t/24t/43t (Geschwindigkeitsprofile.xlsx + Szenario-Netz)
  - Telemetrie-Trips f22a/f22b/... (trip_<label>.csv + section_<label>_<res>m.xml.gz)

Ausgabe (alles gitignored unter data/realtrip_elevation/):
  - hoehenlinie_<label>_V<N>.png/.pdf   3 Panels: Höhe, Steigung, kum. Anstieg
  - grades_<label>_V<N>.csv             Steigung je Link (Netz)
  - excess_climb_<label>_V<N>.csv       Anstiegs-Überschuss je Link
  - metrics_<label>_V<N>.csv            Kennzahlen je Route
  - referenz_alle_routen_V<N>.csv/.png  Übersicht über alle Routen (--all)

Aufruf:
  python realtrip_elevation_profile.py --trip 43t
  python realtrip_elevation_profile.py --trip h19 --resolution 400
  python realtrip_elevation_profile.py --all

VERTRAULICH: liest reale Messdaten (nur Bogenlänge/Höhe, KEINE Leistungsspalten);
Ausgaben liegen unter ignorierten Pfaden und sind ausschließlich Aggregate.
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from importlib.util import spec_from_file_location, module_from_spec

_SCRIPT_DIR = Path(__file__).parent
_ROOT = _SCRIPT_DIR.parent.parent
_DATA = _SCRIPT_DIR / "data"

# --- klassische Fahrten -------------------------------------------------
CLASSIC_SCEN = {
    "19t": _ROOT / "scenarios" / "19t_BET_G5",
    "24t": _ROOT / "scenarios" / "24t_BET_G5_100km",
    "43t": _ROOT / "scenarios" / "43t_BET_G5",
}
CLASSIC_EXCEL_SUFFIX = {"19t": "19t", "24t": "25t", "43t": "43t"}
CLASSIC_MASS_KG = {"19t": 19000.0, "24t": 24600.0, "43t": 43000.0}
EXCEL_PATH = _ROOT / "python" / "calibration" / "data" / "Geschwindigkeitsprofile.xlsx"

# --- Telemetrie-Trips ---------------------------------------------------
TELEMETRY_PROFILES = _DATA / "realtrip_telemetry_profiles"
TELEMETRY_NETWORKS = _DATA / "realtrip_networks_telemetry_20260806"
TELEMETRY_RESOLUTIONS = [400, 250, 100]

G = 9.81
BASELINES_M = [100, 250, 500, 1000]


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def telemetry_labels():
    meta = TELEMETRY_PROFILES / "trips_meta.csv"
    if not meta.exists():
        return []
    m = pd.read_csv(meta)
    return [str(x) for x in m["label"].tolist()]


def resolve_route(label, resolution=None):
    """(Netzpfad, s_meas [m], z_meas [m], Masse [kg]) für eine Route."""
    if label in CLASSIC_SCEN:
        scen = CLASSIC_SCEN[label]
        cands = sorted(scen.glob("*_realspeed_trimmed.xml.gz")) or \
                sorted(scen.glob("*_realspeed.xml.gz"))
        if not cands:
            raise SystemExit(f"Kein Routen-Netz in {scen}")
        col = CLASSIC_EXCEL_SUFFIX[label]
        meas = pd.read_excel(EXCEL_PATH)
        m = meas[[f"Mileage {col}", f"Altitude {col}"]].dropna()
        s = (m[f"Mileage {col}"].values - m[f"Mileage {col}"].values[0]) * 1000.0
        return cands[0], s, m[f"Altitude {col}"].values, CLASSIC_MASS_KG[label]

    # Telemetrie: nur Bogenlänge und Höhe einlesen, keine Leistungsspalten
    prof = TELEMETRY_PROFILES / f"trip_{label}.csv"
    if not prof.exists():
        raise SystemExit(f"Kein Telemetrie-Profil: {prof}")
    t = pd.read_csv(prof, usecols=["s_m", "alt_m"]).dropna()
    s = t["s_m"].values - t["s_m"].values[0]

    res_list = [resolution] if resolution else TELEMETRY_RESOLUTIONS
    net = None
    for r in res_list:
        p = TELEMETRY_NETWORKS / f"section_{label}_{r}m_realspeed.xml.gz"
        if not p.exists():
            p = TELEMETRY_NETWORKS / f"section_{label}_{r}m.xml.gz"
        if p.exists():
            net = p
            break
    if net is None:
        raise FileNotFoundError(f"Kein Telemetrie-Netz für {label}")

    meta = pd.read_csv(TELEMETRY_PROFILES / "trips_meta.csv")
    row = meta.loc[meta["label"].astype(str) == label]
    mass = float(row["mass_t"].iloc[0]) * 1000.0 if not row.empty else 40000.0
    return net, s, t["alt_m"].values, mass


def cumulative_climb(s, z, baseline=None):
    """Kumulierter Anstieg/Abstieg [m]; optional auf Basislänge resampelt."""
    if baseline:
        grid = np.arange(s[0], s[-1] + baseline, baseline)
        z = np.interp(grid, s, z)
    dz = np.diff(z)
    return float(np.sum(dz[dz > 0])), float(-np.sum(dz[dz < 0]))


def grade_series(s, z, baseline):
    grid = np.arange(s[0], s[-1], baseline)
    zz = np.interp(grid, s, z)
    return grid[:-1] + baseline / 2.0, 100.0 * np.diff(zz) / baseline


def analyze(label, outdir, version, rme, resolution=None,
            eta_t=0.87, recup_eff=0.75, spike_grade=6.0, make_plot=True):
    """Vollauswertung einer Route. Rückgabe: dict der Kennzahlen."""
    net_path, s_meas, z_meas, mass = resolve_route(label, resolution)
    prof = rme.load_chain_profile(net_path)
    s_net, z_net, chain = prof["s"], prof["z"], prof["chain"]

    direction, offset, scale, corr = rme.align_profiles(s_net, z_net, s_meas, z_meas)
    if direction < 0:
        s_al = (s_meas[-1] - s_meas[::-1]) * scale + offset
        z_al = z_meas[::-1]
    else:
        s_al = s_meas * scale + offset
        z_al = z_meas

    link_len = np.diff(s_net)
    link_grade = 100.0 * np.diff(z_net) / link_len
    spike_mask = np.abs(link_grade) > spike_grade

    z_meas_at = np.interp(s_net, s_al, z_al)
    excess = np.maximum(np.diff(z_net), 0.0) - np.maximum(np.diff(z_meas_at), 0.0)

    up_n_250, _ = cumulative_climb(s_net, z_net, 250)
    up_m_250, _ = cumulative_climb(s_al, z_al, 250)
    d_h = up_n_250 - up_m_250
    km = s_net[-1] / 1000.0
    # Mehrarbeit bergauf abzüglich rekuperiertem Anteil des Mehr-Gefälles
    e_factor = (1.0 / eta_t - recup_eff) / 3.6e6
    e_250 = mass * G * d_h * e_factor
    e_link = mass * G * float(excess.sum()) * e_factor

    res = {
        "route": label,
        "netz": net_path.name,
        "masse_t": mass / 1000.0,
        "route_len_netz_km": km,
        "route_len_mess_km": (s_meas[-1] - s_meas[0]) / 1000.0,
        "links": len(chain),
        "linklaenge_mittel_m": float(link_len.mean()),
        "align_richtung": float(direction),
        "align_scale": float(scale),
        "align_corr_steigung": float(corr),
        "z_min_m": float(np.nanmin(z_net)),
        "z_max_m": float(np.nanmax(z_net)),
        "grad_abs_median_pct": float(np.median(np.abs(link_grade))),
        "grad_abs_p95_pct": float(np.percentile(np.abs(link_grade), 95)),
        "grad_max_pct": float(np.max(link_grade)),
        "grad_min_pct": float(np.min(link_grade)),
        "spike_links": int(spike_mask.sum()),
        "spike_links_pro_100km": float(spike_mask.sum() / km * 100.0),
        "anstieg_ueberschuss_250m_m": d_h,
        "anstieg_ueberschuss_linkbasis_m": float(excess.sum()),
        "ueberschuss_aus_spike_links_m": float(excess[spike_mask].sum()),
        "ueberschuss_m_pro_km": d_h / km,
        "energie_ueberschuss_kWh": e_250,
        "energie_ueberschuss_kWh_pro_km": e_250 / km,
        "energie_ueberschuss_linkbasis_kWh_pro_km": e_link / km,
    }
    for b in BASELINES_M:
        up_n, _ = cumulative_climb(s_net, z_net, b)
        up_m, _ = cumulative_climb(s_al, z_al, b)
        res[f"anstieg_netz_{b}m_m"] = up_n
        res[f"anstieg_mess_{b}m_m"] = up_m
        res[f"steigungs_mae_{b}m_pct"] = rme.slope_mae(s_net, z_net, s_al, z_al, b)

    pd.DataFrame(sorted(res.items()), columns=["kennzahl", "wert"]).to_csv(
        outdir / f"metrics_{label}_V{version}.csv", index=False, sep=";")
    pd.DataFrame({
        "link_id": [c["id"] for c in chain],
        "km": s_net[:-1] / 1000.0,
        "laenge_m": link_len,
        "steigung_netz_pct": link_grade,
        "steigung_mess_pct": 100.0 * np.diff(z_meas_at) / link_len,
        "anstieg_ueberschuss_m": excess,
    }).sort_values("anstieg_ueberschuss_m", ascending=False).to_csv(
        outdir / f"excess_climb_{label}_V{version}.csv", index=False, sep=";")

    if make_plot:
        _plot(label, outdir, version, s_net, z_net, s_al, z_al,
              chain, link_grade, spike_mask, spike_grade)
    return res


def _plot(label, outdir, version, s_net, z_net, s_al, z_al,
          chain, link_grade, spike_mask, spike_grade):
    C_NET, C_MEAS = "#1f77b4", "#d62728"
    fig, axes = plt.subplots(3, 1, figsize=(9.5, 8.4), sharex=True,
                             gridspec_kw={"height_ratios": [1.25, 1.0, 0.9]})

    ax = axes[0]
    ax.plot(s_al / 1000.0, z_al, color=C_MEAS, lw=1.1, label="Messung (Fahrzeug)")
    ax.plot(s_net / 1000.0, z_net, color=C_NET, lw=1.3, label="MATSim-Netz (Knoten-z)")
    if spike_mask.any():
        ax.plot(s_net[1:][spike_mask] / 1000.0, z_net[1:][spike_mask], "v", ms=5,
                color="black", label=f"Link |Steigung| > {spike_grade:g} %")
    ax.set_ylabel("Höhe [m ü. NN]")
    ax.set_title(f"Höhenlinie Realfahrt {label} — Netz vs. Messung "
                 f"({s_net[-1]/1000.0:.1f} km, {len(chain)} Links)")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.3)

    ax = axes[1]
    gs_m, gg_m = grade_series(s_al, z_al, 250)
    gs_n, gg_n = grade_series(s_net, z_net, 250)
    ax.axhline(0, color="0.6", lw=0.7)
    ax.plot(gs_m / 1000.0, gg_m, color=C_MEAS, lw=0.9, label="Messung")
    ax.plot(gs_n / 1000.0, gg_n, color=C_NET, lw=1.0, label="Netz")
    ax.set_ylabel("Steigung (%) (250-m-Basis)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)

    ax = axes[2]
    for s_, z_, c_, lab in ((s_al, z_al, C_MEAS, "Messung"), (s_net, z_net, C_NET, "Netz")):
        grid = np.arange(s_[0], s_[-1], 250.0)
        dz = np.diff(np.interp(grid, s_, z_))
        ax.plot(grid[1:] / 1000.0, np.cumsum(np.maximum(dz, 0.0)), color=c_, lw=1.3, label=lab)
    ax.set_ylabel("kumulierter Anstieg (m)")
    ax.set_xlabel("Bogenlänge entlang der Route (km)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    fig.tight_layout()
    base = outdir / f"hoehenlinie_{label}_V{version}"
    fig.savefig(base.with_suffix(".png"), dpi=200, bbox_inches="tight")
    fig.savefig(base.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)


def _overview_plot(df, outdir, version):
    d = df.sort_values("ueberschuss_m_pro_km")
    fig, axes = plt.subplots(1, 2, figsize=(11, 0.42 * len(d) + 2.2), sharey=True)
    y = np.arange(len(d))

    axes[0].barh(y, d["ueberschuss_m_pro_km"], color="#1f77b4")
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(d["route"])
    axes[0].set_xlabel("Anstiegs-Überschuss [m/km]")
    axes[0].set_title("Netz minus Messung (250-m-Basis)")
    axes[0].grid(alpha=0.3, axis="x")

    axes[1].barh(y, d["energie_ueberschuss_kWh_pro_km"], color="#d62728")
    axes[1].set_xlabel("Energie-Äquivalent (kWh/km)")
    axes[1].set_title("Mehrverbrauch durch Scheinsteigung")
    axes[1].grid(alpha=0.3, axis="x")

    fig.suptitle("Höhenqualität der Routen-Netze gegen Messung — alle Realfahrten")
    fig.tight_layout()
    base = outdir / f"referenz_alle_routen_V{version}"
    fig.savefig(base.with_suffix(".png"), dpi=200, bbox_inches="tight")
    fig.savefig(base.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description="Höhenlinie Netz vs. Messung für Realfahrten.")
    ap.add_argument("--trip", default=None, help="Route (19t/24t/43t oder Telemetrie-Label)")
    ap.add_argument("--all", action="store_true", help="alle verfügbaren Routen")
    ap.add_argument("--resolution", type=int, default=None, help="Telemetrie-Netzauflösung [m]")
    ap.add_argument("--outdir", type=str, default=str(_DATA / "realtrip_elevation"))
    ap.add_argument("--eta-t", type=float, default=0.87, help="Traktionswirkungsgrad")
    ap.add_argument("--recup-eff", type=float, default=0.75, help="Rekuperationswirkungsgrad")
    ap.add_argument("--spike-grade", type=float, default=6.0, help="Spike-Schwelle [%%]")
    args = ap.parse_args()

    rme = _import_module("rme", "realtrip_measured_eval.py")
    kpa = _import_module("kpa", "knee_point_analysis.py")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    labels = ([*CLASSIC_SCEN] + telemetry_labels()) if args.all else [args.trip or "43t"]
    # Version über ALLE Routen hochzählen, sonst kollidiert eine Route, die
    # schon öfter gelaufen ist, mit einer neu hinzugekommenen.
    version = max(kpa._next_version(outdir, base=f"hoehenlinie_{lab}") for lab in labels)

    rows = []
    for lab in labels:
        try:
            rows.append(analyze(lab, outdir, version, rme, resolution=args.resolution,
                                eta_t=args.eta_t, recup_eff=args.recup_eff,
                                spike_grade=args.spike_grade))
            print(f"[ok]   {lab}")
        except Exception as exc:
            print(f"[skip] {lab}: {exc}")

    if not rows:
        raise SystemExit("Keine Route ausgewertet.")
    df = pd.DataFrame(rows)
    cols = ["route", "masse_t", "route_len_netz_km", "linklaenge_mittel_m",
            "align_corr_steigung", "spike_links", "spike_links_pro_100km",
            "anstieg_ueberschuss_250m_m", "ueberschuss_m_pro_km",
            "steigungs_mae_250m_pct", "energie_ueberschuss_kWh_pro_km"]
    if len(rows) > 1:
        df.to_csv(outdir / f"referenz_alle_routen_V{version}.csv", index=False, sep=";")
        _overview_plot(df, outdir, version)
    print()
    print(df[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))


if __name__ == "__main__":
    main()
