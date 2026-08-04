"""Erzeugt Fig. 3 (sensitivity_and_knee) und Fig. 4 (decomposition_
validity) des IEEE-TTE-Papers als native TikZ/pgfplots-Snippets.

Repliziert die matplotlib-V3-Figuren (knee_point_analysis.py bzw.
grade_decomposition.py) 1:1 aus denselben CSVs; Ausgabe versioniert
(_V<N>, nie ueberschreiben). Benoetigt im Paper-Preamble:
\\usepgfplotslibrary{groupplots}.

Aufruf:  ../../.venv/Scripts/python plot_tikz_fig34.py
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data" / "section_variants_20260706_20q"
# kanonischer Auswahl-Run (182750, s. paper_findings Sec 13.2)
SELEKTION = (Path(__file__).parents[2].parent / "MATSim-MPM-netgen" /
             "python" / "network-generation" / "data" /
             "sections_quantile_run_20260706_182750")
COL_SENS, COL_KNEE = "C0392B", "21618C"


def naechste_version(muster, endung=".tex"):
    v = 1
    while (DATA / f"{muster}_V{v}{endung}").exists():
        v += 1
    return v


def paare(xs, ys, dx=2, dy=2, je_zeile=5):
    p = [f"({x:.{dx}f},{y:.{dy}f})" for x, y in zip(xs, ys)]
    return "\n".join(" ".join(p[i:i + je_zeile])
                     for i in range(0, len(p), je_zeile))


def schreibe_fig3(df, knee, cand, pfad):
    """Dreizeilige Kernfigur, Variante A (2026-08-04): Histogramm /
    relative Verbrauchs-Aufloesungs-Kurven (log-x, Knie-IQR-Band) /
    Spanne vs. Steigung mit Fit-Gerade. Empty-Knees (Kneedle auf fast
    flacher Kurve = Rauschen) sind bewusst nicht mehr enthalten."""
    kl = knee[knee.loading == "loaded"].dropna(
        subset=["knee_link_length_m"]).sort_values("g_abs_mean")
    gx = kl.g_abs_mean.values * 100.0
    secs = list(kl.section.values)
    x_max = gx.max() + 0.3
    laengen = sorted(df.max_link_length.unique())

    def cons(sec, loading, L):
        v = df[(df.section == sec) & (df.loading == loading) &
               (df.max_link_length == L)].kWh_per_km
        return float(v.iloc[0])

    def rel_kurve(sec):
        e250 = cons(sec, "loaded", 250)
        return [(L, (cons(sec, "loaded", L) / e250 - 1.0) * 100.0)
                for L in laengen]

    def spanne(sec, loading):
        return ((cons(sec, loading, 50) - cons(sec, loading, 1000))
                / cons(sec, loading, 250) * 100.0)

    kq1, kq3 = kl.knee_link_length_m.quantile([0.25, 0.75])
    alle_rel = [v for s in secs for _, v in rel_kurve(s)]
    y2min, y2max = min(alle_rel) - 1.5, max(alle_rel) + 1.5

    # Highlights: flachste / mediane / steilste Sektion
    hi = {secs[0]: ("fighia", "flattest"),
          secs[len(secs) // 2]: ("fighib", "median"),
          secs[-1]: ("figsens", "steepest")}

    def kurve_tex(sec, stil, forget=False):
        pts = paare(*zip(*rel_kurve(sec)), dx=0, dy=2, je_zeile=6)
        f = ", forget plot" if forget else ""
        return f"\\addplot[{stil}, no marks{f}] coordinates {{\n{pts}\n}};"

    grau = "\n".join(kurve_tex(s, "black!35, line width=0.5pt", True)
                     for s in secs if s not in hi)
    bunt = "\n".join(
        kurve_tex(s, f"{hi[s][0]}, line width=1.1pt") +
        f"\n\\addlegendentry{{{hi[s][1]} ({g:.1f}\\,\\%)}}"
        for s, g in zip(secs, gx) if s in hi)

    # Histogramm wie matplotlib: Binbreite 0,2 %
    kanten = np.arange(0, cand.max() + 0.2, 0.2)
    zaehl, _ = np.histogram(cand, bins=kanten)
    hmax = float(zaehl.max()) * 1.08
    hist = paare(kanten[:-1], zaehl, dx=1, dy=0, je_zeile=6)
    hist += f"\n({kanten[-1]:.1f},{zaehl[-1]:.0f})"  # ybar interval: Endkante
    rug = "\n".join(
        f"\\draw[figknee, line width=0.5pt, opacity=0.7] "
        f"(axis cs:{g:.2f},0) -- (axis cs:{g:.2f},{0.14 * hmax:.1f});"
        for g in gx)

    sp_l = paare(gx, [spanne(s, "loaded") for s in secs], dy=1)
    sp_e = paare(gx, [spanne(s, "empty") for s in secs], dy=1)

    tex = f"""% Auto-generiert von plot_tikz_fig34.py aus
