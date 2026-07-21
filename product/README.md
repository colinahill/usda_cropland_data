# USDA NASS Cropland Data Layer (CDL)

Annual crop-specific land cover classification of the conterminous United States,
produced by the USDA National Agricultural Statistics Service (NASS), reformatted into a
cloud-optimized, version-controlled [Icechunk](https://icechunk.io) Zarr store.

## Contents

One Icechunk repository at `v0.1.0.icechunk/` with two groups:

| group | variable | years | resolution | grid (y × x) |
|---|---|---|---|---|
| `30m` | `crop_type (year, y, x) uint8` | 2008–2025 | 30 m | 105,432 × 160,171 |
| `10m` | `crop_type (year, y, x) uint8` | 2024–2025 | 10 m | 316,296 × 480,513 |

- CRS: EPSG:5070 (USA Contiguous Albers Equal Area Conic, NAD83); coordinates are pixel
  centres in metres. A CF `spatial_ref` variable carries the WKT and GeoTransform.
- Pixel values are categorical class codes. The full code → class-name and
  code → color mappings are embedded in the `crop_type` attributes
  (`class_names`, `class_colors`, CF `flag_values`/`flag_meanings`).
- `0` = Background (also the fill value: outside a given year's classified extent).
  `81` = Clouds/No Data is a real class within the classified extent.
- From product year 2024 the native CDL resolution is 10 m; the 30 m product for
  2024+ is NASS's resampling of the 10 m CDL, included for continuity with 2008–2023.
- Per-year provenance (source zip URL, SHA-256, placement) is stored in group
  attributes and icechunk commit metadata; each year is tagged (e.g. `30m-2025`).
- Storage layout: zarr v3 sharded arrays with `(1, 512, 512)` inner chunks — a
  field-scale or point query fetches only ~tens of KB per year via range requests.

## Reading the data

```python
import icechunk, xarray as xr

storage = icechunk.s3_storage(
    bucket="chill",
    prefix="usda-cropland-data-layer/v0.1.0.icechunk",
    endpoint_url="https://data.source.coop",
    region="us-east-1",
    anonymous=True,
    force_path_style=True,
)
repo = icechunk.Repository.open(storage)
session = repo.readonly_session("main")
ds = xr.open_zarr(session.store, group="30m")
```

Select an area of interest in projected coordinates (or transform lon/lat with pyproj),
then plot it with the official class colors embedded in the attributes:

```python
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch
from pyproj import Transformer

# 10 km x 10 km window around a point in Iowa (lon/lat -> Albers EPSG:5070)
x, y = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform(-93.45, 42.15)
aoi = ds["crop_type"].sel(year=2025, x=slice(x - 5000, x + 5000), y=slice(y + 5000, y - 5000))

# class_names / class_colors attrs map each integer code to its name and hex color
names, colors = aoi.attrs["class_names"], aoi.attrs["class_colors"]

# build the colormap from just the classes present in the AOI (skip Background = 0)
present = np.unique(aoi)
cmap = ListedColormap([colors[str(c)] for c in present])
cmap.set_under(colors["0"])  # Background
norm = BoundaryNorm([present[0] - 0.5, *(c + 0.5 for c in present)], cmap.N)

fig, ax = plt.subplots(figsize=(9, 7))
aoi.plot(ax=ax, cmap=cmap, norm=norm, add_colorbar=False)
ax.legend(
    handles=[Patch(color=colors[str(c)], label=names[str(c)]) for c in present],
    loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8,
)
ax.set_title("USDA Cropland Data Layer 2025")
fig.tight_layout()
```

## License

**US Public Domain.** The USDA NASS Cropland Data Layer has no copyright restrictions,
is considered public domain, and is free to redistribute. USDA NASS asks for
acknowledgement when the data are used. This README and the store metadata are provided
under the same terms.

## Citation

United States Department of Agriculture (USDA) National Agricultural Statistics Service (NASS), 20260227, Cropland Data Layer: USDA NASS, USDA NASS Marketing and Information Services Office, Washington, D.C.
Online Links: https://croplandcros.scinet.usda.gov/

## Provenance & processing

Source files are the official national GeoTIFF zips from
[NASS Research and Science](https://www.nass.usda.gov/Research_and_Science/Cropland/Release/index.php).
Pixel values are bit-identical to the source rasters (validated by sampled comparison at
ingest). 

Processing code (MIT licensed): https://github.com/colinahill/usda_cropland_data

Contact (data producer): USDA NASS Spatial Analysis Research Section,
SM.NASS.RDD.GIB@usda.gov · [CDL FAQ](https://www.nass.usda.gov/Research_and_Science/Cropland/sarsfaqs2.php)