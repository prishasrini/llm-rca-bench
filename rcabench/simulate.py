"""Run fault-injection trials and turn each one into evidence bundles.

One *trial* = a seed + a set of injected faults. It runs a normal baseline
window, then injects the faults and runs an incident window. The same trial is
then rendered under every *evidence condition*, so differences between
conditions are paired comparisons on identical incidents:

  full          all warning/error logs from the incident window
  noisy         15% of the real log lines, buried in 3x misleading distractors
  generic       logs reduced to "request failed status=503 path=/checkout"
  metrics_only  no logs at all

A bundle is what an RCA method sees. Ground truth is stored under "truth" and
is removed before any method gets the bundle.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import random
import statistics
from pathlib import Path

import httpx

from .faults import FAULTS, NO_INCIDENT
from .shop import ShopState, create_app

CONDITIONS = ["full", "noisy", "generic", "metrics_only"]
BASELINE_SECONDS = 120
INCIDENT_SECONDS = 120
BASE_RPS = 2.0

DISTRACTORS = [
    ("WARNING", "deprecated API version v1 used by client mobile-app/2.3", "gateway"),
    ("WARNING", "retrying DNS lookup for analytics.internal (succeeded on retry)", "shop-api"),
    ("INFO", "GC pause 180ms", "shop-api"),
    ("WARNING", "TLS certificate for api.partner.example expires in 25 days", "gateway"),
    ("WARNING", "rate limit approaching for client 10.0.4.21", "gateway"),
    ("WARNING", "feature flag new_banner evaluated with stale config", "shop-api"),
    ("WARNING", "slow query took 640ms", "database"),                     # red herring
    ("WARNING", "payment provider response slow (1.1s)", "payment-provider"),  # red herring
    ("WARNING", "cache eviction rate above 5%", "cache"),                  # red herring
    ("ERROR", "failed to send analytics event: queue full (dropped)", "analytics"),
]

BENIGN_CHANGES = [
    "config: feature flag new_banner enabled for 10% of users",
    "deploy: analytics-worker 2.8.1 -> 2.8.2",
    "infra: autoscaling max replicas 6 -> 8 for search-service",
    "deploy: admin-dashboard 0.9.4 -> 0.9.5",
]


# ------------------------------------------------------------ running ---


async def _drive(state: ShopState, client: httpx.AsyncClient, start: float, seconds: float, rps: float) -> None:
    rng = state.rng
    t = start
    while t < start + seconds:
        t += rng.expovariate(rps)
        state.now = t
        roll = rng.random()
        if roll < 0.45:
            await client.get("/products")
        elif roll < 0.72:
            await client.get(f"/products/{rng.choice([1, 2, 3, 4, 4, 3, 2, 1, 99])}")
        elif roll < 0.97:
            await client.post("/checkout", json={"product_id": rng.randint(1, 4), "quantity": rng.randint(1, 3)})
        else:
            await client.get("/health")


async def _run_trial(seed: int, faults: tuple[str, ...]) -> tuple[ShopState, list[str]]:
    state = ShopState(rng=random.Random(seed))
    app = create_app(state)
    rng = state.rng
    changes: list[str] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shop") as client:
        await _drive(state, client, 0, BASELINE_SECONDS, BASE_RPS)

        # --- incident starts ---
        state.now = BASELINE_SECONDS
        if rng.random() < 0.5:  # a harmless change shortly before, as a red herring
            changes.append(f"t={rng.randint(-600, -60):+d}s {rng.choice(BENIGN_CHANGES)}")
        if "bad_deployment" in faults:
            state.version = "1.4.0"
            changes.append(f"t={rng.randint(-90, -10):+d}s deploy: shop-api 1.3.2 -> 1.4.0")
            state.log("INFO", "service started, version 1.4.0", component="shop-api")
        state.faults = set(faults)
        state.cpu = 0.97 if "resource_saturation" in faults else round(rng.uniform(0.3, 0.45), 2)
        rps = BASE_RPS * (2.2 if not faults else 1.0)  # control trials: a harmless traffic spike
        await _drive(state, client, BASELINE_SECONDS, INCIDENT_SECONDS, rps)
    return state, sorted(changes)


# ---------------------------------------------------------- summarizing ---


def _window_stats(records) -> dict:
    if not records:
        return {"requests": 0}
    lat = sorted(r.latency for r in records)
    errors = sum(1 for r in records if r.status >= 500)
    span = max(r.t for r in records) - min(r.t for r in records) or 1
    return {
        "requests": len(records),
        "rps": round(len(records) / span, 2),
        "error_rate_5xx": round(errors / len(records), 3),
        "p50_ms": round(statistics.median(lat) * 1000),
        "p95_ms": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))] * 1000),
    }


def _status_counts(records) -> dict:
    counts: dict[str, int] = {}
    for r in records:
        counts[str(r.status)] = counts.get(str(r.status), 0) + 1
    return dict(sorted(counts.items()))


def _metrics(state: ShopState) -> dict:
    base = [r for r in state.requests if r.t < BASELINE_SECONDS]
    inc = [r for r in state.requests if r.t >= BASELINE_SECONDS]
    paths = sorted({r.path for r in state.requests if r.path != "unmatched"})
    return {
        "overall": {"baseline": _window_stats(base), "incident": _window_stats(inc)},
        "endpoints": {
            p: {"baseline": _window_stats([r for r in base if r.path == p]),
                "incident": _window_stats([r for r in inc if r.path == p])}
            for p in paths
        },
        "status_codes": {"baseline": _status_counts(base), "incident": _status_counts(inc)},
        "cpu_utilization": {"baseline": round(state.rng.uniform(0.3, 0.45), 2), "incident": state.cpu},
        "service_version": {"baseline": "1.3.2", "incident": state.version},
    }


def _alert(metrics: dict) -> str:
    inc = metrics["overall"]["incident"]
    base = metrics["overall"]["baseline"]
    if inc["error_rate_5xx"] > 0.05:
        return f"ALERT shop-api: 5xx error rate {inc['error_rate_5xx']:.1%} (threshold 5%)"
    if inc["p95_ms"] > 750:
        return f"ALERT shop-api: p95 latency {inc['p95_ms']} ms (threshold 750 ms)"
    if inc["rps"] > 1.5 * base["rps"]:
        return f"ALERT shop-api: traffic {inc['rps']} req/s, {inc['rps'] / base['rps']:.1f}x normal"
    return "ALERT shop-api: anomaly detector flagged unusual behaviour"


def _fmt(log) -> str:
    extra = " ".join(f"{k}={v}" for k, v in log.fields.items() if k != "component")
    comp = log.fields.get("component", "shop-api")
    return f"t=+{log.t - BASELINE_SECONDS:6.1f}s {log.level:<7} [{comp}] {log.msg}" + (f" {extra}" if extra else "")


def _logs_for(condition: str, state: ShopState, seed: int) -> list[str]:
    rng = random.Random(f"{seed}-{condition}")
    incident_logs = [l for l in state.logs if l.t >= BASELINE_SECONDS and l.level in ("WARNING", "ERROR", "INFO")]
    if condition == "metrics_only":
        return []
    if condition == "full":
        return [_fmt(l) for l in incident_logs][-150:]
    if condition == "generic":
        failed = [r for r in state.requests if r.t >= BASELINE_SECONDS and r.status >= 500]
        return [f"t=+{r.t - BASELINE_SECONDS:6.1f}s ERROR   [shop-api] request failed status={r.status} path={r.path}"
                for r in failed][-150:]
    # noisy: a small sample of the truth, buried in plausible but misleading lines
    keep = [l for l in incident_logs if rng.random() < 0.15]
    if incident_logs and len(keep) < 2:
        keep = rng.sample(incident_logs, min(2, len(incident_logs)))
    lines = [(l.t, _fmt(l)) for l in keep]
    for _ in range(max(10, 3 * len(keep))):
        level, msg, comp = rng.choice(DISTRACTORS)
        t = BASELINE_SECONDS + rng.uniform(0, INCIDENT_SECONDS)
        lines.append((t, f"t=+{t - BASELINE_SECONDS:6.1f}s {level:<7} [{comp}] {msg}"))
    return [s for _, s in sorted(lines)][-150:]


def make_bundles(seed: int, faults: tuple[str, ...]) -> list[dict]:
    state, changes = asyncio.run(_run_trial(seed, faults))
    metrics = _metrics(state)
    labels = sorted(faults) or [NO_INCIDENT]
    trial_id = f"{'+'.join(labels)}-s{seed}"
    return [
        {
            "id": f"{trial_id}-{cond}",
            "trial_id": trial_id,
            "condition": cond,
            "alert": _alert(metrics),
            "window": {"baseline_seconds": BASELINE_SECONDS, "incident_seconds": INCIDENT_SECONDS},
            "metrics": metrics,
            "recent_changes": changes,
            "logs": _logs_for(cond, state, seed),
            "truth": {
                "labels": labels,
                "components": sorted({FAULTS[f].component for f in faults}) or ["none"],
                "multi_fault": len(faults) > 1,
            },
        }
        for cond in CONDITIONS
    ]


# ------------------------------------------------------------- dataset ---


def trial_plan(seeds_per_fault: int = 10, multi_trials: int = 20, controls: int = 20, master_seed: int = 7) -> list[tuple[int, tuple[str, ...]]]:
    rng = random.Random(master_seed)
    plan = []
    for fault in sorted(FAULTS):
        for i in range(seeds_per_fault):
            plan.append((int(hashlib.sha256(f"{fault}-{i}-{master_seed}".encode()).hexdigest()[:8], 16), (fault,)))
    pairs = list(itertools.combinations(sorted(FAULTS), 2))
    for i in range(multi_trials):
        plan.append((rng.randrange(1, 2**31), tuple(sorted(rng.choice(pairs)))))
    for i in range(controls):
        plan.append((rng.randrange(1, 2**31), ()))
    return plan


def build_dataset(out_path: Path, **kwargs) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w") as fh:
        for seed, faults in trial_plan(**kwargs):
            for bundle in make_bundles(seed, faults):
                fh.write(json.dumps(bundle) + "\n")
                count += 1
    return count


def load_dataset(path: Path) -> list[dict]:
    with Path(path).open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def public_view(bundle: dict) -> dict:
    """What an RCA method is allowed to see: everything except the ground truth."""
    return {k: v for k, v in bundle.items() if k not in ("truth", "trial_id", "id", "condition")}
