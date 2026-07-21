"""Create the empty store structure: groups, coords, attrs, and arrays.

Only metadata and (small) coordinate arrays are written here; the crop_type
arrays are created empty and filled year-by-year by ingest. Chunks that are
never written read back as fill_value 0 (Background) and occupy no storage.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
import zarr
from icechunk import Session
from icechunk.xarray import to_icechunk

from . import config, metadata
from .config import Resolution


def coords_dataset(resolution: Resolution) -> xr.Dataset:
    grid = config.GRIDS[resolution]
    coord_attrs = metadata.coordinate_attrs()
    ds = xr.Dataset(
        coords={
            config.APPEND_DIM: (
                config.APPEND_DIM,
                np.array(config.YEARS[resolution], dtype="int32"),
                coord_attrs[config.APPEND_DIM],
            ),
            "y": ("y", grid.y_coords(), coord_attrs["y"]),
            "x": ("x", grid.x_coords(), coord_attrs["x"]),
            "spatial_ref": ((), np.int64(0), metadata.spatial_ref_attrs(grid)),
        },
    )
    ds.attrs = config.group_attrs(resolution)
    return ds


def init_group(session: Session, resolution: Resolution) -> None:
    """Write one resolution group: coords + attrs + empty crop_type array."""
    grid = config.GRIDS[resolution]
    enc = config.ENCODING
    to_icechunk(coords_dataset(resolution), session, group=resolution, mode="w")

    group = zarr.open_group(session.store, path=resolution, mode="r+")
    classes = metadata.load_bundled_classes()
    group.create_array(
        config.DATA_VAR_NAME,
        shape=(len(config.YEARS[resolution]), grid.height, grid.width),
        chunks=enc.chunks,
        shards=enc.shards,
        dtype="uint8",
        fill_value=enc.fill_value,
        compressors=[zarr.codecs.ZstdCodec(level=enc.zstd_level)],
        dimension_names=(config.APPEND_DIM, "y", "x"),
        attributes=metadata.crop_type_attrs(classes),
    )


def init_store(session: Session, resolutions: list[Resolution] | None = None) -> None:
    """Write the full empty structure (root attrs + both groups). No commit."""
    for resolution in resolutions or ["30m", "10m"]:
        init_group(session, resolution)
    root = zarr.open_group(session.store, mode="a")
    root.attrs.update(config.ROOT_ATTRS)
