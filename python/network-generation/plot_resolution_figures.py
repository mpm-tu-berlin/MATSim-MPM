"""Erzeugt die Paper-Figuren "Netz-Overlay" und "Hoehenprofil-Vergleich"
fuer IEEE-TTE (Sec. II) aus den V2-Sektionsvarianten.

Vergleicht die feinste (50 m) mit der groebsten (1000 m) Aufloesungsstufe
derselben Sektion. Ausgabe versioniert (_V<N>, nie ueberschreiben) nach
data/section_variants_20260706_20q/.

Aufruf:  ../../.venv/Scripts/python plot_resolution_figures.py [--section q50]
"""
import argparse
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

DATA = Path(__file__).parent / "data" / "section_variants_20260706_20q"
FEIN, GROB = "50m", "1000m"


def lade_kette(pfad):
    """Laedt ein Sektions-Netz und ordnet die Knoten als Wegkette.

    Links sind bidirektional (2 je Kante); die Kette wird ungerichtet
    ueber die Knotengrade rekonstruiert (Enden = Grad 1).
    Rueckgabe: kumulative Distanz [m], xyz-Array (n, 3).
    """
    with gzip.open(pfad, "rb") as f:
        root = ET.parse(f).getroot()
    nodes = {
        n.get("id"): (float(n.get("x")), float(n.get("y")), float(n.get("z")))
        for n in root.iter("node")
    }
    kanten = {}  # ungerichtet: (min,max) -> Laenge
    nachbarn = {}
    for l in root.iter("link"):
        a, b, ln = l.get("from"), l.get("to"), float(l.get("length"))
        kanten[(min(a, b), max(a, b))] = ln
        nachbarn.setdefault(a, set()).add(b)
        nachbarn.setdefault(b, set()).add(a)
    enden = [n for n, nb in nachbarn.items() if len(nb) == 1]
    if len(enden) != 2:
        raise ValueError(f"{pfad.name}: keine reine Kette ({len(enden)} Enden)")
    # deterministische Richtung: Start = Ende mit kleinerer y-Koordinate
    start = min(enden, key=lambda n: (nodes[n][1], nodes[n][0]))
    reihe, dist = [start], [0.0]
    vorher, aktuell = None, start
    while True:
        nxt = [n for n in nachbarn[aktuell] if n != vorher]
        if not nxt:
            break
        n = nxt[0]
        dist.append(dist[-1] + kanten[(min(aktuell, n), max(aktuell, n))])
        reihe.append(n)
        vorher, aktuell = aktuell, n
    xyz = np.array([nodes[i] for i in reihe])
    return np.array(dist), xyz


def fenster_waehlen(d_f, z_f, d_g, z_g, laenge_m):
    """Waehlt das Fenster mit maximaler mittlerer |Fein-Grob|-Abweichung.

    Dort ist der Glaettungseffekt der Vergroeberung am sichtbarsten
    (monotone Steilstuecke haben hohe Varianz, aber kaum Abweichung).
    """
    z_g_auf_f = np.interp(d_f, d_g, z_g)
    abw = np.abs(z_f - z_g_auf_f)
    beste, d0_best = -1.0, 0.0
    for d0 in np.arange(0, d_f[-1] - laenge_m, 250.0):
        maske = (d_f >= d0) & (d_f <= d0 + laenge_m)
        if maske.sum() < 4:
            continue
        score = float(np.mean(abw[maske]))
        if score > beste:
            beste, d0_best = score, d0
    return d0_best


def naechste_version(muster, endung=".pdf"):
    """Erste freie Versionsnummer fuer data/<muster>_V<N><endung>."""
    v = 1
    while (DATA / f"{muster}_V{v}{endung}").exists():
        v += 1
    return v


def koord_zeilen(xs, ys, dezimal=1, je_zeile=4):
    """Formatiert Koordinatenpaare fuer pgfplots, mehrere je Zeile."""
    paare = [f"({x:.{dezimal}f},{y:.{dezimal}f})" for x, y in zip(xs, ys)]
    zeilen = [" ".join(paare[i:i + je_zeile])
              for i in range(0, len(paare), je_zeile)]
    return "\n".join(zeilen)


