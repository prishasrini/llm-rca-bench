"""The fault catalogue: every failure the benchmark can inject, with its ground truth.

Each fault changes how the simulated shop-api behaves (latency, errors, logs,
gauges). The label is what a correct root-cause analysis must name.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fault:
    label: str          # ground-truth root-cause category
    component: str      # the component actually at fault
    description: str    # human-readable, used in the paper's fault table


FAULTS: dict[str, Fault] = {
    f.label: f
    for f in [
        Fault("database_outage", "database",
              "Database unreachable: queries hang until a connection timeout, requests fail with 503."),
        Fault("database_slow", "database",
              "Database degraded: queries take 0.5-2 s, occasional slow-query warnings, no errors."),
        Fault("upstream_payment_errors", "payment-provider",
              "Third-party payment provider returns 502 for ~40% of charges."),
        Fault("upstream_payment_slow", "payment-provider",
              "Payment provider responds in 1.5-4 s; calls over 3.5 s time out with 504."),
        Fault("resource_saturation", "shop-api",
              "Service CPU saturated: every endpoint (including /health) slows down uniformly."),
        Fault("bad_deployment", "checkout-service",
              "New release 1.4.0 has a bug: ~60% of checkouts raise an unhandled KeyError."),
        Fault("cache_failure", "cache",
              "Cache cluster refuses connections; reads fall back to the database (slower, no errors)."),
        Fault("disk_full", "storage",
              "Receipt volume is full: ~50% of checkouts fail writing receipts (ENOSPC)."),
    ]
}

NO_INCIDENT = "no_incident"   # control trials: an alert fired, but nothing is actually broken
LABELS = sorted(FAULTS) + [NO_INCIDENT]
