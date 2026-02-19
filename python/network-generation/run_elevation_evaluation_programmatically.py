from pathlib import Path
from evaluate_elevation_resolution import run_programmatically  # ← richtiges Modul!

results = run_programmatically(
    single_network=Path("data/Saarland_max1000m_V0.xml.gz"),
    batch_glob="data/Saarland_max*V0.xml.gz",
    gpkg=Path("data/Saarland_3d_raster_clamped.gpkg"),
    layer="roads_3d",
    ds=5.0,
    mode="by_uv",                         # ← WICHTIG: echter Linienmodus
    limit_links=0,
    bbox_pad=0.0,
    out_csv_single=Path("data/benchmark_Saarland_slope_only.csv"),
    out_csv_batch=Path("data/batch_slope_vs_maxlen.csv"),
    out_plot=Path("data/slope_vs_maxlink.png"),
    save_samples=None
)

print("Single-Run CSV:", results.get("single"))
print("Batch-CSV:", results.get("batch_csv"))
print("Plot:", results.get("plot"))
