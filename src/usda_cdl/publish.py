"""Publish the locally built icechunk store to Source Coop.

data.source.coop does not support icechunk's commit protocol (no CopyObject,
no multipart upload, If-Match ignored), so publishing is a plain file sync of
the local store: the immutable content-addressed files go up first, and the
mutable ``repo`` pointer file goes up LAST - readers always see either the old
complete version or the new complete one. All uploads are single-request PUTs
(boto3 ``put_object`` never uses multipart; the largest shard objects are well
under the 5 GB single-PUT limit).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import botocore.session
from botocore.client import Config
from botocore.credentials import RefreshableCredentials
from tqdm import tqdm

from . import store as store_config

log = logging.getLogger(__name__)

REPO_POINTER = "repo"  # icechunk's only mutable file
PRODUCT_README = Path("product/README.md")


def _creds_metadata(credentials_file: str | None) -> dict:
    """Credentials in botocore RefreshableCredentials metadata form.

    The advertised expiry makes botocore re-invoke this every ~15 minutes, so a
    long upload picks up a refreshed creds.json / source-coop CLI login instead
    of failing with ExpiredToken when the original STS credentials lapse.
    """
    creds = store_config.load_credentials(credentials_file)
    return {
        "access_key": creds["access_key_id"],
        "secret_key": creds["secret_access_key"],
        "token": creds["session_token"],
        "expiry_time": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    }


def _client(credentials_file: str | None):
    refreshable = RefreshableCredentials.create_from_metadata(
        metadata=_creds_metadata(credentials_file),
        refresh_using=lambda: _creds_metadata(credentials_file),
        method="source-coop",
    )
    session = botocore.session.get_session()
    session._credentials = refreshable
    return boto3.Session(botocore_session=session).client(
        "s3",
        endpoint_url=store_config.SOURCE_COOP_ENDPOINT,
        region_name=store_config.SOURCE_COOP_REGION,
        config=Config(
            s3={"addressing_style": "path"},
            # "standard" rather than "adaptive": adaptive's client-side rate
            # limiter can throttle all workers to a crawl after a few 5xx/429s,
            # which looks like a hang.
            retries={"max_attempts": 10, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    )


def _remote_sizes(s3, bucket: str, prefix: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            sizes[obj["Key"]] = obj["Size"]
    return sizes


def _delete_remote(s3, bucket: str, keys: list[str], workers: int = 8) -> None:
    # one DeleteObject per key: data.source.coop does not implement the batch
    # DeleteObjects operation (returns NoSuchBucket for it)
    with (
        tqdm(total=len(keys), desc="deleting remote store", unit="obj") as bar,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        futures = [pool.submit(s3.delete_object, Bucket=bucket, Key=key) for key in keys]
        for future in as_completed(futures):
            future.result()
            bar.update(1)


def plan_uploads(local_store: Path, remote_sizes: dict[str, int], prefix: str) -> list[tuple[Path, str, int]]:
    """(path, key, size) for every immutable file that is missing or differs remotely.

    Store files are content-addressed and immutable, so same key + same size
    means already uploaded. The mutable repo pointer is excluded - it is always
    uploaded, last.
    """
    uploads = []
    for path in sorted(local_store.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_store).as_posix()
        if rel == REPO_POINTER:
            continue
        key = f"{prefix}/{rel}"
        size = path.stat().st_size
        if remote_sizes.get(key) != size:
            uploads.append((path, key, size))
    return uploads


def publish(
    local_store: Path,
    account: str,
    product: str = store_config.PRODUCT_NAME,
    *,
    credentials_file: str | None = None,
    overwrite: bool = False,
    workers: int = 4,
) -> None:
    if not (local_store / REPO_POINTER).exists():
        raise FileNotFoundError(f"{local_store} does not look like an icechunk store (no '{REPO_POINTER}' file)")

    bucket = account
    prefix = f"{product}/{store_config.STORE_SUBPATH}"
    s3 = _client(credentials_file)

    remote = _remote_sizes(s3, bucket, f"{prefix}/")
    if overwrite and remote:
        log.info("overwrite: deleting %d remote objects under %s/%s", len(remote), bucket, prefix)
        _delete_remote(s3, bucket, list(remote), workers=workers)
        remote = {}

    uploads = plan_uploads(local_store, remote, prefix)
    total_bytes = sum(size for _, _, size in uploads)
    log.info(
        "uploading %d files (%.1f GB) to s3://%s/%s (%d already up to date)",
        len(uploads),
        total_bytes / 1e9,
        bucket,
        prefix,
        sum(1 for p in local_store.rglob("*") if p.is_file()) - 1 - len(uploads),
    )

    def put(path: Path, key: str, size: int) -> int:
        s3.put_object(Bucket=bucket, Key=key, Body=path.read_bytes())
        return size

    pool = ThreadPoolExecutor(max_workers=workers)
    with tqdm(total=total_bytes, desc="uploading store", unit="B", unit_scale=True, unit_divisor=1024) as bar:
        futures = [pool.submit(put, *u) for u in uploads]
        try:
            for future in as_completed(futures):
                bar.update(future.result())
        except BaseException:
            # fail fast: drop the queued uploads instead of draining them
            # (re-running publish resumes from whatever completed)
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()

    log.info("uploading mutable '%s' pointer last", REPO_POINTER)
    put(local_store / REPO_POINTER, f"{prefix}/{REPO_POINTER}", 0)

    if PRODUCT_README.exists():
        s3.put_object(
            Bucket=bucket,
            Key=f"{product}/README.md",
            Body=PRODUCT_README.read_bytes(),
            ContentType="text/markdown",
        )
        log.info("uploaded product README")

    log.info("published s3://%s/%s", bucket, prefix)
