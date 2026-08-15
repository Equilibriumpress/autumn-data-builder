# Autumn Atlas Worker Trigger

This repository has one purpose: **wake the Autumn Atlas precomputed foliage pipeline on a schedule**.

It does not download or process satellite imagery itself. It does not build a local SQLite database, run MODIS jobs, or calculate foliage scores in GitHub Actions.

## Architecture

```text
GitHub Actions
    ↓ every 3 hours
POST process-foliage-queue
    ↓
Supabase orchestrator
    ↓
5° CLMS forest discovery
    ↓
1° → 0.25° → 0.1° foliage updates
    ↓
Supabase foliage_tiles
    ↓
Autumn Atlas iOS app + widget
```

The actual backend implementation, database migrations and Edge Functions are versioned in [`Equilibriumpress/ios-autumn`](https://github.com/Equilibriumpress/ios-autumn) under `supabase/`.

## Workflow

`.github/workflows/precomputed-foliage-worker.yml`

The workflow:

- runs automatically every 3 hours and can also be started manually;
- sends one authenticated HTTP request to the Supabase `process-foliage-queue` Edge Function;
- does not need a private Supabase service-role key;
- passes at most `maxJobs = 4` to the server.

Supabase remains authoritative. The database enforces a global worker window of at least 150 minutes and the Edge Function decides which queued cells are processed. Triggering the GitHub workflow more frequently therefore cannot create an uncontrolled Copernicus request burst.

## Why this repository is intentionally small

Earlier versions contained a large experimental MODIS/CDSE/shard/SQLite builder. That architecture is no longer used. The old implementation remains available in Git history if it is ever needed for research or reference.

Runtime clients never call Copernicus directly. The iOS app and widget read the precomputed `foliage_tiles` dataset from Supabase.
