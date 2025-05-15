import rasterio
import numpy as np
from scipy.spatial import KDTree

# === Parameter ===
tif_path = "data/DTM Cyprus 50m v1 by Sonny.tif"
output_path = "data/kdtree_cyprus_dtm50m.npz"

# === 1. GeoTIFF öffnen ===
with rasterio.open(tif_path) as dataset:
    data = dataset.read(1)
    transform = dataset.transform
    nodata = dataset.nodata
    mask = data != nodata

    rows, cols = np.where(mask)
    xs, ys = rasterio.transform.xy(transform, rows, cols)
    coords = np.column_stack([xs, ys])
    heights = data[rows, cols]

# === 2. KDTree erstellen ===
tree = KDTree(coords)

# === 3. Baumdaten serialisieren ===
np.savez_compressed(output_path, coords=coords, heights=heights)

print(f"KDTree-Daten gespeichert unter: {output_path}")
