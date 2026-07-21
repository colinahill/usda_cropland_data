"""Catalog of source files: which (resolution, year) products exist and where."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import config
from .config import Resolution


class SourceFile(BaseModel):
    """One national CDL zip for a (resolution, year)."""

    model_config = ConfigDict(frozen=True)

    resolution: Resolution
    year: int

    @property
    def name(self) -> str:
        return f"{self.year}_{self.resolution}_cdls"

    @property
    def url(self) -> str:
        return config.SOURCE_URL_TEMPLATE.format(year=self.year, resolution=self.resolution)

    @property
    def tif_name(self) -> str:
        return f"{self.name}.tif"

    def zip_path(self, data_dir: Path) -> Path:
        return data_dir / f"{self.name}.zip"

    def extract_dir(self, data_dir: Path) -> Path:
        return data_dir / self.name

    def tif_path(self, data_dir: Path) -> Path:
        return self.extract_dir(data_dir) / self.tif_name


def source_files(resolution: Resolution, years: list[int] | None = None) -> list[SourceFile]:
    available = config.YEARS[resolution]
    if years is None:
        years = available
    unknown = sorted(set(years) - set(available))
    if unknown:
        raise ValueError(
            f"No known national {resolution} CDL for year(s) {unknown}. "
            f"Known years: {available[0]}-{available[-1]}. If NASS has released a new "
            "year, add it to usda_cdl.config.YEARS."
        )
    return [SourceFile(resolution=resolution, year=y) for y in sorted(years)]


def parse_years(spec: str) -> list[int]:
    """Parse '2020', '2008-2012', or '2008,2010,2012-2014'."""
    years: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            years.update(range(int(lo), int(hi) + 1))
        elif part:
            years.add(int(part))
    return sorted(years)
