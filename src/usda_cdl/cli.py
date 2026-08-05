"""Command-line interface.

Examples:
    # Source Coop product (`source-coop login` first, or pass --credentials-file)
    usda-cdl init-store --source-coop-account my-account
    usda-cdl ingest --source-coop-account my-account --resolution 30m --years 2008-2025
    usda-cdl validate --source-coop-account my-account --resolution 30m --years 2025

    # local development store
    usda-cdl init-store --store ./cdl_store
    usda-cdl ingest --store ./cdl_store --resolution 30m --years 2025 --workers 8
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
import zarr

from . import catalog, config, download, ingest, store, template

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
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="re-ingest years that are already in the store without asking")
    ] = False,
):
    """Download source zip(s) and write them into the store, one commit per year.

    Years already in the store are skipped unless confirmed interactively or
    --overwrite is passed (a re-ingest commits new data and tags it with a
    -rN suffix; existing tags are immutable and keep their snapshots).
    """
    storage = _resolve_storage(store_uri, account, credentials_file)
    repo = store.open_repo(storage)
    year_list = catalog.parse_years(years) if years else None
    sources = catalog.source_files(resolution, year_list)  # type: ignore[arg-type]

    for source in sources:
        log.info("=== %s %s ===", source.resolution, source.year)

        existing = _existing_ingest(repo, source.resolution, source.year)
        if existing and not overwrite:
            if sys.stdin.isatty() and sys.stdout.isatty():
                if not typer.confirm(
                    f"{source.name} is already in the store ({existing}). "
                    "Re-ingest? (new commit, tag gets a -rN suffix)"
                ):
                    log.info("skipping %s", source.name)
                    continue
            else:
                log.warning(
                    "%s already in the store (%s); skipping. Pass --overwrite to re-ingest.",
                    source.name,
                    existing,
                )
                continue

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
        tag = _tag_ingest(repo, f"{source.resolution}-{source.year}", snapshot)
        log.info("committed %s as %s (tag %s)", source.name, snapshot, tag)

        if cleanup:
            download.cleanup(source, data_dir)


def _existing_ingest(repo, resolution: str, year: int) -> str | None:
    """Return a description of the prior ingest of (resolution, year), if any.

    Checks the ingest tag first, then the group's provenance attrs (covers a
    commit whose tagging step failed).
    """
    import icechunk
    import zarr

    tag = f"{resolution}-{year}"
    try:
        snapshot = repo.lookup_tag(tag)
        return f"tag {tag} -> {snapshot}"
    except icechunk.IcechunkError:
        pass
    group = zarr.open_group(repo.readonly_session("main").store, path=resolution, mode="r")
    if str(year) in group.attrs.get("source_files", {}):
        return "provenance record in group attrs"
    return None


def _tag_ingest(repo, base: str, snapshot: str) -> str:
    """Tag this ingest's snapshot.

    Icechunk tags are immutable and their names can never be reused (even after
    deletion), so NEVER delete a tag: a re-ingest of the same year gets a
    ``-r2``/``-r3``... suffix instead, and older tags keep pointing at the
    snapshots they were created for.
    """
    import icechunk

    last_error = None
    for attempt in range(1, 100):
        name = base if attempt == 1 else f"{base}-r{attempt}"
        try:
            repo.create_tag(name, snapshot_id=snapshot)
            if attempt > 1:
                log.warning("tag %s already used; tagged re-ingest as %s", base, name)
            return name
        except icechunk.IcechunkError as exc:
            last_error = exc
            if "already exist" not in str(exc) and "reuse" not in str(exc):
                raise
    raise RuntimeError(f"could not create a tag for {base}") from last_error


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
def overviews(
    store_uri: StoreOpt = None,
    account: AccountOpt = None,
    credentials_file: CredsOpt = None,
    resolution: Annotated[str, typer.Option()] = "10m",
    years: Annotated[str, typer.Option(help="default: all years in the store")] = "",
    workers: Annotated[int, typer.Option()] = 8,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="regenerate years already done")] = False,
):
    """Build multiscale overview pyramids (mode-resampled from native; additive)."""
    from . import overviews as overviews_mod

    storage = _resolve_storage(store_uri, account, credentials_file)
    repo = store.open_repo(storage)

    session = repo.writable_session("main")
    if not overviews_mod.overviews_initialized(session, resolution):  # type: ignore[arg-type]
        overviews_mod.init_overviews(session, resolution)  # type: ignore[arg-type]
        factors = config.OVERVIEW_FACTORS[resolution]  # type: ignore[index]
        snapshot = session.commit(f"Initialize {resolution} overview levels {factors}")
        log.info("initialized %s overview structure at %s", resolution, snapshot)

    year_list = catalog.parse_years(years) if years else config.YEARS[resolution]  # type: ignore[index]
    for year in year_list:
        session = repo.writable_session("main")
        group = zarr.open_group(session.store, path=resolution, mode="r+")
        done = list(group.attrs.get("overview_years", []))
        if year in done and not overwrite:
            log.info("%s %s overviews already generated; skipping (--overwrite to redo)", resolution, year)
            continue
        stats = overviews_mod.generate_year(session, resolution, year, workers=workers)  # type: ignore[arg-type]
        group.attrs["overview_years"] = sorted({*done, year})
        snapshot = session.commit(f"Overviews {resolution} {year}", metadata=stats)
        log.info("committed %s %s overviews as %s", resolution, year, snapshot)


@app.command()
def publish_readme(
    account: Annotated[str, typer.Option("--source-coop-account")] = store.SOURCE_COOP_ACCOUNT,
    credentials_file: CredsOpt = None,
):
    """Upload product/README.md to the product root (the Source Coop landing page)."""
    from . import remote

    remote.upload_readme(account, credentials_file=credentials_file)


@app.command()
def clean_remote_store(
    account: Annotated[str, typer.Option("--source-coop-account")] = store.SOURCE_COOP_ACCOUNT,
    credentials_file: CredsOpt = None,
    workers: Annotated[int, typer.Option(help="parallel deletes")] = 8,
):
    """DESTRUCTIVE: delete every object in the remote store prefix.

    Only needed to abandon a version path - ingesting a year is additive and
    never requires this. Asks for two confirmations, interactive terminal only.
    """
    from . import remote

    prefix = f"s3://{account}/{store.PRODUCT_NAME}/{store.STORE_SUBPATH}"
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise typer.BadParameter(f"clean-remote-store refuses to run non-interactively (target {prefix})")

    s3 = remote.client(credentials_file)
    keys, total = remote.store_keys(account, s3=s3)
    if not keys:
        log.info("%s is already empty", prefix)
        return

    typer.echo(f"\nTarget: {prefix}\n  {len(keys)} objects, {total / 1e9:.2f} GB")
    typer.echo("This deletes the published store: all data, snapshot history, and tags.\n")
    if not typer.confirm(f"Delete all {len(keys)} objects under {prefix}?"):
        raise typer.Abort
    if typer.prompt("Confirm again by typing the store path exactly") != prefix:
        typer.echo("path did not match; nothing deleted")
        raise typer.Abort

    remote.delete_keys(account, keys, workers=workers, s3=s3)
    log.info("deleted %d objects under %s", len(keys), prefix)


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
