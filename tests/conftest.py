from __future__ import annotations

import numpy as np
import pytest
import rasterio
import rasterio.transform

from usda_cdl import config

# Shrunken canonical grid on the real 30m lattice (pattern from
# dynamical-org/reformatters: test at tiny scale but keep multi-chunk paths hot).
TEST_GRID = config.GridSpec(
    resolution="30m",
    pixel_size=30.0,
    x_min=-2_417_835.0,
    y_max=3_321_225.0,
    width=100,
    height=80,
)
TEST_ENCODING = config.EncodingSpec(chunk_y=16, chunk_x=16, shard_y=32, shard_x=32)
TEST_YEARS = {"30m": [2024, 2025], "10m": [2024, 2025]}


@pytest.fixture(autouse=True)
def small_grid(monkeypatch):
    monkeypatch.setattr(config, "GRIDS", {"30m": TEST_GRID, "10m": TEST_GRID})
    monkeypatch.setattr(config, "YEARS", TEST_YEARS)
    monkeypatch.setattr(config, "ENCODING", TEST_ENCODING)


def write_synthetic_tif(path, *, width=64, height=48, col_off=10, row_off=20, seed=0):
    """A tiny CDL-like GeoTIFF placed inside TEST_GRID at a pixel offset."""
    rng = np.random.default_rng(seed)
    codes = np.array([0, 1, 5, 24, 36, 61, 81, 121, 176], dtype="uint8")
    data = rng.choice(codes, size=(height, width)).astype("uint8")
    data[0, 0] = 1  # deterministic corners for spot checks
    data[-1, -1] = 5
    transform = rasterio.transform.from_origin(
        TEST_GRID.x_min + col_off * 30.0, TEST_GRID.y_max - row_off * 30.0, 30.0, 30.0
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs=rasterio.crs.CRS.from_epsg(config.EPSG),
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(data, 1)
    return data


@pytest.fixture
def synthetic_tif(tmp_path):
    tif = tmp_path / "2025_30m_cdls.tif"
    data = write_synthetic_tif(tif)
    return tif, data
