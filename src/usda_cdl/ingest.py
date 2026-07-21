"""Ingest one CDL year into the store: windowed reads -> zarr region writes.

Design notes:
- The source GeoTIFF is validated against the canonical grid (same CRS, same
  pixel size, on-lattice origin, contained extent) and placed by affine offset.
- Work is split into windows aligned to the zarr shard grid (the storage-object
  unit), so concurrent writers never co-write one object. Reads and writes run
  in a thread pool (rasterio decodes ~500 Mpx/s/thread; zarr/icechunk release
  the GIL during compression and uploads).
- All-background windows are skipped, and zarr's sharding codec omits
  all-fill-value inner chunks, so background areas occupy no storage.
- This function performs no commit; the caller owns the session/commit, which
  keeps it reusable for a future distributed (fork/merge, e.g. Coiled) wrapper.
"""

from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.crs
import rasterio.windows
import zarr
from icechunk import Session

from . import config
from .config import Resolution

log = logging.getLogger(__name__)

LATTICE_TOL = 1e-3  # metres; source origins must sit on the canonical lattice


@dataclass(frozen=True)
class Placement:
    """Where a source raster lands inside the canonical grid (pixel units)."""

    row_off: int
    col_off: int
    height: int
    width: int


def validate_and_place(src: rasterio.DatasetReader, grid: config.GridSpec) -> Placement:
    """Assert the source raster is compatible with the canonical grid."""
    expected_crs = rasterio.crs.CRS.from_epsg(config.EPSG)
    if src.crs != expected_crs:
        raise ValueError(
            f"{src.name}: CRS {src.crs} does not match canonical EPSG:{config.EPSG}"
        )
    t = src.transform
    if not (
        math.isclose(t.a, grid.pixel_size, abs_tol=LATTICE_TOL)
        and math.isclose(t.e, -grid.pixel_size, abs_tol=LATTICE_TOL)
        and t.b == 0
        and t.d == 0
    ):
        raise ValueError(f"{src.name}: transform {t!r} is not {grid.pixel_size} m north-up")

    col_f = (t.c - grid.x_min) / grid.pixel_size
    row_f = (grid.y_max - t.f) / grid.pixel_size
    if abs(col_f - round(col_f)) * grid.pixel_size > LATTICE_TOL or (
        abs(row_f - round(row_f)) * grid.pixel_size > LATTICE_TOL
    ):
        raise ValueError(
            f"{src.name}: origin ({t.c}, {t.f}) is not on the canonical "
            f"{grid.pixel_size} m lattice anchored at ({grid.x_min}, {grid.y_max})"
        )
    col_off, row_off = round(col_f), round(row_f)
    if (
        col_off < 0
        or row_off < 0
        or col_off + src.width > grid.width
        or row_off + src.height > grid.height
    ):
        raise ValueError(
            f"{src.name}: extent (offset {row_off},{col_off}, size {src.height}x"
            f"{src.width}) exceeds the canonical {grid.resolution} grid "
            f"({grid.height}x{grid.width}). NASS has likely expanded the product "
            "extent; update the GridSpec in usda_cdl/config.py and re-init the group."
        )
    return Placement(row_off=row_off, col_off=col_off, height=src.height, width=src.width)


def shard_aligned_windows(placement: Placement) -> list[tuple[int, int, int, int]]:
    """Canonical-pixel windows (gy0, gy1, gx0, gx1), aligned to the shard grid,
    covering exactly the source extent.

    Shards are the storage-object unit, so aligning work to shard boundaries
    guarantees concurrent writers never co-write one object (and whole-shard
    writes avoid read-modify-write).
    """
    enc = config.ENCODING
    y0, y1 = placement.row_off, placement.row_off + placement.height
    x0, x1 = placement.col_off, placement.col_off + placement.width
    windows = []
    gy = y0
    while gy < y1:
        gy_next = min((gy // enc.shard_y + 1) * enc.shard_y, y1)
        gx = x0
        while gx < x1:
            gx_next = min((gx // enc.shard_x + 1) * enc.shard_x, x1)
            windows.append((gy, gy_next, gx, gx_next))
            gx = gx_next
        gy = gy_next
    return windows


def ingest_year(
    session: Session,
    resolution: Resolution,
    year: int,
    tif_path: str | Path,
    *,
    workers: int = 8,
) -> dict:
    """Write one year's raster into ``{resolution}/crop_type``. No commit.

    Returns stats for the commit metadata / logs.
    """
    grid = config.GRIDS[resolution]
    array_path = f"{resolution}/{config.DATA_VAR_NAME}"

    years = zarr.open_array(session.store, path=f"{resolution}/{config.APPEND_DIM}", mode="r")[:]
    matches = np.nonzero(years == year)[0]
    if len(matches) != 1:
        raise ValueError(f"year {year} not found in {resolution} year coordinate {years}")
    year_idx = int(matches[0])

    with rasterio.open(tif_path) as src:
        placement = validate_and_place(src, grid)
    windows = shard_aligned_windows(placement)
    log.info(
        "%s %s: placing %dx%d at offset (%d, %d); %d windows, %d workers",
        resolution, year, placement.height, placement.width,
        placement.row_off, placement.col_off, len(windows), workers,
    )

    target = zarr.open_array(session.store, path=array_path, mode="r+")
    local = threading.local()
    written = skipped = 0
    lock = threading.Lock()

    def process(window: tuple[int, int, int, int]) -> None:
        nonlocal written, skipped
        gy0, gy1, gx0, gx1 = window
        src_handle = getattr(local, "src", None)
        if src_handle is None:
            src_handle = local.src = rasterio.open(tif_path)
        block = src_handle.read(
            1,
            window=rasterio.windows.Window(
                gx0 - placement.col_off,
                gy0 - placement.row_off,
                gx1 - gx0,
                gy1 - gy0,
            ),
        )
        if not block.any():  # all Background: leave chunk unwritten (== fill_value)
            with lock:
                skipped += 1
            return
        target[year_idx, gy0:gy1, gx0:gx1] = block
        with lock:
            written += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # list() propagates the first exception from any worker
        list(pool.map(process, windows))

    stats = {
        "resolution": resolution,
        "year": year,
        "windows_written": written,
        "windows_skipped_all_background": skipped,
        "source_shape": [placement.height, placement.width],
        "placement_offset": [placement.row_off, placement.col_off],
    }
    log.info("%s %s: %d windows written, %d skipped", resolution, year, written, skipped)
    return stats


def record_year_provenance(
    session: Session, resolution: Resolution, year: int, provenance: dict
) -> None:
    """Merge per-year provenance (source url, checksum, extent) into group attrs."""
    group = zarr.open_group(session.store, path=resolution, mode="r+")
    per_year = dict(group.attrs.get("source_files", {}))
    per_year[str(year)] = provenance
    group.attrs["source_files"] = per_year
