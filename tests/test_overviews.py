"""Overview pyramid tests on the shrunken grid (80 x 100 native, 16px chunks, 32px shards)."""

from __future__ import annotations

import math

import icechunk
import numpy as np
import pytest
import xarray as xr
import zarr

from tests.conftest import TEST_GRID
from usda_cdl import config, ingest, overviews, template


def reference_mode(data: np.ndarray, factor: int) -> np.ndarray:
    """Brute-force block mode with pad-0 and smallest-value tie-break."""
    h, w = data.shape
    out = np.zeros((math.ceil(h / factor), math.ceil(w / factor)), dtype=np.uint8)
    for by in range(out.shape[0]):
        for bx in range(out.shape[1]):
            block = np.zeros((factor, factor), dtype=np.uint8)
            src = data[by * factor : (by + 1) * factor, bx * factor : (bx + 1) * factor]
            block[: src.shape[0], : src.shape[1]] = src
            counts = np.bincount(block.ravel(), minlength=256)
            out[by, bx] = counts.argmax()  # argmax -> smallest value on ties
    return out


@pytest.fixture(autouse=True)
def small_factors(monkeypatch):
    monkeypatch.setattr(config, "OVERVIEW_FACTORS", {"30m": [2, 4, 8], "10m": [2]})


@pytest.fixture
def repo_with_year(tmp_path, synthetic_tif):
    tif, data = synthetic_tif
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(tmp_path / "store")))
    session = repo.writable_session("main")
    template.init_store(session, ["30m"])
    session.commit("init")
    session = repo.writable_session("main")
    ingest.ingest_year(session, "30m", 2025, tif, workers=2)
    session.commit("ingest")
    return repo, data


@pytest.mark.parametrize("factor", [1, 2, 3, 4, 7])
def test_block_mode_matches_reference(factor):
    rng = np.random.default_rng(factor)
    data = rng.choice(np.array([0, 1, 5, 24, 61], dtype=np.uint8), size=(factor * 6, factor * 9))
    np.testing.assert_array_equal(overviews.block_mode(data, factor), reference_mode(data, factor))


def test_block_mode_tie_breaks_to_smallest():
    block = np.array([[1, 5], [5, 1]], dtype=np.uint8)
    assert overviews.block_mode(block, 2)[0, 0] == 1


def test_level_grid_dims_and_coords():
    lg = overviews.level_grid(TEST_GRID, 8)  # 80x100 -> 10x13 (partial last col)
    assert (lg.height, lg.width) == (10, 13)
    assert lg.pixel_size == 240.0
    assert lg.x_coords()[0] == TEST_GRID.x_min + 120.0  # pixel centre at level res


def test_init_and_generate(repo_with_year):
    repo, _ = repo_with_year

    session = repo.writable_session("main")
    overviews.init_overviews(session, "30m")
    session.commit("init overviews")

    session = repo.writable_session("main")
    stats = overviews.generate_year(session, "30m", 2025, workers=2)
    session.commit("overviews 2025")
    assert stats["shards_written"] > 0

    ro = repo.readonly_session("main")
    native = zarr.open_array(ro.store, path="30m/crop_type", mode="r")[1]  # 2025 slot

    for factor in (2, 4, 8):
        ds = xr.open_zarr(ro.store, group=f"30m/{factor}x", chunks=None)
        lg = overviews.level_grid(config.GRIDS["30m"], factor)
        assert ds.crop_type.shape == (2, lg.height, lg.width)
        assert ds.crop_type.dtype == np.uint8
        # exact mode against brute-force reference over the FULL canonical grid
        np.testing.assert_array_equal(ds.crop_type.sel(year=2025).values, reference_mode(native, factor))
        # untouched year stays background
        assert ds.crop_type.sel(year=2024).values.max() == 0
        # level coords are pixel centres of the coarser grid
        assert ds.x.values[0] == lg.x_min + lg.pixel_size / 2
        assert ds.attrs["overview_factor"] == factor


def test_multiscales_metadata(repo_with_year):
    repo, _ = repo_with_year
    session = repo.writable_session("main")
    overviews.init_overviews(session, "30m")
    session.commit("init overviews")

    group = zarr.open_group(repo.readonly_session("main").store, path="30m", mode="r")
    attrs = dict(group.attrs)
    ms = attrs["multiscales"]
    assert ms["resampling_method"] == "mode"
    layout = ms["layout"]
    assert layout[0]["asset"] == "."
    assert "derived_from" not in layout[0]
    assert [entry["asset"] for entry in layout[1:]] == ["2x", "4x", "8x"]
    for entry in layout[1:]:
        assert entry["derived_from"] == "."  # mode: always from native
        assert entry["resampling_method"] == "mode"
        assert "transform" in entry and "spatial:shape" in entry
    # relative scale between consecutive levels is always 2 for our ladder
    assert layout[1]["transform"]["scale"] == [1.0, 2.0, 2.0]
    assert layout[2]["transform"]["scale"] == [1.0, 2.0, 2.0]
    assert any(c["uuid"] == "d35379db-88df-4056-af3a-620245f8e347" for c in attrs["zarr_conventions"])
    assert attrs["proj:code"] == "EPSG:5070"
    assert attrs["spatial:shape"] == [TEST_GRID.height, TEST_GRID.width]
    # native group attrs (class metadata etc.) survive the update
    assert attrs["spatial_resolution"] == "30 m"


def test_overviews_initialized_detection(repo_with_year):
    repo, _ = repo_with_year
    session = repo.writable_session("main")
    assert not overviews.overviews_initialized(session, "30m")
    overviews.init_overviews(session, "30m")
    session.commit("init overviews")
    session = repo.writable_session("main")
    assert overviews.overviews_initialized(session, "30m")
