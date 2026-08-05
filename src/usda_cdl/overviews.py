"""Multiscale overview pyramids (zarr-conventions/multiscales, GeoZarr style).

Layout (parent/child model, matching the Earthmover reference implementation):
the native arrays stay untouched in the resolution group, which gains
``multiscales`` metadata; each downsample factor F gets a child group ``{F}x``
with its own coords and a mode-resampled crop_type. Everything is additive - no
existing data is rewritten, so retrofitting a pyramid onto a published store
costs only the new levels' objects.

Resampling is exact block **mode** (majority), matching NASS's own practice for
deriving 30m from 10m. Mode is NOT composable, so every level is computed from
the native array, never from a coarser level. Ties break to the smallest class
code (deterministic). Partial blocks at the grid's bottom/right edge are padded
with 0 (Background) before the mode - consistent with "outside the classified
extent is Background", and those edges are ocean/background anyway.
"""

from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import zarr
from icechunk import Session
from icechunk.xarray import to_icechunk
from tqdm import tqdm

from . import config, metadata, template
from .config import Resolution

log = logging.getLogger(__name__)

# native pixels read per compute tile; must be divisible by every factor
COMPUTE_TILE = 4096


def level_grid(grid: config.GridSpec, factor: int) -> config.GridSpec:
    """Grid of one overview level: same origin, factor-times coarser pixels."""
    return config.GridSpec(
        resolution=grid.resolution,
        pixel_size=grid.pixel_size * factor,
        x_min=grid.x_min,
        y_max=grid.y_max,
        width=math.ceil(grid.width / factor),
        height=math.ceil(grid.height / factor),
    )


def level_group(resolution: Resolution, factor: int) -> str:
    return f"{resolution}/{factor}x"


def block_mode(block: np.ndarray, factor: int) -> np.ndarray:
    """Exact mode over factor x factor blocks (2-D uint8 input).

    Input dims must be multiples of ``factor`` (callers pad edges with 0).
    Vectorized: sort each block's pixels, find the longest run. Ties resolve to
    the smallest value (first longest run in ascending order).
    """
    h, w = block.shape
    bh, bw = h // factor, w // factor
    k = factor * factor
    v = block.reshape(bh, factor, bw, factor).transpose(0, 2, 1, 3).reshape(bh, bw, k)
    s = np.sort(v, axis=-1)
    idx = np.arange(k, dtype=np.int32)
    starts = np.empty(s.shape, dtype=bool)
    starts[..., 0] = True
    starts[..., 1:] = s[..., 1:] != s[..., :-1]
    start_pos = np.where(starts, idx, 0).astype(np.int32)
    np.maximum.accumulate(start_pos, axis=-1, out=start_pos)
    run_len = idx - start_pos + 1
    best = run_len.argmax(axis=-1)
    return np.take_along_axis(s, best[..., None], axis=-1)[..., 0]


# ---------------------------------------------------------------------------
# structure + metadata


def _spatial_transform(grid: config.GridSpec) -> list[float]:
    """GDAL-style 6-parameter affine [a, b, c, d, e, f] for spatial:transform."""
    return [grid.pixel_size, 0.0, grid.x_min, 0.0, -grid.pixel_size, grid.y_max]


