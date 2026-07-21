# USDA Cropland Data Layer

End-to-end pipeline that reformats the [USDA NASS Cropland Data Layer (CDL)](https://www.nass.usda.gov/Research_and_Science/Cropland/Release/index.php)
into a single [Icechunk](https://icechunk.io)-managed Zarr v3 store and publishes it as a
[Source Cooperative](https://source.coop) data product (`usda-cropland-data-layer`).

The store has two groups, one per spatial resolution:

| group | variable | years | grid (y × x) | source |
|---|---|---|---|---|
| `30m` | `crop_type (year, y, x) uint8` | 2008–2025 | 105,432 × 160,171 | national 30 m zips (2024+ are resampled from the native 10 m CDL) |
| `10m` | `crop_type (year, y, x) uint8` | 2024–2025 | 316,296 × 480,513 | native 10 m national zips |

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
```

Target a different store with `STORE=s3://bucket/prefix` or the Source Coop product with
`ACCOUNT=chill` (the Source Coop account). The underlying CLI is available directly as
`uv run usda-cdl`:

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
Re-running a year is idempotent. `--cleanup` deletes source files after each year
(useful for the ~9–10 GB 10m zips).

## Publishing to Source Coop

See the official [data upload docs](https://docs.source.coop/data-upload).

The Source Coop endpoint (`data.source.coop`) does not support S3 server-side copy,
which icechunk commits require — so the store is **built locally, then synced up**
(`make publish` uploads the immutable files first and the mutable `repo` pointer
last, so readers always see a consistent version; `OVERWRITE=1` wipes the remote
store first, for use after a local rebuild).

1. Create the data product `usda-cropland-data-layer` on [source.coop](https://source.coop).
2. Authenticate with the [source-coop CLI](https://github.com/source-cooperative/source-coop-cli)
   (`brew install source-cooperative/tap/source-coop`, then `source-coop login` — browser
   OAuth, credentials cached in the OS keyring and picked up automatically). Alternative:
   save the product page's JSON credential export as `creds.json` (gitignored); an
   existing `creds.json` takes precedence.
3. Build locally and publish:

```bash
make init-store backfill-30m backfill-10m   # build the full local store
make publish ACCOUNT=chill CREDS_FILE=creds.json
make validate ACCOUNT=chill RESOLUTION=30m  # reads back through data.source.coop
```

4. Upload `product/README.md` to the product root, verify the anonymous read snippet in
   that README works, then set the product to **Listed**.

Yearly updates are the same flow: ingest the new year locally, `make publish` again —
sync only uploads the new objects.

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
- The 10m canonical grid is provisional: verified against the 2024 FGDC metadata and the
  2025 30m footprint; the first real 10m ingest asserts it.
- Code `0` (Background) is the fill value and marks "outside this year's classified
  extent"; code `81` (Clouds/No Data) is a real class inside the classified extent.
- `crop_type` deliberately has no `missing_value`/`_FillValue` attr so xarray keeps the
  categorical uint8 dtype instead of masking to float.
- 30m products for 2024+ are nearest-neighbour resamples of the native 10m CDL
  (methodology break recorded in group attrs).

## License

The pipeline code is [MIT licensed](LICENSE). The CDL data itself is US public domain
(see `product/README.md` for the data license and citation).
