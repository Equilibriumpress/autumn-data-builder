#!/usr/bin/env python3
"""Quota-aware Autumn Atlas worker.

Claims update jobs from Supabase, asks the existing server-side foliage-score
function for one observation, persists the result as a precomputed tile and
optionally refines high-priority forest cells. End users never call this worker.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WORKER_ID = os.getenv("WORKER_ID", f"github-{socket.gethostname()}-{int(time.time())}")
MAX_JOBS = max(1, min(int(os.getenv("MAX_JOBS", "40")), 200))
BOOTSTRAP_JOBS = max(0, min(int(os.getenv("BOOTSTRAP_JOBS", "120")), 1000))
REQUEST_DELAY = max(0.25, float(os.getenv("REQUEST_DELAY_SECONDS", "1.0")))


def request(method: str, path: str, payload=None, *, prefer: str | None = None, timeout: int = 45):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "apikey": SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "autumn-atlas-precomputed-worker/1",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(f"{SUPABASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = {"message": raw[:1000]}
        return error.code, data


def rpc(name: str, payload: dict):
    status, data = request("POST", f"/rest/v1/rpc/{name}", payload)
    if not 200 <= status < 300:
        raise RuntimeError(f"RPC {name} failed ({status}): {data}")
    return data


def seed_queue() -> None:
    rpc("enqueue_due_foliage_tiles", {"p_limit": max(MAX_JOBS * 2, 50)})
    if BOOTSTRAP_JOBS:
        rpc("enqueue_global_foliage_bootstrap", {"p_limit": BOOTSTRAP_JOBS})


def claim_jobs() -> list[dict]:
    data = rpc("claim_foliage_jobs", {"p_worker_id": WORKER_ID, "p_limit": MAX_JOBS})
    return data or []


def foliage_score(job: dict):
    return request(
        "POST",
        "/functions/v1/foliage-score",
        {
            "latitude": job["latitude"],
            "longitude": job["longitude"],
            "resolutionDegrees": job["resolution_degrees"],
        },
        timeout=35,
    )


def tile_from_response(job: dict, value: dict) -> dict:
    forest = value.get("forest") or {}
    satellite = value.get("satellite") or {}
    observed_at = value.get("observedAt")
    age_hours = value.get("ageHours")
    freshness = value.get("freshness")
    if freshness not in {"observed", "recent", "stale", "forecast"}:
        if age_hours is None:
            freshness = "observed" if not value.get("cached") else "recent"
        elif age_hours <= 36:
            freshness = "observed"
        elif age_hours <= 120:
            freshness = "recent"
        else:
            freshness = "stale"

    return {
        "cell_id": job["cell_id"],
        "resolution_degrees": job["resolution_degrees"],
        "latitude": job["latitude"],
        "longitude": job["longitude"],
        "observed_at": observed_at,
        "valid_until": value.get("validUntil"),
        "score": value.get("score", 0),
        "confidence": value.get("confidence", 0),
        "phase": value.get("phase", "unknown"),
        "trend": value.get("trend", "unknown"),
        "freshness": freshness,
        "tree_cover": forest.get("treeCover"),
        "deciduous_cover": forest.get("deciduousCover"),
        "mixed_cover": forest.get("mixedCover"),
        "forest_confidence": forest.get("confidence"),
        "otci": satellite.get("otci"),
        "gifapar": satellite.get("gifapar"),
        "otci_reference": satellite.get("otciReference"),
        "gifapar_reference": satellite.get("gifaparReference"),
        "sample_count": satellite.get("sampleCount", 0),
        "source_cell_count": 1,
        "season_relevance": max(0.0, min(1.0, (float(job.get("priority", 100)) - 100.0) / 500.0)),
        "source": value.get("source", "sentinel-3-olci-l2"),
        "model_version": value.get("model") or "autumn-score-v3",
        "raw": {
            "worker": WORKER_ID,
            "jobId": job["id"],
            "processedAt": datetime.now(timezone.utc).isoformat(),
            "satellite": satellite,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_tile(tile: dict) -> None:
    status, data = request(
        "POST",
        "/rest/v1/foliage_tiles?on_conflict=cell_id",
        tile,
        prefer="resolution=merge-duplicates,return=minimal",
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"tile upsert failed ({status}): {data}")


def maybe_refine(job: dict, tile: dict) -> None:
    tree = float(tile.get("tree_cover") or 0)
    deciduous = float(tile.get("deciduous_cover") or 0) + float(tile.get("mixed_cover") or 0)
    priority = int(job.get("priority") or 0)
    resolution = float(job["resolution_degrees"])
    if tree < 0.12 or deciduous < 0.05:
        return
    target = None
    if resolution >= 0.99 and priority >= 425:
        target = 0.25
    elif 0.24 <= resolution <= 0.26 and priority >= 535:
        target = 0.1
    if target is None:
        return
    rpc(
        "enqueue_child_foliage_jobs",
        {
            "p_parent_cell_id": job["cell_id"],
            "p_parent_lat": job["latitude"],
            "p_parent_lon": job["longitude"],
            "p_parent_resolution": resolution,
            "p_target_resolution": target,
            "p_limit": 64,
        },
    )


def complete(job_id: int) -> None:
    rpc("complete_foliage_job", {"p_job_id": job_id})


def fail(job_id: int, message: str, retry_minutes: int) -> None:
    rpc(
        "fail_foliage_job",
        {"p_job_id": job_id, "p_error": message[:900], "p_retry_minutes": retry_minutes},
    )


def process(job: dict) -> str:
    status, data = foliage_score(job)
    if 200 <= status < 300 and isinstance(data, dict):
        tile = tile_from_response(job, data)
        upsert_tile(tile)
        maybe_refine(job, tile)
        complete(job["id"])
        return "completed"

    message = json.dumps(data, ensure_ascii=False)[:900] if data is not None else f"HTTP {status}"
    lowered = message.lower()
    # A non-forest cell is a successful classification. Do not retry it.
    if status == 404 and "not_deciduous_forest" in lowered:
        complete(job["id"])
        return "non_forest"
    if status == 404 and "no_satellite_data" in lowered:
        fail(job["id"], "no_satellite_data", 720)
        return "no_data"
    if status in (429, 503) or "rate_limit" in lowered or "too many" in lowered:
        fail(job["id"], "copernicus_busy", 180)
        return "rate_limited"
    fail(job["id"], message, 120)
    return "failed"


def main() -> int:
    seed_queue()
    jobs = claim_jobs()
    print(f"worker={WORKER_ID} claimed={len(jobs)} max={MAX_JOBS}")
    counters: dict[str, int] = {}
    for index, job in enumerate(jobs, 1):
        try:
            outcome = process(job)
        except Exception as exc:  # keep the queue recoverable
            outcome = "failed"
            try:
                fail(job["id"], str(exc), 120)
            except Exception:
                pass
            print(f"[{index}/{len(jobs)}] {job['cell_id']} ERROR {exc}", file=sys.stderr)
        else:
            print(f"[{index}/{len(jobs)}] {job['cell_id']} {outcome}")
        counters[outcome] = counters.get(outcome, 0) + 1
        time.sleep(REQUEST_DELAY)
        if outcome == "rate_limited":
            print("Copernicus throttled; stopping early so the next run can resume safely.")
            break
    print(json.dumps(counters, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
