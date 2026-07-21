"""Icechunk storage/repository helpers.

Supported store targets:
- local path (development / staging)
- any ``s3://bucket/prefix`` (uses the standard AWS credential chain)
- Source Cooperative product prefix (helper that fills in the well-known bucket)

Source Coop writes use temporary scoped STS credentials issued from the product
page. Export them as env vars (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
AWS_SESSION_TOKEN) and ``from_env=True`` picks them up; for jobs that outlive
one credential set, pass ``credentials_file`` pointing at a JSON file you can
refresh out-of-band - it is re-read whenever icechunk asks for credentials.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import icechunk

from . import config

# Source Coop S3-compatible access: bucket = the account name, prefix = the
# product name. These values (and the region) come from the product's
# credentials page - that page is the ground truth if they ever change.
# See https://docs.source.coop/data-upload
#
# NOTE (verified 2026-07-21): data.source.coop supports plain GET/PUT/DELETE but
# NOT S3 CopyObject (and it ignores If-Match preconditions), so icechunk commits
# fail against it with "service error" at update_repo_info. Reads work fine.
# Publishing therefore builds the store locally and syncs it up with
# scripts/publish.sh (immutable files first, mutable "repo" pointer last).
SOURCE_COOP_ENDPOINT = "https://data.source.coop"
SOURCE_COOP_REGION = "us-east-1"
SOURCE_COOP_ACCOUNT = "chill"
PRODUCT_NAME = "usda-cropland-data-layer"
# Versioned store directory (community convention, cf. dynamical.org's
# `<dataset>/v0.2.0.icechunk/`): breaking structural changes get a new
# v{x.y.z}.icechunk path alongside the old one instead of breaking readers.
STORE_SUBPATH = f"v{config.DATASET_VERSION}.icechunk"


def source_coop_storage(
    account: str = SOURCE_COOP_ACCOUNT,
    product: str = PRODUCT_NAME,
    *,
    credentials_file: str | None = None,
    anonymous: bool = False,
) -> icechunk.Storage:
    """Icechunk storage for the Source Coop product (bucket=account, prefix=product)."""
    kwargs: dict = {
        "bucket": account,
        "prefix": f"{product}/{STORE_SUBPATH}",
        "region": SOURCE_COOP_REGION,
        "endpoint_url": SOURCE_COOP_ENDPOINT,
        "force_path_style": True,
    }
    if anonymous:
        kwargs["anonymous"] = True
    elif credentials_file:
        kwargs["get_credentials"] = _refreshable_credentials(credentials_file)
    else:
        kwargs["from_env"] = True
    return icechunk.s3_storage(**kwargs)


class _RefreshableCredentials:
    """get_credentials callable that re-reads a JSON creds file on each refresh.

    File format (matches Source Coop's "JSON (SDK)" credential export):
    {"aws_access_key_id": ..., "aws_secret_access_key": ..., "aws_session_token": ...}
    Refresh the file contents before the old credentials expire and long jobs
    keep writing without interruption.

    A module-level class (rather than a closure) because icechunk pickles the
    callable.
    """

    def __init__(self, credentials_file: str):
        self.credentials_file = credentials_file

    def __call__(self) -> icechunk.S3StaticCredentials:
        creds = json.loads(Path(self.credentials_file).read_text())
        return icechunk.S3StaticCredentials(
            access_key_id=creds["aws_access_key_id"],
            secret_access_key=creds["aws_secret_access_key"],
            session_token=creds.get("aws_session_token"),
            expires_after=datetime.now(UTC) + timedelta(minutes=15),
        )


def _refreshable_credentials(credentials_file: str) -> _RefreshableCredentials:
    return _RefreshableCredentials(credentials_file)


def storage_from_uri(
    uri: str,
    *,
    region: str = SOURCE_COOP_REGION,
    credentials_file: str | None = None,
    anonymous: bool = False,
) -> icechunk.Storage:
    """Resolve a store URI to icechunk Storage.

    ``uri`` is either a local path or ``s3://bucket/prefix``.
    """
    if uri.startswith("s3://"):
        bucket, _, prefix = uri.removeprefix("s3://").partition("/")
        kwargs: dict = {"bucket": bucket, "prefix": prefix, "region": region}
        if anonymous:
            kwargs["anonymous"] = True
        elif credentials_file:
            kwargs["get_credentials"] = _refreshable_credentials(credentials_file)
        else:
            kwargs["from_env"] = True
        return icechunk.s3_storage(**kwargs)
    return icechunk.local_filesystem_storage(str(Path(uri).expanduser()))


def open_repo(storage: icechunk.Storage, *, create: bool = False) -> icechunk.Repository:
    if create:
        return icechunk.Repository.open_or_create(storage)
    return icechunk.Repository.open(storage)