% energy_results_summary.csv / knee_points.csv /
% candidate_paths_features.csv (Run 182750). Nicht von Hand editieren.
% Variante A (2026-08-04): Kurven-Panel + Spannen-Panel, Empty-Knees raus.
\\begin{{tikzpicture}}
\\definecolor{{figsens}}{{HTML}}{{{COL_SENS}}}
\\definecolor{{figknee}}{{HTML}}{{{COL_KNEE}}}
\\definecolor{{fighia}}{{HTML}}{{1E8449}}
\\definecolor{{fighib}}{{HTML}}{{B7950B}}
\\begin{{groupplot}}[group style={{group size=1 by 3, vertical sep=26pt}},
  width=0.84\\columnwidth, scale only axis,
  tick label style={{font=\\scriptsize}},
  label style={{font=\\footnotesize}},
  legend style={{font=\\scriptsize, draw=none, fill=white,
    fill opacity=0.85, text opacity=1}},
  legend cell align=left, grid=major, grid style={{black!12}},
]
% --- Zeile 1: Netz-Steigungsverteilung (1707 Kandidaten) + Sektions-Rug
\\nextgroupplot[height=1.5cm, ylabel={{Route count}},
  xmin=-0.15, xmax={x_max:.2f}, xlabel={{Mean absolute grade [\\%]}},
  ymin=0, ymax={hmax:.0f}, ytick={{0,400,800}}, grid=none]
\\addplot[ybar interval, fill=black!30, draw=white, line width=0.2pt]
  coordinates {{
{hist}
}};
\\draw[densely dotted, black] (axis cs:1,0) -- (axis cs:1,{hmax:.0f});
{rug}
% --- Zeile 2: relative Verbrauchs-Aufloesungs-Kurven (loaded)
\\nextgroupplot[height=3.3cm, xmode=log, xmin=47, xmax=1060,
  xtick={{50,100,250,500,1000}}, xticklabels={{50,100,250,500,1000}},
  minor xtick={{}}, xlabel={{Maximum link length [m]}},
  ylabel={{Deviation from 250\\,m grid [\\%]}},
  ymin={y2min:.1f}, ymax={y2max:.1f},
  legend style={{at={{(0.02,0.03)}}, anchor=south west}}]
\\fill[figknee, fill opacity=0.12]
  (axis cs:{kq1:.0f},{y2min:.1f}) rectangle (axis cs:{kq3:.0f},{y2max:.1f});
\\addlegendimage{{area legend, fill=figknee!25, draw=none}}
\\addlegendentry{{loaded knee IQR ({kq1:.0f}--{kq3:.0f}\\,m)}}
\\addplot[densely dotted, black, no marks, line width=0.7pt]
  coordinates {{(250,{y2min:.1f}) (250,{y2max:.1f})}};
\\addlegendentry{{250\\,m calibration scale}}
{grau}
{bunt}
\\addplot[black, densely dashed, line width=0.8pt, no marks] coordinates {{
{paare(*zip(*[(L, (cons("flat", "loaded", L) / cons("flat", "loaded", 250) - 1.0) * 100.0) for L in laengen]), dx=0, dy=2, je_zeile=6)}
}};
\\addlegendentry{{flat control}}
% --- Zeile 3: Spanne 50-1000 m vs. Steigung, Fit 4.0*g+9.8
\\nextgroupplot[height=2.4cm, xmin=-0.15, xmax={x_max:.2f},
  ymin=0, ymax=36, xlabel={{Mean absolute grade [\\%]}},
  ylabel={{50--1000\\,m span [\\%]}},
  legend style={{at={{(0.02,0.97)}}, anchor=north west}}]
