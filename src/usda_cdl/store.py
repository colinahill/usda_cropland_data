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
import shutil
import subprocess
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
# `usda-cdl publish` (immutable files first, mutable "repo" pointer last).
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
    elif credentials_file or shutil.which("source-coop"):
        kwargs["get_credentials"] = _RefreshableCredentials(credentials_file)
    else:
        kwargs["from_env"] = True
    return icechunk.s3_storage(**kwargs)


def load_credentials(credentials_file: str | None = None) -> dict[str, str | None]:
    """Source Coop credentials, in preference order:

    1. ``credentials_file`` if given and present - Source Coop's "JSON (SDK)"
       export: {"aws_access_key_id": ..., "aws_secret_access_key": ..., ...}
    2. the ``source-coop`` CLI's cached browser login (``source-coop login``),
       which prints AWS credential_process JSON.
    """
    if credentials_file and Path(credentials_file).exists():
        creds = json.loads(Path(credentials_file).read_text())
        return {
            "access_key_id": creds["aws_access_key_id"],
            "secret_access_key": creds["aws_secret_access_key"],
            "session_token": creds.get("aws_session_token"),
        }
    cli = shutil.which("source-coop")
    if cli is None:
        raise RuntimeError(
            "No Source Coop credentials: pass --credentials-file with the product's JSON "
            "credential export, or install the source-coop CLI "
            "(brew install source-cooperative/tap/source-coop) and run `source-coop login`."
        )
    proc = subprocess.run([cli, "creds", "--format", "credential-process"], capture_output=True, text=True)
    try:
        creds = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # the CLI exits 0 even without cached credentials; detect via non-JSON output
        message = (proc.stdout + proc.stderr).strip() or "no output"
        raise RuntimeError(f"source-coop creds failed ({message}); run `source-coop login`") from None
    return {
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretAccessKey"],
        "session_token": creds.get("SessionToken"),
    }


class _RefreshableCredentials:
    """get_credentials callable for icechunk that re-resolves credentials on
    each refresh, via load_credentials (JSON file or source-coop CLI cache).

    A module-level class (rather than a closure) because icechunk pickles the
    callable.
    """

    def __init__(self, credentials_file: str | None):
        self.credentials_file = credentials_file

    def __call__(self) -> icechunk.S3StaticCredentials:
        creds = load_credentials(self.credentials_file)
        return icechunk.S3StaticCredentials(
            access_key_id=creds["access_key_id"],
            secret_access_key=creds["secret_access_key"],
            session_token=creds["session_token"],
            expires_after=datetime.now(UTC) + timedelta(minutes=15),
        )


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
            kwargs["get_credentials"] = _RefreshableCredentials(credentials_file)
        else:
            kwargs["from_env"] = True
        return icechunk.s3_storage(**kwargs)
    return icechunk.local_filesystem_storage(str(Path(uri).expanduser()))


def open_repo(storage: icechunk.Storage, *, create: bool = False) -> icechunk.Repository:
    if create:
        return icechunk.Repository.open_or_create(storage)
    return icechunk.Repository.open(storage)
