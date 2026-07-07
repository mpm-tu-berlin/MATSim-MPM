# -*- coding: utf-8 -*-
"""
Auswertung des 20-Sektionen-Diskretisierungs-Sweeps
(energy_results_summary.csv aus run_section_energy_analysis.py).

Redesign 2026-07-06 (User): ersetzt die alte 2/3-Sektionen-Studie. Kernpunkte:
  - KEIN Konvergenz-Framing. Feine Stufen konvergieren nicht (DTM-Restfehler
    erzeugt skalenabhaengigen Verbrauch); Referenz ist die 250-m-Stufe
    (VECTO-verankerte Kalibrier-Skala), NICHT ein Kontinuumslimit.
  - Darstellung RELATIV zur 250-m-Stufe je (Sektion, Beladung), nicht absolut
    und nicht relativ zur rauschbehafteten 50-m-Stufe.
  - Kniepunkt je (Sektion, Beladung) via Kneedle auf spline-geglaetteter Kurve.
  - HAUPTERGEBNIS: Knie-Linklaenge vs. Topografie-Kennzahl (Hm/km bzw. sigma_g).

Eingaben:
  - <results-dir>/energy_results_summary.csv  (Sweep-Ausgabe)
  - selected_sections_features.csv der ZUGEHOERIGEN Auswahl (Hm/km, sigma_g je
    Sektion); Default = kanonischer Run 182750 im Netzgen-Worktree.

Ausgaben (alle in results-dir):
  - energy_relative_to_250m.png/pdf   Sec-5-Hauptdarstellung
  - knee_vs_topography.png/pdf        Hauptergebnis-Scatter
  - knee_analysis.png/pdf             Rohdaten + Spline + Knie (Uebersicht)
  - knee_points.csv                   je Kurve: Knie + Topografie-Merge
  - relative_to_250m.csv              volle Relativ-Tabelle fuer paper_findings
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np
import pandas as pd
from kneed import KneeLocator
from scipy.interpolate import UnivariateSpline

_SCRIPT_DIR = Path(__file__).parent
_NETGEN_DIR = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
DEFAULT_FEATURES_CSV = (_NETGEN_DIR / "data" / "sections_quantile_run_20260706_182750"
                        / "selected_sections_features.csv")

REFERENCE_LINK_LENGTH_M = 250.0  # VECTO-verankerte Kalibrier-Skala
SPLINE_RESOLUTION = 500
LOADING_LINESTYLE = {"loaded": "-", "empty": "--"}
LOADING_MARKER = {"loaded": "o", "empty": "s"}


def _quantile_number(section):
    """'q97' -> 97; 'flat' -> -1 (fuer Sortierung/Farbe)."""
    if section == "flat":
        return -1
    return int(section[1:])


def section_color(section, all_sections):
    """coolwarm nach Quantil-Rang (konsistent zu run_section_energy_analysis.py)."""
    if section == "flat":
        return "#757575"
    quant = sorted((s for s in all_sections if s != "flat"), key=_quantile_number)
    i = quant.index(section)
    return cm.get_cmap("coolwarm")(i / max(1, len(quant) - 1))


def fit_and_find_knee(x, y, smoothing_mult=1.0):
    """UnivariateSpline (k=3) + Kneedle (convex/decreasing) auf dichtem Gitter.

    Rueckgabe (x_dense, y_dense, knee_x, knee_y). knee_* = None, wenn Kneedle
    keinen Knick findet (erwartet fuer sehr flache Sektionen ohne
    Aufloesungs-Sensitivitaet).
    """
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    order = np.argsort(x_arr)
    x_sorted, y_sorted = x_arr[order], y_arr[order]

    s = smoothing_mult * 0.02
    spline = UnivariateSpline(x_sorted, y_sorted, k=3, s=s)
    x_dense = np.linspace(x_sorted.min(), x_sorted.max(), SPLINE_RESOLUTION)
    y_dense = spline(x_dense)

    try:
        kl = KneeLocator(x_dense, y_dense, curve="convex", direction="decreasing",
                         S=1.0, online=True)
        knee_x, knee_y = kl.knee, kl.knee_y
    except Exception:
        knee_x, knee_y = None, None
    return x_dense, y_dense, knee_x, knee_y


def compute_relative(df, sections, loadings):
    """rel(L) = kWh(L)/kWh(250) - 1 je (Sektion, Beladung). Long-Format-DataFrame."""
    rows = []
    for section in sections:
        for loading in loadings:
            sub = df[(df.section == section) & (df.loading == loading)]
            sub = sub.sort_values("max_link_length")
            if sub.empty:
                continue
            ref = sub.loc[sub.max_link_length == REFERENCE_LINK_LENGTH_M, "kWh_per_km"]
            if ref.empty:
                print(f"  WARNING: keine 250-m-Referenz fuer {section}/{loading} — skip")
                continue
            ref_val = float(ref.iloc[0])
            for _, r in sub.iterrows():
                rows.append({
                    "section": section, "loading": loading,
                    "max_link_length": int(r["max_link_length"]),
                    "kWh_per_km": float(r["kWh_per_km"]),
                    "kWh_per_km_ref250": ref_val,
                    "rel_to_250_pct": 100.0 * (float(r["kWh_per_km"]) / ref_val - 1.0),
                })
    return pd.DataFrame(rows)


def plot_relative(rel_df, sections, output_dir):
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), sharey=True)
    for ax, loading in zip(axes, ("empty", "loaded")):
        for section in sections:
            sub = rel_df[(rel_df.section == section) & (rel_df.loading == loading)]
            sub = sub.sort_values("max_link_length")
            if sub.empty:
                continue
            color = section_color(section, sections)
            ax.plot(sub.max_link_length, sub.rel_to_250_pct, marker="o", ms=4,
                    color=color, lw=1.4, label=section)
        ax.axvline(REFERENCE_LINK_LENGTH_M, color="black", ls=":", lw=1, alpha=0.6)
        ax.axhline(0, color="black", lw=0.8, alpha=0.5)
        ax.set_xscale("log")
        xs = sorted(rel_df.max_link_length.unique())
        ax.set_xticks(xs)
        ax.set_xticklabels([str(x) for x in xs], rotation=45, fontsize=8)
        ax.set_xlabel("Max. allowed link length [m]", fontsize=12)
        ax.set_title(loading, fontsize=12)
        ax.grid(True, alpha=0.3, which="both")
    axes[0].set_ylabel("Energy relative to 250 m step [%]", fontsize=12)
    axes[1].legend(loc="upper right", fontsize=7, ncol=2, title="section")
    fig.suptitle("Discretisation sensitivity relative to the 250 m calibration scale",
                 fontsize=13)
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(output_dir / f"energy_relative_to_250m.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def compute_sensitivity(rel_df, feats):
    """Spanne rel%(50 m) - rel%(1000 m) je (Sektion, Beladung) + Hm/km-Merge.

    Diese Spanne ist das robuste Sensitivitaetsmass (wie stark haengt der
    Verbrauch ueberhaupt von der Linklaenge ab). Im Gegensatz zur Knie-POSITION
    skaliert sie klar mit der Topografie.
    """
    rows = []
    for (section, loading), sub in rel_df.groupby(["section", "loading"]):
        if section == "flat":
            hm = 0.0
        elif section in feats.index:
            hm = float(feats.loc[section, "D_plus_per_km"])
        else:
            continue
        r50 = sub.loc[sub.max_link_length == 50, "rel_to_250_pct"]
        r1000 = sub.loc[sub.max_link_length == 1000, "rel_to_250_pct"]
        if r50.empty or r1000.empty:
            continue
        rows.append({"section": section, "loading": loading, "D_plus_per_km": hm,
                     "rel_50m_pct": float(r50.iloc[0]),
                     "rel_1000m_pct": float(r1000.iloc[0]),
                     "span_50_1000_pct": float(r50.iloc[0]) - float(r1000.iloc[0])})
    return pd.DataFrame(rows)


def plot_sensitivity_vs_topography(sens_df, output_dir):
    fig, ax = plt.subplots(1, 1, figsize=(9, 6.5))
    for loading in ("empty", "loaded"):
        sub = sens_df[(sens_df.loading == loading) & (sens_df.section != "flat")]
        sub = sub.sort_values("D_plus_per_km")
        if sub.empty:
            continue
        ax.scatter(sub.D_plus_per_km, sub.span_50_1000_pct,
                   marker=LOADING_MARKER[loading], s=70, zorder=3,
                   edgecolors="black", linewidths=0.6, label=loading)
    # Flat-Kontrolle als Referenzlinie (grade-frei -> ~0)
    flat = sens_df[sens_df.section == "flat"]
    if not flat.empty:
        ax.axhline(flat.span_50_1000_pct.abs().max(), color="grey", ls=":", lw=1,
                   label="flat control (grade-free)")
    ax.set_xlabel("Elevation gain [Hm/km]", fontsize=12)
    ax.set_ylabel("Discretisation span 50 m - 1000 m [%pt]", fontsize=12)
    ax.set_title("Discretisation sensitivity scales with topography", fontsize=13)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=10)
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(output_dir / f"sensitivity_vs_topography.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_knee_vs_topography(knee_df, output_dir, topo_col="D_plus_per_km",
                            topo_label="Elevation gain [Hm/km]"):
    fig, ax = plt.subplots(1, 1, figsize=(9, 6.5))
    for loading in ("empty", "loaded"):
        sub = knee_df[(knee_df.loading == loading) & knee_df.knee_link_length_m.notna()]
        sub = sub.sort_values(topo_col)
        if sub.empty:
            continue
        ax.scatter(sub[topo_col], sub.knee_link_length_m,
                   marker=LOADING_MARKER[loading], s=70, zorder=3,
                   edgecolors="black", linewidths=0.6, label=loading)
        for _, r in sub.iterrows():
            ax.annotate(r["section"], xy=(r[topo_col], r["knee_link_length_m"]),
                        xytext=(4, 4), textcoords="offset points", fontsize=7, alpha=0.7)
    ax.set_xlabel(topo_label, fontsize=12)
    ax.set_ylabel("Knee link length [m]", fontsize=12)
    ax.set_title("Discretisation knee vs. section topography", fontsize=13)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=10, title="loading")
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(output_dir / f"knee_vs_topography.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_knee_overview(curves, sections, output_dir):
    link_lengths = sorted({int(x) for c in curves for x in c["x"]})
    fig, ax = plt.subplots(1, 1, figsize=(13, 7.5))
    for c in curves:
        color = section_color(c["section"], sections)
        ls = LOADING_LINESTYLE[c["loading"]]
        ax.scatter(c["x"], c["y"], color=color, s=18, zorder=3,
                   marker=LOADING_MARKER[c["loading"]], edgecolors="white", linewidths=0.4)
        ax.plot(c["x_dense"], c["y_dense"], color=color, linestyle=ls, lw=1.3, alpha=0.8,
                label=f"{c['section']} ({c['loading']})")
        if c["knee_x"] is not None:
            ax.scatter([c["knee_x"]], [c["knee_y"]], color=color, marker="*",
                       s=150, edgecolors="black", linewidths=0.6, zorder=5)
    ax.set_xscale("log")
    ax.set_xticks(link_lengths)
    ax.set_xticklabels([str(x) for x in link_lengths], rotation=45, fontsize=8)
    ax.set_xlabel("Max. allowed link length [m]", fontsize=12)
    ax.set_ylabel("Energy consumption [kWh/km]", fontsize=12)
    ax.set_title("Raw + spline-smoothed curves with Kneedle knees (20 sections)", fontsize=13)
    ax.grid(True, alpha=0.3, which="both")
    ax.legend(loc="upper right", fontsize=6, ncol=3)
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(output_dir / f"knee_analysis.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="20-Sektionen-Sweep-Auswertung (relativ + Knie).")
    parser.add_argument("--results-dir", type=str, required=True,
                        help="Ordner mit energy_results_summary.csv (= variants-dir des Sweeps).")
    parser.add_argument("--features-csv", type=str, default=str(DEFAULT_FEATURES_CSV),
                        help="selected_sections_features.csv der zugehoerigen Auswahl (Hm/km, sigma_g).")
    parser.add_argument("--smoothing", type=float, default=1.0,
                        help="Spline-Glaettungsmultiplikator (>1 glatter).")
    parser.add_argument("--max-link-length", type=float, default=700.0,
                        help="Linklaengen-Achse/Knee auf diesen Wert kappen [m]; Default 700.")
    parser.add_argument("--include-flat", action="store_true",
                        help="Flat-Referenz in Relativ-Plot einbeziehen (hat keinen Knie).")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    df = pd.read_csv(results_dir / "energy_results_summary.csv")
    print(f"Loaded {len(df)} rows from {results_dir / 'energy_results_summary.csv'}")

    feats = pd.read_csv(args.features_csv).set_index("section")

    # Sektionen dynamisch aus der CSV (ausser flat, sofern nicht gewuenscht)
    sections = sorted((s for s in df.section.unique() if s != "flat"), key=_quantile_number)
    sections_for_rel = (["flat"] + sections) if args.include_flat and "flat" in df.section.unique() else sections
    loadings = ["empty", "loaded"]

    # --- Relative Darstellung (volle Leiter, ungekappt) ---
    # Flat immer mitrechnen (Kontrolle fuer Sensitivitaet), aber nur bei
    # --include-flat mitplotten (sonst ueberladen).
    rel_sections = sections_for_rel if "flat" in sections_for_rel else (sections_for_rel + ["flat"])
    rel_df = compute_relative(df, rel_sections, loadings)
    rel_df.to_csv(results_dir / "relative_to_250m.csv", index=False)
    plot_relative(rel_df, sections_for_rel, results_dir)

    # --- Knie je (Sektion, Beladung) auf gekappter Leiter ---
    df_knee = df[df.max_link_length <= args.max_link_length].copy()
    curves, knee_rows = [], []
    for section in sections:
        for loading in loadings:
            sub = df_knee[(df_knee.section == section) & (df_knee.loading == loading)]
            sub = sub.sort_values("max_link_length")
            if sub.empty:
                continue
            x, y = sub.max_link_length.values, sub.kWh_per_km.values
            x_dense, y_dense, knee_x, knee_y = fit_and_find_knee(x, y, args.smoothing)
            curves.append(dict(section=section, loading=loading, x=x, y=y,
                               x_dense=x_dense, y_dense=y_dense, knee_x=knee_x, knee_y=knee_y))
            knee_rows.append({
                "section": section, "loading": loading,
                "knee_link_length_m": float(knee_x) if knee_x is not None else None,
                "knee_kWh_per_km": float(knee_y) if knee_y is not None else None,
                "D_plus_per_km": float(feats.loc[section, "D_plus_per_km"]) if section in feats.index else None,
                "sigma_g": float(feats.loc[section, "sigma_g"]) if section in feats.index else None,
                "g_abs_mean": float(feats.loc[section, "g_abs_mean"]) if section in feats.index else None,
            })

    knee_df = pd.DataFrame(knee_rows)
    knee_df.to_csv(results_dir / "knee_points.csv", index=False)

    sens_df = compute_sensitivity(rel_df, feats)
    sens_df.to_csv(results_dir / "sensitivity_vs_topography.csv", index=False)

    plot_sensitivity_vs_topography(sens_df, results_dir)
    plot_knee_vs_topography(knee_df, results_dir)
    plot_knee_overview(curves, sections, results_dir)

    # --- Konsolen-Zusammenfassung fuer paper_findings.md ---
    print("\n=== Relative Abweichung zur 250-m-Stufe [%] (Auszug: 50 m, 100 m, 1000 m) ===")
    pivot = rel_df.pivot_table(index=["section", "loading"], columns="max_link_length",
                               values="rel_to_250_pct")
    cols = [c for c in (50, 100, 1000) if c in pivot.columns]
    print(pivot[cols].round(2).to_string())

    print("\n=== Kniepunkte + Topografie ===")
    print(knee_df.round({"knee_link_length_m": 0, "knee_kWh_per_km": 3,
                         "D_plus_per_km": 2, "sigma_g": 4, "g_abs_mean": 4}).to_string(index=False))

    n_knee = knee_df.knee_link_length_m.notna().sum()
    print(f"\n{n_knee}/{len(knee_df)} Kurven mit detektiertem Knie.")
    print(f"\nOutputs in: {results_dir}")


if __name__ == "__main__":
    main()
