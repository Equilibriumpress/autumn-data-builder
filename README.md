# Autumn Atlas Data Builder

Cloud-only builder for the worldwide offline foliage database used by Autumn Atlas. No local computer or Mac is required.

## What it does

The production workflow divides the useful global land extent into 126 independent 20° × 20° shards. Each Ubuntu runner:

1. downloads one MCD12Q2 year at a time from NASA Earthdata;
2. converts only the required phenology bands to temporary GeoTIFFs;
3. immediately reduces that year into the Autumn Atlas SQLite workspace and deletes the raw files;
4. downloads 2019 Copernicus Global Dynamic Land Cover 100 m data for the shard;
5. downloads Copernicus GLO-90 elevation only for that shard;
6. runs the production `DataPipeline` from the private `Equilibriumpress/ios-autumn` repository;
7. uploads one compressed shard to a temporary GitHub Release.

After all 126 shards complete, one final job merges the world database, rebuilds R*Tree indexes and 5° / 2.5° / 1° aggregate layers, validates SQLite integrity and schema v2, checks the iOS bundle-size budget, publishes `foliage.sqlite` as a permanent Release asset and optionally opens a data PR in `Equilibriumpress/ios-autumn`.

The temporary shard Release is deleted only after a successful final merge.

## Required GitHub Secrets

Open **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Purpose |
| --- | --- |
| `IOS_AUTUMN_TOKEN` | Fine-grained GitHub PAT for `Equilibriumpress/ios-autumn`. Give **Contents: Read and write** and **Pull requests: Read and write**. |
| `EARTHDATA_TOKEN` | Preferred NASA Earthdata Login token. |
| `EARTHDATA_USERNAME` | Optional alternative when not using `EARTHDATA_TOKEN`. |
| `EARTHDATA_PASSWORD` | Optional alternative when not using `EARTHDATA_TOKEN`. |
| `CDSE_S3_ACCESS_KEY` | Copernicus Data Space S3 access key. |
| `CDSE_S3_SECRET_KEY` | Copernicus Data Space S3 secret key. |

For NASA, either `EARTHDATA_TOKEN` **or** the username/password pair is sufficient.

Do not place credentials in repository files.

## Run the production build

Open **Actions → Build production foliage database → Run workflow**.

Recommended first run:

- Years: `2014:2024`
- Cell degrees: `0.2`
- Max bundle MiB: `95`
- Pipeline ref: `main`
- Publish to iOS: enabled

`0.2°` is intentionally the first production setting because the final app database must remain comfortably below normal Git file/bundle limits. Once the real size report is known, a denser `0.1°` build can be tested without changing the architecture.

## Outputs

Successful runs publish a permanent Release containing:

- `foliage.sqlite` — production offline database;
- `foliage.json` — merge summary;
- `database-report.json` — SQLite size/index report.

When **Publish to iOS** is enabled, the workflow also creates a branch and pull request in `Equilibriumpress/ios-autumn` containing:

`ios/RouteCinematic/foliage.sqlite`

The iOS app already prefers this schema-v2 database automatically and falls back to its small bundled prototype dataset when it is absent.

## Cost and resource strategy

The builder uses `ubuntu-latest`, never macOS. Raw satellite files are temporary. MCD12Q2 is processed year-by-year so eleven years are never held on one runner at once. The workflow limits the matrix to six concurrent jobs.

Intermediate shards are stored as temporary Release assets rather than long-retention Actions artifacts. The permanent release keeps only the final production outputs.

## Workflows

- `.github/workflows/smoke-test.yml` — compiles all Python scripts and validates the 126-shard matrix on every change.
- `.github/workflows/build-production.yml` — manual worldwide production build.

## Data sources

- NASA MODIS MCD12Q2 v6.1 — yearly land-surface phenology, 500 m.
- Copernicus Global Dynamic Land Cover 100 m v3 — `Tree_Cover_Fraction` and `Forest_Type`, reference year 2019.
- Copernicus DEM GLO-90 — elevation.

These sources are used only during the GitHub build. Autumn Atlas does not call NASA or Copernicus at runtime.
