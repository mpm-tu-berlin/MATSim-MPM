# -*- coding: utf-8 -*-
"""
Knee-point analysis on the energy_results_summary.csv from
run_section_energy_analysis.py.

Pro (section, loading) wird die kWh/km-vs-Linklaenge-Kurve mittels
scipy UnivariateSpline (k=3, default smoothing factor s) interpoliert.
Auf dem Spline laeuft der Kneedle-Algorithmus (Satopaa et al. 2011)
mit curve='convex' / direction='decreasing' und liefert den Kniepunkt
(Linklaenge in m, kWh/km am Knie).

Ausgabe:
  - knee_points.csv  ->  je Kurve eine Zeile mit Knee-Koordinaten
  - knee_analysis.png/pdf  ->  Plot mit Rohdaten, Splines und Knees
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from kneed import KneeLocator
from scipy.interpolate import UnivariateSpline

# Plot styling matches run_section_energy_analysis.py
SECTION_COLORS = {"flat": "#757575", "q75": "#FF9800", "q97": "#E53935"}
SECTION_LABELS = {"flat": "Flat", "q75": "Q75 (medium)", "q97": "Q97 (hilly)"}
LOADING_LINESTYLE = {"loaded": "-", "empty": "--"}
LOADING_MARKER = {"loaded": "o", "empty": "s"}

SPLINE_RESOLUTION = 500  # evaluation points on the spline for Kneedle


def fit_and_find_knee(x, y, smoothing_mult=1.0):
    """Fit UnivariateSpline and find Kneedle knee on a dense evaluation grid.

    smoothing_mult scales the auto smoothing factor: scipy's default with no
    weights uses s ~ len(x). We pass s = smoothing_mult * len(x). Larger values
    => smoother curve, may shift / blur the knee slightly. Returns
    (spline_eval_x, spline_eval_y, knee_x, knee_y).
    Kneedle assumes convex-decreasing for these convergence curves.
    """
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    order = np.argsort(x_arr)
    x_sorted, y_sorted = x_arr[order], y_arr[order]

    # Smoothing budget: s = mult * 0.02 (sum of squared residuals).
    # At mult=1, RMS residual ~ sqrt(0.02/22) ~ 0.03 kWh/km per point — a
    # gentle smoothing that follows the trend without hugging every point.
    # mult >1 => stronger smoothing (tames endpoint kinks); mult <1 => tighter.
    s = smoothing_mult * 0.02
    spline = UnivariateSpline(x_sorted, y_sorted, k=3, s=s)
    x_dense = np.linspace(x_sorted.min(), x_sorted.max(), SPLINE_RESOLUTION)
    y_dense = spline(x_dense)

    # Kneedle on the smoothed curve
    try:
        kl = KneeLocator(
            x_dense, y_dense,
            curve="convex", direction="decreasing",
            S=1.0, online=True,
        )
        knee_x, knee_y = kl.knee, kl.knee_y
    except Exception:
        knee_x, knee_y = None, None

    return x_dense, y_dense, knee_x, knee_y


def main():
    parser = argparse.ArgumentParser(description="Knee-point analysis of convergence curves.")
    parser.add_argument("--results-dir", type=str,
                        default=r"C:/Users/Admin/PycharmProjects/MATSim-MPM/python/calibration/data/Real-world topography",
                        help="Directory containing energy_results_summary.csv (default = realworld topography dir).")
    parser.add_argument("--include-flat", action="store_true",
                        help="Include flat reference in plot/output (default: skip — has no knee).")
    parser.add_argument("--smoothing", type=float, default=1.0,
                        help="Spline smoothing multiplier (default 1.0). >1 = smoother, <1 = closer to raw.")
    parser.add_argument("--max-link-length", type=float, default=700.0,
                        help="Cap link-length axis at this value [m]. Data beyond is excluded from "
                             "spline fit, kneedle, and plot. Default 700 m (Q75-loaded tail-down "
                             "kink starts at ~800 m). Use a larger value to include the full range.")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    csv_path = results_dir / "energy_results_summary.csv"
    df = pd.read_csv(csv_path)
    print(f"Loaded {len(df)} rows from {csv_path}")

    if args.max_link_length is not None:
        before = len(df)
        df = df[df["max_link_length"] <= args.max_link_length].copy()
        print(f"Capped at max_link_length <= {args.max_link_length}: "
              f"{len(df)} rows kept ({before - len(df)} dropped)")

    sections_to_analyze = ["flat", "q75", "q97"] if args.include_flat else ["q75", "q97"]
    loadings = ["empty", "loaded"]

    # Compute knee points ONCE (scale-invariant), then plot on both x-scales.
    curves = []
    for section in sections_to_analyze:
        for loading in loadings:
            sub = df[(df.section == section) & (df.loading == loading)].sort_values("max_link_length")
            if sub.empty:
                continue
            x = sub["max_link_length"].values
            y = sub["kWh_per_km"].values
            x_dense, y_dense, knee_x, knee_y = fit_and_find_knee(x, y, smoothing_mult=args.smoothing)
            curves.append({
                "section": section, "loading": loading,
                "x": x, "y": y, "x_dense": x_dense, "y_dense": y_dense,
                "knee_x": knee_x, "knee_y": knee_y,
            })

    link_lengths = sorted(df["max_link_length"].unique())
    saved_paths = []

    for scale, suffix in [("log", ""), ("linear", "_linear")]:
        fig, ax = plt.subplots(1, 1, figsize=(12, 7))

        for c in curves:
            color = SECTION_COLORS[c["section"]]
            ls = LOADING_LINESTYLE[c["loading"]]
            mk = LOADING_MARKER[c["loading"]]
            label = f"{SECTION_LABELS[c['section']]} ({c['loading']})"

            ax.scatter(c["x"], c["y"], color=color, marker=mk, s=30, zorder=3,
                       edgecolors="white", linewidths=0.6)
            ax.plot(c["x_dense"], c["y_dense"], color=color, linestyle=ls,
                    linewidth=1.6, alpha=0.85, label=label)

            if c["knee_x"] is not None:
                ax.scatter([c["knee_x"]], [c["knee_y"]], color=color, marker="*",
                           s=180, edgecolors="black", linewidths=0.7, zorder=5)
                ax.annotate(
                    f"  {c['knee_x']:.0f} m\n  {c['knee_y']:.3f} kWh/km",
                    xy=(c["knee_x"], c["knee_y"]), xytext=(8, -4),
                    textcoords="offset points",
                    fontsize=8, color=color, fontweight="bold",
                )

        if scale == "log":
            ax.set_xscale("log")
            ax.set_xticks(link_lengths)
            ax.set_xticklabels([str(x) for x in link_lengths], rotation=45, fontsize=8)
        ax.set_xlabel("Max. allowed link length [m]", fontsize=12)
        ax.set_ylabel("Energy consumption [kWh/km]", fontsize=12)
        ax.set_title(f"Knee-point analysis: spline-smoothed convergence + Kneedle ({scale} scale)",
                     fontsize=13)
        ax.grid(True, alpha=0.3, which="both")
        ax.legend(loc="best", fontsize=9)
        fig.tight_layout()

        pdf_path = results_dir / f"knee_analysis{suffix}.pdf"
        png_path = results_dir / f"knee_analysis{suffix}.png"
        fig.savefig(pdf_path, dpi=300, bbox_inches="tight")
        fig.savefig(png_path, dpi=300, bbox_inches="tight")
        plt.close(fig)
        saved_paths.extend([pdf_path, png_path])

    knee_rows = [
        {
            "section": c["section"], "loading": c["loading"],
            "knee_link_length_m": float(c["knee_x"]) if c["knee_x"] is not None else None,
            "knee_kWh_per_km":    float(c["knee_y"]) if c["knee_y"] is not None else None,
        }
        for c in curves
    ]
    knee_df = pd.DataFrame(knee_rows)
    knee_csv = results_dir / "knee_points.csv"
    knee_df.to_csv(knee_csv, index=False)

    print(f"\nKnee points:")
    print(knee_df.to_string(index=False))
    print(f"\nSaved:")
    for p in [knee_csv, *saved_paths]:
        print(f"  {p}")


if __name__ == "__main__":
    main()