def schreibe_tikz_karte(p_f, p_g, n_f, n_g, pfad, sektion, karte_km):
    """Netz-Overlay als pgfplots-Snippet (Vektor-Qualitaet im Paper)."""
    xs = np.concatenate([p_f[:, 0], p_g[:, 0]])
    ys = np.concatenate([p_f[:, 1], p_g[:, 1]])
    rand = 0.06 * max(np.ptp(xs), np.ptp(ys))
    bx, by = xs.min(), ys.min() - 1.6 * rand
    tex = f"""% Auto-generiert von plot_resolution_figures.py
% (Sektion {sektion}, {karte_km:.0f}-km-Ausschnitt, PCA-rotiert:
% Hauptachse horizontal, reine Rotation, Massstab bleibt gueltig).
% Nicht von Hand editieren; neue Variante = Skript neu laufen lassen.
\\begin{{tikzpicture}}
\\begin{{axis}}[
  width=\\columnwidth, scale only axis, hide axis, axis equal image,
  enlargelimits=false, clip=false,
  xmin={xs.min() - rand:.1f}, xmax={xs.max() + rand:.1f},
  ymin={ys.min() - 2.6 * rand:.1f}, ymax={ys.max() + rand:.1f},
  legend columns=2,
  legend style={{at={{(0.5,1.02)}}, anchor=south, font=\\footnotesize,
    draw=none, /tikz/every even column/.append style={{column sep=8pt}}}},
]
\\addplot[color=black!55, line width=0.5pt, mark=*, mark size=0.8pt,
  mark options={{fill=black!55, draw=black!55}}] coordinates {{
{koord_zeilen(p_f[:, 0], p_f[:, 1])}
}};
\\addlegendentry{{max.\\ 50\\,m ({n_f} nodes)}}
\\addplot[color=black, line width=1.2pt, mark=*, mark size=1.8pt]
  coordinates {{
{koord_zeilen(p_g[:, 0], p_g[:, 1])}
}};
\\addlegendentry{{max.\\ 1000\\,m ({n_g} nodes)}}
\\draw[black, line width=1pt]
  (axis cs:{bx:.1f},{by:.1f}) -- (axis cs:{bx + 1000:.1f},{by:.1f})
  node[midway, above, font=\\scriptsize] {{1\\,km}};
\\end{{axis}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def schreibe_tikz_profil(d_f, z_f, d_g, z_g, pfad, sektion, fen_km):
    """Hoehenprofil-Vergleich als pgfplots-Snippet."""
    zmin = min(z_f.min(), z_g.min())
    zmax = max(z_f.max(), z_g.max())
    zr = 0.08 * (zmax - zmin)
    tex = f"""% Auto-generiert von plot_resolution_figures.py
