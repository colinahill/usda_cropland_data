"""Post-ingest validation: compare store contents against source rasters."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.windows
import zarr
from icechunk import Repository

from . import config, ingest
from .config import Resolution

log = logging.getLogger(__name__)

SAMPLE_WINDOW = 1024  # px, per random sample comparison


@dataclass(frozen=True)
class ValidationResult:
    check: str
    passed: bool
    message: str


def validate_year(
    repo: Repository,
    resolution: Resolution,
    year: int,
    *,
    tif_path: Path | None = None,
    samples: int = 12,
    seed: int | None = None,
) -> list[ValidationResult]:
    """Run all checks for one (resolution, year). Source comparisons need tif_path."""
    session = repo.readonly_session("main")
    results: list[ValidationResult] = []

    arr = zarr.open_array(session.store, path=f"{resolution}/{config.DATA_VAR_NAME}", mode="r")
    grid = config.GRIDS[resolution]
    shape_ok = arr.shape[1:] == (grid.height, grid.width)
    results.append(
        ValidationResult(
            "array_shape",
            shape_ok,
            f"crop_type shape {arr.shape} vs grid ({grid.height}, {grid.width})",
        )
    )

    years = zarr.open_array(session.store, path=f"{resolution}/{config.APPEND_DIM}", mode="r")[:]
    if year not in years:
        results.append(ValidationResult("year_coord", False, f"{year} missing from year coord"))
        return results
    year_idx = int(np.nonzero(years == year)[0][0])

    x = zarr.open_array(session.store, path=f"{resolution}/x", mode="r")
    coord_ok = np.isclose(x[0], grid.x_min + grid.pixel_size / 2)
    results.append(
        ValidationResult(
            "coord_alignment",
            bool(coord_ok),
            f"x[0]={x[0]} vs expected {grid.x_min + grid.pixel_size / 2}",
        )
    )

    attrs = dict(arr.attrs)
    for key in ("flag_values", "class_names", "class_colors", "grid_mapping"):
        results.append(ValidationResult(f"attr:{key}", key in attrs, f"crop_type attr '{key}' present"))

    if tif_path is None:
        results.append(ValidationResult("source_comparison", True, "skipped (source tif not on disk)"))
        return results

    rng = np.random.default_rng(seed)
    with rasterio.open(tif_path) as src:
        placement = ingest.validate_and_place(src, grid)
        mismatches = 0
        checked_px = 0
        nonzero_px = 0
        for _ in range(samples):
            h = min(SAMPLE_WINDOW, placement.height)
            w = min(SAMPLE_WINDOW, placement.width)
            r0 = int(rng.integers(0, placement.height - h + 1))
            c0 = int(rng.integers(0, placement.width - w + 1))
            src_block = src.read(1, window=rasterio.windows.Window(c0, r0, w, h))
            store_block = arr[
                year_idx,
                placement.row_off + r0 : placement.row_off + r0 + h,
                placement.col_off + c0 : placement.col_off + c0 + w,
            ]
            mismatches += int((src_block != store_block).sum())
            checked_px += src_block.size
            nonzero_px += int((src_block != 0).sum())
        results.append(
            ValidationResult(
                "pixel_equality",
                mismatches == 0,
                f"{mismatches} mismatched of {checked_px:,} sampled px "
                f"({nonzero_px:,} non-background) across {samples} windows",
            )
        )

    return results