def multiscales_attrs(resolution: Resolution) -> dict:
    """GeoZarr multiscales/spatial/proj attributes for the resolution group.

    Mirrors earth-mover/icechunk-multiscales-demo, with the native level as a
    "." self-reference (the schema's asset pattern forbids a leading "/";
    relative self is the parent/child equivalent).
    """
    grid = config.GRIDS[resolution]
    factors = config.OVERVIEW_FACTORS[resolution]

    layout: list[dict] = [
        {
            "asset": ".",
            "transform": {"scale": [1.0, 1.0, 1.0], "translation": [0.0, 0.0, 0.0]},
            "spatial:transform": _spatial_transform(grid),
            "spatial:shape": [grid.height, grid.width],
        }
    ]
    prev = 1
    for factor in factors:
        lg = level_grid(grid, factor)
        scale = factor / prev
        layout.append(
            {
                "asset": f"{factor}x",
                "derived_from": ".",  # mode is non-composable: always from native
                "transform": {
                    "scale": [1.0, scale, scale],
                    "translation": [0.0, (scale - 1) / 2, (scale - 1) / 2],
                },
                "spatial:transform": _spatial_transform(lg),
                "spatial:shape": [lg.height, lg.width],
                "resampling_method": config.OVERVIEW_RESAMPLING,
            }
        )
        prev = factor

    return {
        "zarr_conventions": [
            {
                "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
                "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
                "uuid": "d35379db-88df-4056-af3a-620245f8e347",
                "name": "multiscales",
                "description": "Multiscale layout of zarr datasets",
            },
            {
                "schema_url": "https://raw.githubusercontent.com/zarr-experimental/geo-proj/refs/tags/v1/schema.json",
                "spec_url": "https://github.com/zarr-experimental/geo-proj/blob/v1/README.md",
                "uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
                "name": "proj:",
                "description": "Coordinate reference system information for geospatial data",
            },
            {
                "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v1/schema.json",
                "spec_url": "https://github.com/zarr-conventions/spatial/blob/v1/README.md",
                "uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
                "name": "spatial:",
                "description": "Spatial coordinate information",
            },
        ],
        "multiscales": {"layout": layout, "resampling_method": config.OVERVIEW_RESAMPLING},
        "spatial:dimensions": ["y", "x"],
        "spatial:transform": _spatial_transform(grid),
        "spatial:bbox": [grid.x_min, grid.y_min, grid.x_max, grid.y_max],
        "spatial:shape": [grid.height, grid.width],
        "proj:code": f"EPSG:{config.EPSG}",
    }


def init_overviews(session: Session, resolution: Resolution) -> None:
    """Create the overview level groups (coords + empty arrays) and pyramid
    metadata on the resolution group. Additive; native arrays untouched. No commit."""
    grid = config.GRIDS[resolution]
    enc = config.ENCODING
    classes = metadata.load_bundled_classes()

    for factor in config.OVERVIEW_FACTORS[resolution]:
        lg = level_grid(grid, factor)
        group_path = level_group(resolution, factor)
        group_attrs = {
            "overview_factor": factor,
            "spatial_resolution": f"{lg.pixel_size:g} m",
            "resampling_method": config.OVERVIEW_RESAMPLING,
            "derived_from": f"native {grid.pixel_size:g} m crop_type via {config.OVERVIEW_RESAMPLING} "
            f"over {factor}x{factor} blocks",
        }
        to_icechunk(
            template.coords_dataset(resolution, grid=lg, group_attrs=group_attrs),
            session,
            group=group_path,
            mode="w",
        )
        group = zarr.open_group(session.store, path=group_path, mode="r+")
        group.create_array(
            config.DATA_VAR_NAME,
            shape=(len(config.YEARS[resolution]), lg.height, lg.width),
            chunks=enc.chunks,
            shards=enc.shards,
            dtype="uint8",
            fill_value=enc.fill_value,
            compressors=[zarr.codecs.ZstdCodec(level=enc.zstd_level)],
            dimension_names=(config.APPEND_DIM, "y", "x"),
            attributes=metadata.crop_type_attrs(classes),
        )

    parent = zarr.open_group(session.store, path=resolution, mode="r+")
    parent.attrs.update(multiscales_attrs(resolution))


def overviews_initialized(session: Session, resolution: Resolution) -> bool:
    parent = zarr.open_group(session.store, path=resolution, mode="r")
    return "multiscales" in parent.attrs and all(f"{f}x" in parent for f in config.OVERVIEW_FACTORS[resolution])


# ---------------------------------------------------------------------------
# generation


@dataclass(frozen=True)
class _Task:
    factor: int
    oy0: int
    oy1: int
    ox0: int
    ox1: int  # output-level pixel window, aligned to the output shard grid


