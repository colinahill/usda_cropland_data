"""Dataset structure as frozen, reviewable configuration.

Grid extents, chunking, and attribute constants are declared here so that any
structural change to the published store shows up as a code diff (pattern
borrowed from dynamical-org/reformatters).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Resolution = Literal["30m", "10m"]

EPSG = 5070  # USA Contiguous Albers Equal Area Conic (NAD83), as used by NLCD/mrlc.gov
NODATA = 0  # 0 == "Background" class; also the zarr fill_value

DATA_VAR_NAME = "crop_type"
APPEND_DIM = "year"

# Bumped only for breaking structural changes (re-chunk, re-grid); also names the
# store directory (v{DATASET_VERSION}.icechunk) so incompatible versions can be
# published side by side without breaking existing readers.
DATASET_VERSION = "0.1.0"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class GridSpec(FrozenModel):
    """Canonical target grid for one resolution group.

    Per-year source files do not all share one extent (e.g. the 2025 30m file is
    larger than the 2008-2024 grid). The canonical grid is the union of known
    yearly extents on the shared lattice; each year is placed into it by affine
    offset and the surrounding area stays at fill_value 0 (Background).
    """

    resolution: Resolution
    pixel_size: float  # metres; pixels are square, north-up
    x_min: float  # upper-left corner x (edge, not pixel centre)
    y_max: float  # upper-left corner y
    width: int
    height: int

    @property
    def x_max(self) -> float:
        return self.x_min + self.width * self.pixel_size

    @property
    def y_min(self) -> float:
        return self.y_max - self.height * self.pixel_size

    def x_coords(self):
        """Pixel-centre x coordinates (EPSG:5070 metres)."""
        import numpy as np

        return self.x_min + (np.arange(self.width) + 0.5) * self.pixel_size

    def y_coords(self):
        """Pixel-centre y coordinates, descending (EPSG:5070 metres)."""
        import numpy as np

        return self.y_max - (np.arange(self.height) + 0.5) * self.pixel_size

    @property
    def geotransform(self) -> str:
        """GDAL GeoTransform string for the spatial_ref attr."""
        return f"{self.x_min} {self.pixel_size} 0.0 {self.y_max} 0.0 {-self.pixel_size}"


# 30m: union of the historical national grid (2008-2024: UL -2356095, 3172605,
# 153811 x 96523) and the 2025 grid (verified locally from 2025_30m_cdls.tif),
# which fully contains it.
GRID_30M = GridSpec(
    resolution="30m",
    pixel_size=30.0,
    x_min=-2_417_835.0,
    y_max=3_321_225.0,
    width=160_171,
    height=105_432,
)

# 10m: union of the 2024 and 2025 grids, both verified from the actual GeoTIFFs
# (2024: 289567 x 461431 at UL -2356085, 3172595; 2025: 316295 x 480509 at
# UL -2417815, 3321235, which fully contains 2024). Note the 2025 10m grid does
# NOT share corners with the 2025 30m grid - never infer one from the other.
# Ingest asserts every source file fits this grid and sits on its lattice; if a
# future year exceeds it, ingest fails loudly and this spec must be extended.
GRID_10M = GridSpec(
    resolution="10m",
    pixel_size=10.0,
    x_min=-2_417_815.0,
    y_max=3_321_235.0,
    width=480_509,
    height=316_295,
)

GRIDS: dict[Resolution, GridSpec] = {"30m": GRID_30M, "10m": GRID_10M}

# National GeoTIFF zips exist for these product years.
YEARS: dict[Resolution, list[int]] = {
    "30m": list(range(2008, 2026)),
    "10m": list(range(2024, 2026)),
}


class EncodingSpec(FrozenModel):
    """Zarr v3 sharded encoding for the crop_type array.

    Small inner chunks keep field/point reads cheap (~39 KB compressed per
    512x512 chunk, individually range-requestable); shards pack 16x16 inner
    chunks into one storage object so the store stays at a few hundred objects
    per year. Shard dims must be multiples of chunk dims.
    """

    chunk_year: int = 1
    chunk_y: int = 512
    chunk_x: int = 512
    shard_y: int = 8192
    shard_x: int = 8192
    zstd_level: int = 3
    fill_value: int = NODATA

    @property
    def chunks(self) -> tuple[int, int, int]:
        return (self.chunk_year, self.chunk_y, self.chunk_x)

    @property
    def shards(self) -> tuple[int, int, int]:
        return (self.chunk_year, self.shard_y, self.shard_x)


ENCODING = EncodingSpec()

SOURCE_URL_TEMPLATE = (
    "https://www.nass.usda.gov/Research_and_Science/Cropland/Release/datasets/{year}_{resolution}_cdls.zip"
)

# Official form from the CDL FAQ / FGDC metadata:
#   originator, pubdate, title: publisher, publisher place.
CITATION_TEMPLATE = (
    "United States Department of Agriculture (USDA) National Agricultural Statistics "
    "Service (NASS), {year} Cropland Data Layer: USDA NASS, USDA NASS Marketing and "
    "Information Services Office, Washington, D.C. "
    "Available at https://croplandcros.scinet.usda.gov/."
)

ROOT_ATTRS = {
    "title": "USDA NASS Cropland Data Layer (CDL)",
    "description": (
        "Annual crop-specific land cover classification of the conterminous United "
        "States produced by the USDA National Agricultural Statistics Service (NASS). "
        "Group '30m' holds the 30 m national product (2008-present; from product year "
        "2024 the 30 m raster is a resampling of the native 10 m CDL). Group '10m' "
        "holds the native 10 m product (2024-present)."
    ),
    "dataset_id": "usda-cropland-data-layer",
    "dataset_version": DATASET_VERSION,
    "license": (
        "US Public Domain. The USDA NASS Cropland Data Layer has no copyright "
        "restrictions, is considered public domain, and is free to redistribute; "
        "USDA NASS asks for acknowledgement when the data are used."
    ),
    "attribution": CITATION_TEMPLATE.format(year="{year}"),
    "producer": (
        "USDA National Agricultural Statistics Service (NASS), Research and "
        "Development Division, Spatial Analysis Research Section"
    ),
    "contact": "SM.NASS.RDD.GIB@usda.gov",
    "source": SOURCE_URL_TEMPLATE,
    "source_release_page": ("https://www.nass.usda.gov/Research_and_Science/Cropland/Release/index.php"),
    "source_metadata_page": ("https://www.nass.usda.gov/Research_and_Science/Cropland/metadata/meta.php"),
    "source_faq_page": "https://www.nass.usda.gov/Research_and_Science/Cropland/sarsfaqs2.php",
    "croplandcros_url": "https://croplandcros.scinet.usda.gov/",
    "references": (
        'Z. Li, R. Mueller, Z. Yang, D. Johnson and P. Willis, "Cloud-Powered '
        "Agricultural Mapping: A Revolution Toward 10m Resolution Cropland Data "
        'Layers," IGARSS 2024 - 2024 IEEE International Geoscience and Remote Sensing '
        "Symposium, Athens, Greece, 2024, pp. 4081-4084, "
        "doi:10.1109/IGARSS53475.2024.10641079"
    ),
    "update_frequency": "annual (released ~February for the prior crop year)",
    "processing_code": "https://github.com/colinahill/usda_cropland_data",
    "Conventions": "CF-1.10",
}

# Methodology / source-imagery notes keyed by first product year they apply to.
METHODOLOGY_BY_ERA = {
    2008: (
        "Supervised decision-tree classification of satellite imagery (Landsat "
        "TM/ETM+/OLI, AWiFS, and similar sensors depending on year) trained on FSA "
        "Common Land Unit data, with NLCD-derived non-agricultural classes."
    ),
    2024: (
        "Random-forest classification of harmonized Landsat 8/9 OLI and Sentinel-2 "
        "A/B surface reflectance composites in Google Earth Engine; native product "
        "resolution 10 m. The 30 m product from 2024 onward is a resampling of the "
        "official 10 m CDL provided for historical consistency."
    ),
}


def group_attrs(resolution: Resolution) -> dict:
    grid = GRIDS[resolution]
    return {
        "spatial_resolution": f"{grid.pixel_size:g} m",
        "spatial_domain": "Conterminous United States (CONUS)",
        "crs": f"EPSG:{EPSG}",
        "grid_note": (
            "Canonical union grid; yearly source rasters are placed by affine offset "
            "and areas outside a given year's source extent are 0 (Background). See "
            "per-year attrs source_urls / source_extents."
        ),
        "methodology_by_era": {str(k): v for k, v in METHODOLOGY_BY_ERA.items()},
    }