% (Sektion {sektion}, {fen_km:.0f}-km-Fenster mit maximaler
% Fein-grob-Abweichung). Nicht von Hand editieren.
\\begin{{tikzpicture}}
\\begin{{axis}}[
  width=\\columnwidth, height=4.2cm,
  xlabel={{Distance (km)}}, ylabel={{Elevation (m a.s.l.)}},
  xmin=0, xmax={fen_km:.1f},
  ymin={zmin - zr:.0f}, ymax={zmax + zr:.0f},
  grid=major, grid style={{gray!30}},
  tick label style={{font=\\footnotesize}},
  label style={{font=\\footnotesize}},
  legend style={{at={{(0.02,0.98)}}, anchor=north west,
    font=\\footnotesize, draw=none}},
]
\\addplot[color=black!55, line width=0.6pt] coordinates {{
{koord_zeilen(d_f, z_f, dezimal=3)}
}};
\\addlegendentry{{max.\\ 50\\,m}}
\\addplot[color=black, line width=1.2pt] coordinates {{
{koord_zeilen(d_g, z_g, dezimal=3)}
}};
\\addlegendentry{{max.\\ 1000\\,m}}
\\end{{axis}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="q50")
    ap.add_argument("--fenster-km", type=float, default=3.0)
    ap.add_argument("--karte-km", type=float, default=8.0)
    args = ap.parse_args()

    d_f, xyz_f = lade_kette(DATA / f"section_{args.section}_{FEIN}.xml.gz")
    d_g, xyz_g = lade_kette(DATA / f"section_{args.section}_{GROB}.xml.gz")
    # gleiche Richtung sicherstellen (gleicher Startpunkt beider Ketten)
    if np.hypot(*(xyz_f[0, :2] - xyz_g[0, :2])) > np.hypot(*(xyz_f[0, :2] - xyz_g[-1, :2])):
        d_g, xyz_g = d_g[-1] - d_g[::-1], xyz_g[::-1]

    fen = args.fenster_km * 1000
    d0 = fenster_waehlen(d_f, xyz_f[:, 2], d_g, xyz_g[:, 2], fen)
    karte0 = max(0.0, d0 + fen / 2 - args.karte_km * 500)

    grau, schwarz = "0.45", "black"

    # --- Figur 1: Netz-Overlay (Karte) --------------------------------
    mk = (d_f >= karte0) & (d_f <= karte0 + args.karte_km * 1000)
    mg = (d_g >= karte0) & (d_g <= karte0 + args.karte_km * 1000)
    n_f, n_g = int(mk.sum()), int(mg.sum())
    # Route horizontal legen (Hauptachse via SVD): reine Rotation um den
    # Fenster-Schwerpunkt, Distanzen/Massstab bleiben gueltig. Sonst
    # verlaeuft der Abschnitt fast vertikal und die Spaltenbreite ist
    # verschenkt (Feedback 2026-07-08).
    pts = np.concatenate([xyz_f[mk, :2], xyz_g[mg, :2]])
    mitte = pts.mean(axis=0)
    _, _, vt = np.linalg.svd(pts - mitte, full_matrices=False)
    p_f = (xyz_f[mk, :2] - mitte) @ vt.T
    p_g = (xyz_g[mg, :2] - mitte) @ vt.T
    if p_f[0, 0] > p_f[-1, 0]:  # Start links; 180-Grad-Drehung, kein Spiegeln
        p_f, p_g = -p_f, -p_g
    # Figurhoehe an die gedrehte Ausdehnung koppeln (equal aspect)
    xr, yr = np.ptp(pts @ vt.T, axis=0)
    hoehe = float(np.clip(3.5 * (yr / xr) + 0.75, 1.1, 2.2))
    fig, ax = plt.subplots(figsize=(3.5, hoehe))
    ax.plot(p_f[:, 0], p_f[:, 1], "-", color=grau, lw=0.8,
            marker="o", ms=1.8, mfc=grau, mec=grau,
            label=f"max. {FEIN} ({n_f} nodes)")
    ax.plot(p_g[:, 0], p_g[:, 1], "-", color=schwarz, lw=1.8,
            marker="o", ms=4.5, mfc=schwarz,
            label=f"max. {GROB} ({n_g} nodes)")
    ax.set_aspect("equal")
    ax.set_axis_off()
    # Raender + Massstabsbalken 1 km unten links, Legende oberhalb der Achse
    xs, ys = np.concatenate([p_f[:, 0], p_g[:, 0]]), \
        np.concatenate([p_f[:, 1], p_g[:, 1]])
    rand = 0.06 * max(np.ptp(xs), np.ptp(ys))
    ax.set_xlim(xs.min() - rand, xs.max() + rand)
    ax.set_ylim(ys.min() - 2.5 * rand, ys.max() + rand)
    bx, by = xs.min(), ys.min() - 1.6 * rand
    ax.plot([bx, bx + 1000], [by, by], color=schwarz, lw=1.5)
    ax.annotate("1 km", (bx + 500, by), textcoords="offset points",
                xytext=(0, 4), ha="center", fontsize=7)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=2,
              fontsize=7, frameon=False)
    v = naechste_version("network_overlay_paper")
    out1 = DATA / f"network_overlay_paper_V{v}.pdf"
    fig.savefig(out1, bbox_inches="tight")
    fig.savefig(out1.with_suffix(".png"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    vt1 = naechste_version("network_overlay_tikz", ".tex")
    out1t = DATA / f"network_overlay_tikz_V{vt1}.tex"
    schreibe_tikz_karte(p_f, p_g, n_f, n_g, out1t, args.section,
                        args.karte_km)

    # --- Figur 2: Hoehenprofile ---------------------------------------
    mpf = (d_f >= d0) & (d_f <= d0 + fen)
    mpg = (d_g >= d0 - 500) & (d_g <= d0 + fen + 500)  # Rand fuer Linienzug
    fig, ax = plt.subplots(figsize=(3.5, 1.9))
    ax.plot((d_f[mpf] - d0) / 1000, xyz_f[mpf, 2], "-", color=grau, lw=0.9,
            label=f"max. {FEIN}")
    ax.plot((d_g[mpg] - d0) / 1000, xyz_g[mpg, 2], "-", color=schwarz, lw=1.8,
            label=f"max. {GROB}")
    ax.set_xlim(0, fen / 1000)
    ax.set_xlabel("Distance (km)", fontsize=8)
    ax.set_ylabel("Elevation (m a.s.l.)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(color="0.85", lw=0.5)
    ax.legend(loc="best", fontsize=7, frameon=False)
    v = naechste_version("elevation_profile_paper")
    out2 = DATA / f"elevation_profile_paper_V{v}.pdf"
    fig.savefig(out2, bbox_inches="tight")
    fig.savefig(out2.with_suffix(".png"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    vt2 = naechste_version("elevation_profile_tikz", ".tex")
    out2t = DATA / f"elevation_profile_tikz_V{vt2}.tex"
    schreibe_tikz_profil((d_f[mpf] - d0) / 1000, xyz_f[mpf, 2],
                         (d_g[mpg] - d0) / 1000, xyz_g[mpg, 2],
                         out2t, args.section, args.fenster_km)

    print(f"Sektion {args.section}: Fenster ab km {d0/1000:.1f} "
          f"(Profil {args.fenster_km} km, Karte {args.karte_km} km)")
    print(f"Karte : {out1.name}  ({n_f} vs. {n_g} Knoten im Ausschnitt)")
    print(f"Profil: {out2.name}")
    print(f"TikZ  : {out1t.name}, {out2t.name}")


if __name__ == "__main__":
    main()
