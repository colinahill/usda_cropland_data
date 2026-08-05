# USDA Cropland Data Layer

End-to-end pipeline that reformats the [USDA NASS Cropland Data Layer (CDL)](https://www.nass.usda.gov/Research_and_Science/Cropland/Release/index.php)
into a single [Icechunk](https://icechunk.io)-managed Zarr v3 store and publishes it as a
[Source Cooperative](https://source.coop) data product (`usda-cropland-data-layer`).

The store has two groups, one per spatial resolution:

| group | variable | years | grid (y × x) | source |
|---|---|---|---|---|
| `30m` | `crop_type (year, y, x) uint8` | 2008–2025 | 105,432 × 160,171 | national 30 m zips (2024+ are resampled from the native 10 m CDL) |
| `10m` | `crop_type (year, y, x) uint8` | 2024–2025 | 316,295 × 480,509 | native 10 m national zips |

Both grids are EPSG:5070 (CONUS Albers Equal Area), pixel-centre coordinates, fill value
`0` = Background. Class codes, names, and official colors are embedded in the
`crop_type` attrs (`flag_values` / `flag_meanings` / `class_names` / `class_colors`),
along with citation, license, provenance (per-year source URL + sha256), and methodology
notes. Yearly source extents differ; each year is placed by affine offset into a
canonical union grid (see `usda_cdl/config.py`).

## Setup

```bash
uv sync
uv run pytest          # unit + integration tests (tiny synthetic rasters)
```

## Usage

All commands are wrapped in the Makefile (`make help` lists everything):

```bash
make init-store                          # create the empty store structure (local dev store at ./cdl_store_local)
make ingest RESOLUTION=30m YEARS=2025    # download source zip(s) and write them into the store, one commit per year
make validate RESOLUTION=30m YEARS=2025  # verify store pixels against the source rasters + structure/attr checks
make info                                # show store structure, tags, and recent snapshots
make backfill-30m                        # ingest all 30m years 2008-2025
make backfill-10m                        # ingest 2024-2025 (add CLEANUP=1 to delete the ~10 GB sources as it goes)
make overviews RESOLUTION=10m            # build multiscale pyramid levels (mode-resampled from native)
```

Every target takes `ACCOUNT=chill` to commit **directly into the published Source Coop
store** (see below), or `STORE=<local path|s3://bucket/prefix>` for anywhere else.
`STORE` defaults to `./cdl_store_local`, so a bare `make ingest` writes to a local dev
store and can never touch the published product by accident. The underlying CLI is
available directly as `uv run usda-cdl`:

```bash
uv run usda-cdl init-store --store ./cdl_store_local
uv run usda-cdl ingest   --store ./cdl_store_local --resolution 30m --years 2025
uv run usda-cdl validate --store ./cdl_store_local --resolution 30m --years 2025
uv run usda-cdl info     --store ./cdl_store_local
```

`ingest` downloads the national zip into `data/` (cached, atomic, retried), extracts the
GeoTIFF, validates it against the canonical grid, writes shard-aligned windows through a
thread pool, and makes **one icechunk commit per year** tagged `{resolution}-{year}`.

The arrays use zarr v3 sharding: inner chunks `(1, 512, 512)` (~39 KB compressed — cheap
field/point reads via range requests) packed into `(1, 8192, 8192)` shards (~260 storage
objects per 30m year). See `EncodingSpec` in `usda_cdl/config.py`.

`overviews` adds [zarr multiscales](https://github.com/zarr-conventions/multiscales)
pyramid levels as child groups (`10m/2x` … `10m/512x`, `30m/2x` … `30m/256x`; 2× per level), 
block-**mode** resampled from the native array. Re-running a year is idempotent. 

`--cleanup` deletes source files after each year.

## Publishing to Source Coop

See the official [data upload docs](https://docs.source.coop/data-upload).

Ingest **commits directly into the remote store**: pass `ACCOUNT=chill` and each year is
one icechunk commit against the published product. Interrupt anything and no commit
lands, so readers only ever see complete versions.

1. Create the data product `usda-cropland-data-layer` on [source.coop](https://source.coop).
2. Authenticate with the [source-coop CLI](https://github.com/source-cooperative/source-coop-cli)
   (`brew install source-cooperative/tap/source-coop`, then `source-coop login` — browser
   OAuth, credentials cached in the OS keyring and picked up automatically; icechunk
   re-reads them on every refresh, so multi-hour backfills survive credential rotation).
   Alternative: save the product page's JSON credential export as `creds.json`
   (gitignored) and pass `CREDS_FILE=creds.json`.
3. Ingest and validate against the product:

```bash
make init-store ACCOUNT=chill                   # once per version path
make backfill-30m ACCOUNT=chill                 # each year: download -> write -> commit -> tag
make backfill-10m ACCOUNT=chill CLEANUP=1
make overviews ACCOUNT=chill RESOLUTION=30m
make validate ACCOUNT=chill RESOLUTION=30m      # reads back through data.source.coop
```

4. `make publish-readme ACCOUNT=chill` uploads `product/README.md` to the product root
   (the landing page, which sits outside the store prefix). Verify the anonymous read
   snippet in that README works, then set the product to **Listed**.

Yearly updates: `make ingest ACCOUNT=chill RESOLUTION=30m YEARS=<year>`. Commits are
additive, so an update only writes the new year's objects.

Remote writes are network-bound: measured ~58 MiB/s of raw pixels at both 2 and 8
writer threads, i.e. roughly 5 minutes for a 30m year and ~45 minutes for a 10m year
(versus ~13 s to a local store). `WORKERS=8` is a reasonable default.

`make clean-remote-store` deletes every object under the remote store prefix — data,
snapshot history, and tags. It exists only for abandoning a version path, asks for two
separate confirmations, and refuses to run non-interactively. Normal operation never
needs it.

For a staging run, build locally first (`make init-store ingest`, default
`STORE=./cdl_store_local`) and inspect it with `make info` / `make validate`. A local
store is only ever a staging target — nothing copies one up to the product, so the
remote store is the source of truth for published data.

## Reading the published dataset

See `product/README.md` for consumer snippets.

## Repo layout

```
src/usda_cdl/
  config.py     # canonical grids, chunking, dataset attrs (structural changes = code diffs)
  catalog.py    # which (resolution, year) products exist, URL patterns
  download.py   # cached/atomic/retried zip download + extraction
  metadata.py   # class table (VAT) parsing, CF attrs, CRS attrs
  template.py   # empty store structure (groups, coords, arrays)
  ingest.py     # windowed read -> shard-aligned zarr writes (no commit; caller owns the session)
  store.py      # icechunk storage factory (local / s3 / source coop, refreshable creds)
  remote.py     # the plain-S3 bits icechunk doesn't cover: landing-page README, store wipe
  overviews.py  # multiscale pyramids: GeoZarr multiscales attrs + block-mode generation
  validate.py   # pixel-equality sampling vs source, structure checks
  cli.py        # typer CLI
  cdl_classes.json  # bundled class table (extracted from the 2025 VAT)
product/README.md   # Source Coop product landing page
tests/              # synthetic-raster integration tests
```

## Data notes / gotchas

- **Grid extents vary by year.** The 2025 30m file is larger than the 2008–2024 grid;
  the canonical grids in `config.py` are unions. If NASS expands the extent again,
  `ingest` fails with instructions rather than writing misaligned data.
- Both canonical grids are verified against the actual source GeoTIFFs.
- Code `0` (Background) is the fill value and marks "outside this year's classified
  extent"; code `81` (Clouds/No Data) is a real class inside the classified extent.
- `crop_type` deliberately has no `missing_value`/`_FillValue` attr so xarray keeps the
  categorical uint8 dtype instead of masking to float.
- 30m products for 2024+ are nearest-neighbour resamples of the native 10m CDL
  (methodology break recorded in group attrs).

## License

The pipeline code is [MIT licensed](LICENSE). The CDL data itself is US public domain
(see `product/README.md` for the data license and citation).
