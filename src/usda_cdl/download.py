"""Download and extract CDL source zips.

Conventions (matching the pre-existing layout in ``data/``):
- zip lands at   ``{data_dir}/{year}_{res}_cdls.zip``
- extracted into ``{data_dir}/{year}_{res}_cdls/``

Downloads are atomic (temp file + rename), resumable across retries, cached on
disk, and verified by size against the server's Content-Length.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
import zipfile
from pathlib import Path

import httpx

from .catalog import SourceFile

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6


def _backoff(attempt: int) -> float:
    return min(2**attempt, 60) * (0.5 + random.random())


def download_zip(source: SourceFile, data_dir: Path, *, force: bool = False) -> Path:
    """Download the zip for ``source`` into ``data_dir`` (cached)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = source.zip_path(data_dir)
    if dest.exists() and not force:
        log.info("using cached %s", dest)
        return dest

    tmp = dest.with_suffix(f".tmp-{random.randbytes(4).hex()}")
    try:
        for attempt in range(MAX_ATTEMPTS):
            try:
                _stream_download(source.url, tmp)
                tmp.rename(dest)
                return dest
            except (httpx.HTTPError, OSError) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = _backoff(attempt)
                log.warning(
                    "download attempt %d/%d for %s failed (%s); retrying in %.0fs",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    source.url,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")
    finally:
        tmp.unlink(missing_ok=True)


def _stream_download(url: str, dest: Path) -> None:
    with (
        httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client,
        client.stream("GET", url) as resp,
    ):
        if resp.status_code in RETRYABLE_STATUS:
            raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
        resp.raise_for_status()
        expected = int(resp.headers.get("Content-Length", 0))
        written = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                written += len(chunk)
        if expected and written != expected:
            raise OSError(f"short read: got {written} of {expected} bytes for {url}")


def extract_zip(source: SourceFile, data_dir: Path) -> Path:
    """Extract the zip (if not already extracted) and return the .tif path."""
    tif = source.tif_path(data_dir)
    if tif.exists():
        return tif
    zip_path = source.zip_path(data_dir)
    out_dir = source.extract_dir(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("extracting %s -> %s", zip_path, out_dir)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            # Skip the large .ovr overview pyramids; we read the full-res band only.
            if member.filename.endswith(".ovr"):
                continue
            zf.extract(member, out_dir)
    if not tif.exists():
        raise FileNotFoundError(
            f"{source.tif_name} not found in {zip_path}; zip contents: "
            f"{[m.filename for m in zipfile.ZipFile(zip_path).infolist()]}"
        )
    return tif


def sha256_file(path: Path, chunk_size: int = 1 << 22) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(source: SourceFile, data_dir: Path) -> tuple[Path, dict]:
    """Download + extract; returns (tif_path, provenance dict for commit metadata)."""
    zip_path = download_zip(source, data_dir)
    tif = extract_zip(source, data_dir)
    provenance = {
        "source_url": source.url,
        "zip_size_bytes": zip_path.stat().st_size,
        "zip_sha256": sha256_file(zip_path),
    }
    return tif, provenance


def cleanup(source: SourceFile, data_dir: Path, *, keep_zip: bool = False) -> None:
    """Delete extracted files (and optionally the zip) after a successful ingest."""
    import shutil

    shutil.rmtree(source.extract_dir(data_dir), ignore_errors=True)
    if not keep_zip:
        source.zip_path(data_dir).unlink(missing_ok=True)