\\addplot[figsens, densely dashed, line width=0.7pt, no marks]
  coordinates {{(0,9.8) ({x_max:.2f},{4.0 * x_max + 9.8:.1f})}};
\\addlegendentry{{fit $4.0\\cdot\\overline{{|g|}}+9.8$}}
\\addplot[only marks, mark=diamond*, mark size=2.2pt, color=black,
  fill=figsens] coordinates {{
{sp_l}
}};
\\addlegendentry{{loaded}}
\\addplot[only marks, mark=x, mark size=2.2pt, color=black!45,
  line width=0.8pt] coordinates {{
{sp_e}
}};
\\addlegendentry{{empty}}
\\end{{groupplot}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def schreibe_fig4(dec, pfad):
    """Validitaets-Scatter dE_sim vs. dE_stat mit 45-Grad-Linie."""
    ne = dec[dec.max_link_length != 250]
    lim = max(ne.dE_sim_rel250_pct.abs().max(),
              ne.dE_stat_rel250_pct.abs().max()) * 1.08
    ld = ne[ne.loading == "loaded"]
    em = ne[ne.loading == "empty"]
    tex = f"""% Auto-generiert von plot_tikz_fig34.py aus
% grade_decomposition.csv. Nicht von Hand editieren.
\\begin{{tikzpicture}}
\\definecolor{{figsens}}{{HTML}}{{{COL_SENS}}}
\\definecolor{{figknee}}{{HTML}}{{{COL_KNEE}}}
\\begin{{axis}}[
  width=0.86\\columnwidth, scale only axis, axis equal image,
  xmin={-lim:.1f}, xmax={lim:.1f}, ymin={-lim:.1f}, ymax={lim:.1f},
  xlabel={{Simulated energy change vs.\\ 250\\,m grid [\\%]}},
  ylabel={{Static model change vs.\\ 250\\,m grid [\\%]}},
  tick label style={{font=\\scriptsize}},
  label style={{font=\\footnotesize}},
  legend style={{at={{(0.02,0.98)}}, anchor=north west,
    font=\\scriptsize, draw=none, fill=white, fill opacity=0.85,
    text opacity=1}}, legend cell align=left,
  grid=major, grid style={{black!12}},
]
\\addplot[dotted, black, no marks, line width=0.4pt, forget plot]
  coordinates {{({-lim:.1f},{-lim:.1f}) ({lim:.1f},{lim:.1f})}};
\\addplot[only marks, mark=o, mark size=1.1pt, color=figsens,
  line width=0.5pt] coordinates {{
{paare(ld.dE_sim_rel250_pct.values, ld.dE_stat_rel250_pct.values)}
}};
\\addlegendentry{{loaded}}
\\addplot[only marks, mark=square, mark size=1.0pt, color=figknee,
  line width=0.5pt] coordinates {{
{paare(em.dE_sim_rel250_pct.values, em.dE_stat_rel250_pct.values)}
}};
\\addlegendentry{{empty}}
\\end{{axis}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection-dir", default=str(SELEKTION))
    args = ap.parse_args()
    sel = Path(args.selection_dir)

    df = pd.read_csv(DATA / "energy_results_summary.csv")
    knee = pd.read_csv(DATA / "knee_points.csv")
    dec = pd.read_csv(DATA / "grade_decomposition.csv")
    cand = (pd.read_csv(sel / "candidate_paths_features.csv")
            ["g_abs_mean"] * 100.0).values

    v3 = naechste_version("sensitivity_knee_tikz")
    out3 = DATA / f"sensitivity_knee_tikz_V{v3}.tex"
    schreibe_fig3(df, knee, cand, out3)
    v4 = naechste_version("decomposition_validity_tikz")
    out4 = DATA / f"decomposition_validity_tikz_V{v4}.tex"
    schreibe_fig4(dec, out4)
    print(f"TikZ: {out3.name}, {out4.name}")


if __name__ == "__main__":
    main()
