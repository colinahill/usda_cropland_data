"""End-to-end: init store -> ingest synthetic year -> read back -> validate."""

from __future__ import annotations

import icechunk
import numpy as np
import pytest
import rasterio
import xarray as xr
import zarr

from usda_cdl import config, ingest, template, validate
from tests.conftest import TEST_GRID, write_synthetic_tif


@pytest.fixture
def repo(tmp_path):
    storage = icechunk.local_filesystem_storage(str(tmp_path / "store"))
    return icechunk.Repository.create(storage)


def init(repo, resolutions=("30m",)):
    session = repo.writable_session("main")
    template.init_store(session, list(resolutions))
    session.commit("init")


def test_init_store_structure(repo):
    init(repo, ("30m", "10m"))
    session = repo.readonly_session("main")
    root = zarr.open_group(session.store, mode="r")
    assert root.attrs["dataset_id"] == "usda-cropland-data-layer"
    for group_name in ("30m", "10m"):
        ds = xr.open_zarr(session.store, group=group_name, chunks=None)
        assert ds.crop_type.shape == (2, TEST_GRID.height, TEST_GRID.width)
        assert ds.crop_type.dtype == np.uint8
        arr = zarr.open_array(session.store, path=f"{group_name}/crop_type", mode="r")
        assert arr.chunks == (1, 16, 16)
        assert arr.shards == (1, 32, 32)
        assert ds.year.values.tolist() == [2024, 2025]
        assert "spatial_ref" in ds.coords
        assert ds.crop_type.attrs["class_names"]["1"] == "Corn"
        # pixel-centre coords
        assert ds.x.values[0] == TEST_GRID.x_min + 15.0
        assert ds.y.values[0] == TEST_GRID.y_max - 15.0


def test_ingest_and_read_back(repo, synthetic_tif):
    tif, data = synthetic_tif
    init(repo)

    session = repo.writable_session("main")
    stats = ingest.ingest_year(session, "30m", 2025, tif, workers=4)
    session.commit("ingest 2025")

    ds = xr.open_zarr(repo.readonly_session("main").store, group="30m", chunks=None)
    stored = ds.crop_type.sel(year=2025).values
    # placement offset from conftest: row 20, col 10; source 48x64
    np.testing.assert_array_equal(stored[20:68, 10:74], data)
    # outside the source extent stays Background
    assert stored[:20].max() == 0
    assert stored[68:].max() == 0
    # untouched year stays Background
    assert ds.crop_type.sel(year=2024).values.max() == 0
    # shard-aligned windows (32px): rows 20-68 -> 3 spans; cols 10-74 -> 3 spans; 3x3=9
    assert stats["windows_written"] + stats["windows_skipped_all_background"] == 9
    # geolocation round-trip: x coord of source col 0 == canonical col 10
    with rasterio.open(tif) as src:
        src_x0 = src.transform.c + 15.0
    assert ds.x.values[10] == src_x0


def test_reingest_is_idempotent(repo, synthetic_tif):
    tif, data = synthetic_tif
    init(repo)
    for i in range(2):
        session = repo.writable_session("main")
        ingest.ingest_year(session, "30m", 2025, tif, workers=2)
        session.commit(f"ingest {i}")
    ds = xr.open_zarr(repo.readonly_session("main").store, group="30m", chunks=None)
    np.testing.assert_array_equal(ds.crop_type.sel(year=2025).values[20:68, 10:74], data)


def test_ingest_rejects_bad_grid(repo, tmp_path):
    init(repo)
    # off-lattice origin
    tif = tmp_path / "bad.tif"
    write_synthetic_tif(tif)
    import rasterio.transform

    with rasterio.open(tif, "r+") as f:
        f.transform = rasterio.transform.from_origin(
            TEST_GRID.x_min + 7.0, TEST_GRID.y_max, 30.0, 30.0
        )
    session = repo.writable_session("main")
    with pytest.raises(ValueError, match="lattice"):
        ingest.ingest_year(session, "30m", 2025, tif)

    # extent overflow
    tif2 = tmp_path / "big.tif"
    write_synthetic_tif(tif2, width=200, height=200, col_off=0, row_off=0)
    with pytest.raises(ValueError, match="exceeds the canonical"):
        ingest.ingest_year(session, "30m", 2025, tif2)

    # wrong year
    tif3 = tmp_path / "ok.tif"
    write_synthetic_tif(tif3)
    with pytest.raises(ValueError, match="year 1999"):
        ingest.ingest_year(session, "30m", 1999, tif3)


def test_validate_year(repo, synthetic_tif):
    tif, _ = synthetic_tif
    init(repo)
    session = repo.writable_session("main")
    ingest.ingest_year(session, "30m", 2025, tif, workers=2)
    session.commit("ingest")

    results = validate.validate_year(repo, "30m", 2025, tif_path=tif, samples=4, seed=42)
    failed = [r for r in results if not r.passed]
    assert not failed, failed
    assert any(r.check == "pixel_equality" for r in results)


def test_validate_catches_corruption(repo, synthetic_tif):
    tif, _ = synthetic_tif
    init(repo)
    session = repo.writable_session("main")
    ingest.ingest_year(session, "30m", 2025, tif, workers=2)
    # corrupt one block inside the source footprint
    arr = zarr.open_array(session.store, path="30m/crop_type", mode="r+")
    arr[1, 30:40, 30:40] = 199
    session.commit("ingest + corruption")

    results = validate.validate_year(repo, "30m", 2025, tif_path=tif, samples=8, seed=42)
    pixel = next(r for r in results if r.check == "pixel_equality")
    assert not pixel.passed


def test_shard_aligned_windows_cover_exactly():
    placement = ingest.Placement(row_off=20, col_off=10, height=48, width=64)
    windows = ingest.shard_aligned_windows(placement)
    cells = np.zeros((TEST_GRID.height, TEST_GRID.width), dtype=int)
    for gy0, gy1, gx0, gx1 in windows:
        assert gy1 - gy0 <= config.ENCODING.shard_y
        assert gx1 - gx0 <= config.ENCODING.shard_x
        # window must not straddle a shard (storage object) boundary
        assert gy0 // config.ENCODING.shard_y == (gy1 - 1) // config.ENCODING.shard_y
        assert gx0 // config.ENCODING.shard_x == (gx1 - 1) // config.ENCODING.shard_x
        cells[gy0:gy1, gx0:gx1] += 1
    assert (cells[20:68, 10:74] == 1).all()
    assert cells.sum() == 48 * 64
