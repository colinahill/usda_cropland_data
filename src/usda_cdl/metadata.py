"""Crop-class table handling and CF attribute construction.

The authoritative integer->class mapping ships inside each CDL zip as a raster
attribute table (``.tif.vat.dbf`` in recent years, ERDAS ``.aux`` earlier).
A static copy extracted from the 2025 VAT is bundled with this package
(``cdl_classes.json``); per-file VATs are preferred when present.
"""

from __future__ import annotations

import json
import re
import struct
from importlib import resources
from pathlib import Path

from . import config

# Code 81 is a real class ("Clouds/No Data"), distinct from fill/background 0.
CLOUDS_NO_DATA_CODE = 81


def load_bundled_classes() -> dict[int, dict[str, str]]:
    """{code: {"name": ..., "color": "#RRGGBB"}} from the bundled table."""
    text = resources.files("usda_cdl").joinpath("cdl_classes.json").read_text()
    payload = json.loads(text)
    return {int(k): v for k, v in payload["classes"].items()}


def read_vat_dbf(path: str | Path) -> dict[int, dict[str, str]]:
    """Parse a CDL ``.tif.vat.dbf`` raster attribute table.

    Minimal dBASE reader: the CDL VATs are tiny (256 fixed-width records) and a
    dependency-free parser keeps the pipeline lean.
    """
    data = Path(path).read_bytes()
    nrec = struct.unpack("<I", data[4:8])[0]
    hdrlen = struct.unpack("<H", data[8:10])[0]
    reclen = struct.unpack("<H", data[10:12])[0]

    fields: list[tuple[str, int]] = []
    off = 32
    while data[off] != 0x0D:
        name = data[off : off + 11].split(b"\x00")[0].decode()
        flen = data[off + 16]
        fields.append((name, flen))
        off += 32

    classes: dict[int, dict[str, str]] = {}
    pos = hdrlen
    for _ in range(nrec):
        rec = data[pos : pos + reclen]
        pos += reclen
        vals: dict[str, str] = {}
        o = 1  # first byte is the deletion flag
        for name, flen in fields:
            vals[name] = rec[o : o + flen].decode("latin1").strip()
            o += flen
        if vals.get("Class_Name"):
            code = int(vals["Value"])
            classes[code] = {
                "name": vals["Class_Name"],
                "color": "#{:02X}{:02X}{:02X}".format(int(vals["Red"]), int(vals["Green"]), int(vals["Blue"])),
            }
    return classes


def classes_for_tif(tif_path: str | Path) -> dict[int, dict[str, str]]:
    """Best-available class table for a source file.

    Prefers the sibling ``.tif.vat.dbf`` (authoritative for that year); falls
    back to the bundled 2025 table (class codes are stable across years).
    """
    vat = Path(str(tif_path) + ".vat.dbf")
    if vat.exists():
        return read_vat_dbf(vat)
    return load_bundled_classes()


def _cf_sanitize(name: str) -> str:
    """CF flag_meanings tokens: lowercase, blank-separated, no special chars."""
    token = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()
    return token or "unknown"


def crop_type_attrs(classes: dict[int, dict[str, str]]) -> dict:
    """CF-convention attrs for the categorical crop_type variable."""
    codes = sorted(classes)
    return {
        "long_name": "USDA NASS Cropland Data Layer crop and land cover classification",
        "flag_values": codes,
        "flag_meanings": " ".join(_cf_sanitize(classes[c]["name"]) for c in codes),
        "class_names": {str(c): classes[c]["name"] for c in codes},
        "class_colors": {str(c): classes[c]["color"] for c in codes},
        # NOTE: deliberately no missing_value/_FillValue attr - it would make
        # xarray mask code 0 to NaN and upcast the categorical uint8 to float.
        "grid_mapping": "spatial_ref",
        "coordinates": "spatial_ref",
        "comment": (
            "Pixel values are categorical class codes (see class_names / "
            "flag_meanings). 0 = Background: outside the classified area for that "
            "year, also used as the zarr fill_value. 81 = Clouds/No Data is a real "
            "class within the classified area, distinct from Background."
        ),
    }


def spatial_ref_attrs(grid: config.GridSpec) -> dict:
    """CF grid-mapping attrs for the scalar spatial_ref variable (rioxarray style)."""
    from pyproj import CRS

    crs = CRS.from_epsg(config.EPSG)
    attrs = crs.to_cf()
    attrs["spatial_ref"] = attrs.get("crs_wkt", crs.to_wkt())  # rioxarray compatibility
    attrs["GeoTransform"] = grid.geotransform
    return attrs


def coordinate_attrs() -> dict[str, dict]:
    return {
        "x": {
            "standard_name": "projection_x_coordinate",
            "long_name": "x coordinate of projection (pixel centre)",
            "units": "m",
            "axis": "X",
        },
        "y": {
            "standard_name": "projection_y_coordinate",
            "long_name": "y coordinate of projection (pixel centre)",
            "units": "m",
            "axis": "Y",
        },
        config.APPEND_DIM: {
            "long_name": "CDL product year",
            "axis": "T",
        },
    }