def _tasks_for_level(lg: config.GridSpec, factor: int) -> list[_Task]:
    enc = config.ENCODING
    tasks = []
    for oy0 in range(0, lg.height, enc.shard_y):
        for ox0 in range(0, lg.width, enc.shard_x):
            tasks.append(_Task(factor, oy0, min(oy0 + enc.shard_y, lg.height), ox0, min(ox0 + enc.shard_x, lg.width)))
    return tasks


def generate_year(session: Session, resolution: Resolution, year: int, *, workers: int = 8) -> dict:
    """Fill every overview level for one year, computed from the native array.

    Tasks are aligned to each level's shard grid (single writer per storage
    object); within a task the native input is streamed in COMPUTE_TILE blocks
    so memory stays bounded regardless of factor. No commit.
    """
    grid = config.GRIDS[resolution]
    years = zarr.open_array(session.store, path=f"{resolution}/{config.APPEND_DIM}", mode="r")[:]
    matches = np.nonzero(years == year)[0]
    if len(matches) != 1:
        raise ValueError(f"year {year} not found in {resolution} year coordinate")
    year_idx = int(matches[0])

    native = zarr.open_array(session.store, path=f"{resolution}/{config.DATA_VAR_NAME}", mode="r")
    levels = {
        f: zarr.open_array(session.store, path=f"{level_group(resolution, f)}/{config.DATA_VAR_NAME}", mode="r+")
        for f in config.OVERVIEW_FACTORS[resolution]
    }
    tasks = [t for f in config.OVERVIEW_FACTORS[resolution] for t in _tasks_for_level(level_grid(grid, f), f)]
    log.info("%s %s overviews: %d shard tasks across %d levels", resolution, year, len(tasks), len(levels))

    lock = threading.Lock()
    written = skipped = 0

    def process(task: _Task) -> None:
        nonlocal written, skipped
        f = task.factor
        out = np.zeros((task.oy1 - task.oy0, task.ox1 - task.ox0), dtype=np.uint8)
        iy0, ix0 = task.oy0 * f, task.ox0 * f
        iy1, ix1 = min(task.oy1 * f, grid.height), min(task.ox1 * f, grid.width)
        for ty in range(iy0, iy1, COMPUTE_TILE):
            for tx in range(ix0, ix1, COMPUTE_TILE):
                ty1, tx1 = min(ty + COMPUTE_TILE, iy1), min(tx + COMPUTE_TILE, ix1)
                tile = native[year_idx, ty:ty1, tx:tx1]
                if not tile.any():
                    continue
                pad_y = -tile.shape[0] % f
                pad_x = -tile.shape[1] % f
                if pad_y or pad_x:  # bottom/right edge: pad with Background
                    tile = np.pad(tile, ((0, pad_y), (0, pad_x)))
                reduced = block_mode(tile, f)
                out[
                    (ty - iy0) // f : (ty - iy0) // f + reduced.shape[0],
                    (tx - ix0) // f : (tx - ix0) // f + reduced.shape[1],
                ] = reduced
        if out.any():
            levels[f][year_idx, task.oy0 : task.oy1, task.ox0 : task.ox1] = out
            with lock:
                written += 1
        else:
            with lock:
                skipped += 1

    # progress in native-pixels covered: task cost scales with its input area,
    # so weighting by output size would make the bar crawl through the 2x level
    # and then sprint through the rest
    with (
        tqdm(
            total=sum((t.oy1 - t.oy0) * (t.ox1 - t.ox0) * t.factor**2 for t in tasks),
            desc=f"overviews {resolution} {year}",
            unit="px",
            unit_scale=True,
        ) as bar,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        futures = [pool.submit(process, t) for t in tasks]
        for task, future in zip(tasks, futures, strict=True):
            future.result()  # propagates the first worker exception
            bar.update((task.oy1 - task.oy0) * (task.ox1 - task.ox0) * task.factor**2)

    stats = {"resolution": resolution, "year": year, "shards_written": written, "shards_skipped": skipped}
    log.info("%s %s overviews: %d shards written, %d all-background", resolution, year, written, skipped)
    return stats
