"""Command-line interface.

Examples:
    # local development store
    usda-cdl init-store --store ./cdl_store
    usda-cdl ingest --store ./cdl_store --resolution 30m --years 2025 --workers 8

    # Source Coop (export the product's temporary credentials first)
    usda-cdl init-store --source-coop-account my-account
    usda-cdl ingest --source-coop-account my-account --resolution 30m --years 2008-2025
    usda-cdl validate --source-coop-account my-account --resolution 30m --years 2025
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from . import catalog, download, ingest, store, template

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("usda_cdl")

StoreOpt = Annotated[
    str | None,
    typer.Option("--store", help="Store target: local path or s3://bucket/prefix"),
]
AccountOpt = Annotated[
    str | None,
    typer.Option("--source-coop-account", help=f"Source Coop account; targets the '{store.PRODUCT_NAME}' product"),
]
CredsOpt = Annotated[
    str | None,
    typer.Option(
        "--credentials-file", help="JSON credentials file, re-read on refresh (Source Coop 'JSON (SDK)' export format)"
    ),
]


def _resolve_storage(store_uri: str | None, account: str | None, credentials_file: str | None):
    if bool(store_uri) == bool(account):
        raise typer.BadParameter("provide exactly one of --store or --source-coop-account")
    if account:
        log.info("store: source coop s3://%s/%s/%s", account, store.PRODUCT_NAME, store.STORE_SUBPATH)
        return store.source_coop_storage(account, credentials_file=credentials_file)
    log.info("store: %s", store_uri)
    return store.storage_from_uri(store_uri, credentials_file=credentials_file)


@app.command()
def init_store(
    store_uri: StoreOpt = None,
    account: AccountOpt = None,
    credentials_file: CredsOpt = None,
    resolutions: Annotated[str, typer.Option(help="comma-separated groups")] = "30m,10m",
):
    """Create the icechunk repo and empty group structure."""
    storage = _resolve_storage(store_uri, account, credentials_file)
    repo = store.open_repo(storage, create=True)
    session = repo.writable_session("main")
    res_list = [r.strip() for r in resolutions.split(",")]
    template.init_store(session, res_list)  # type: ignore[arg-type]
    snapshot = session.commit(f"Initialize store structure: groups {res_list}")
    log.info("initialized store at snapshot %s", snapshot)


def ingest_cmd(
    store_uri: StoreOpt = None,
    account: AccountOpt = None,
    credentials_file: CredsOpt = None,
    resolution: Annotated[str, typer.Option()] = "30m",
    years: Annotated[str, typer.Option(help="e.g. '2025' or '2008-2025' or '2008,2010'")] = "",
    data_dir: Annotated[Path, typer.Option(help="download/extract cache dir")] = Path("data"),
    workers: Annotated[int, typer.Option()] = 8,
    cleanup: Annotated[bool, typer.Option(help="delete source files after ingest")] = False,
):
    """Download source zip(s) and write them into the store, one commit per year."""
    storage = _resolve_storage(store_uri, account, credentials_file)
    repo = store.open_repo(storage)
    year_list = catalog.parse_years(years) if years else None
    sources = catalog.source_files(resolution, year_list)  # type: ignore[arg-type]

    for source in sources:
        log.info("=== %s %s ===", source.resolution, source.year)
        tif_path, provenance = download.fetch(source, data_dir)

        session = repo.writable_session("main")
        stats = ingest.ingest_year(session, source.resolution, source.year, tif_path, workers=workers)
        ingest.record_year_provenance(
            session,
            source.resolution,
            source.year,
            provenance
            | {
                "source_shape": stats["source_shape"],
                "placement_offset": stats["placement_offset"],
            },
        )
        snapshot = session.commit(
            f"Ingest {source.resolution} CDL {source.year}",
            metadata=provenance | stats,
        )
        _retag(repo, f"{source.resolution}-{source.year}", snapshot)
        log.info("committed %s as %s", source.name, snapshot)

        if cleanup:
            download.cleanup(source, data_dir)


def _retag(repo, tag: str, snapshot: str) -> None:
    try:
        repo.create_tag(tag, snapshot_id=snapshot)
    except Exception:  # tag exists (re-ingest): move it
        repo.delete_tag(tag)
        repo.create_tag(tag, snapshot_id=snapshot)


@app.command()
def validate(
    store_uri: StoreOpt = None,
    account: AccountOpt = None,
    credentials_file: CredsOpt = None,
    resolution: Annotated[str, typer.Option()] = "30m",
    years: Annotated[str, typer.Option()] = "",
    data_dir: Annotated[Path, typer.Option()] = Path("data"),
    samples: Annotated[int, typer.Option(help="random windows compared against source")] = 12,
):
    """Verify store contents against the source rasters."""
    from . import validate as validate_mod

    storage = _resolve_storage(store_uri, account, credentials_file)
    repo = store.open_repo(storage)
    year_list = catalog.parse_years(years) if years else None
    sources = catalog.source_files(resolution, year_list)  # type: ignore[arg-type]
    failures = 0
    for source in sources:
        tif = source.tif_path(data_dir)
        results = validate_mod.validate_year(
            repo,
            source.resolution,
            source.year,
            tif_path=tif if tif.exists() else None,
            samples=samples,
        )
        for result in results:
            level = logging.INFO if result.passed else logging.ERROR
            log.log(level, "%s %s: %s", source.name, "PASS" if result.passed else "FAIL", result.message)
            failures += not result.passed
    if failures:
        raise typer.Exit(code=1)
    log.info("all validations passed")


@app.command()
def info(
    store_uri: StoreOpt = None,
    account: AccountOpt = None,
    credentials_file: CredsOpt = None,
):
    """Show store structure, snapshots, and tags."""
    import xarray as xr

    storage = _resolve_storage(store_uri, account, credentials_file)
    repo = store.open_repo(storage)
    session = repo.readonly_session("main")
    for resolution in ("30m", "10m"):
        try:
            ds = xr.open_zarr(session.store, group=resolution, chunks=None)
        except (KeyError, FileNotFoundError):
            continue
        typer.echo(f"--- group {resolution} ---")
        typer.echo(str(ds))
    typer.echo("--- tags ---")
    for tag in sorted(repo.list_tags()):
        typer.echo(tag)
    typer.echo("--- recent snapshots ---")
    for snap in list(repo.ancestry(branch="main"))[:10]:
        typer.echo(f"{snap.id}  {snap.written_at:%Y-%m-%d %H:%M}  {snap.message}")


# typer uses function names with underscores -> dashes; keep `ingest` name clean
app.command("ingest")(ingest_cmd)

if __name__ == "__main__":
    app()
