"""Root-cause analysis methods under test.

Every method receives the *public* view of a bundle (no ground truth) and returns:
    {"root_cause": <label>, "secondary": <label or null>, "component": str,
     "confidence": float 0-1, "explanation": str}

Methods:
  rules        a hand-tuned expert runbook: keyword rules on logs, then metric patterns.
               Written with knowledge of the fault catalogue, so treat it as a strong
               upper reference, not a fair competitor (see the paper's threats to validity).
  majority     always predicts one label: the chance-level floor
  llm:<name>   any OpenAI-compatible chat model listed in models.json
  mock         deterministic stand-in used by the tests (no network)
"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

from .faults import FAULTS, LABELS, NO_INCIDENT

PROMPT_VERSION = "v1"

# ---------------------------------------------------------------- rules ---

LOG_RULES = [
    (r"database connection timeout", "database_outage"),
    (r"no space left on device|errno 28", "disk_full"),
    (r"keyerror|unhandled exception", "bad_deployment"),
    (r"payment provider timeout", "upstream_payment_slow"),
    (r"payment provider returned 50\d", "upstream_payment_errors"),
    (r"cache connection refused", "cache_failure"),
    (r"event loop lag", "resource_saturation"),
    (r"slow query", "database_slow"),
    (r"payment provider response slow", "upstream_payment_slow"),
]


def rules_method(bundle: dict) -> dict:
    """What a careful on-call engineer's runbook would do, written as code."""
    text = "\n".join(bundle["logs"]).lower()
    votes: dict[str, int] = {}
    for pattern, label in LOG_RULES:
        n = len(re.findall(pattern, text))
        if n:
            votes[label] = votes.get(label, 0) + n
    if votes:
        ranked = sorted(votes, key=lambda k: -votes[k])
        total = sum(votes.values())
        return _result(ranked[0], ranked[1] if len(ranked) > 1 else None,
                       votes[ranked[0]] / total, f"log rule matches: {votes}")

    # no telling logs: fall back to metric patterns
    m = bundle["metrics"]
    base, inc = m["overall"]["baseline"], m["overall"]["incident"]
    if m["service_version"]["incident"] != m["service_version"]["baseline"]:
        return _result("bad_deployment", None, 0.6, "version changed during incident")
    if m["cpu_utilization"]["incident"] > 0.9:
        return _result("resource_saturation", None, 0.7, "CPU above 90%")
    ep = m["endpoints"]
    def slow(path):
        e = ep.get(path, {})
        return e.get("incident", {}).get("p95_ms", 0) > 3 * max(e.get("baseline", {}).get("p95_ms", 1), 1)
    def failing(path):
        return ep.get(path, {}).get("incident", {}).get("error_rate_5xx", 0) > 0.05
    if failing("/products") and failing("/checkout"):
        return _result("database_outage", None, 0.5, "reads and writes both failing")
    codes = m["status_codes"]["incident"]
    if codes.get("504"):
        return _result("upstream_payment_slow", None, 0.5, "gateway timeouts on checkout")
    if codes.get("502"):
        return _result("upstream_payment_errors", None, 0.5, "502s on checkout")
    if failing("/checkout"):
        return _result("bad_deployment", "disk_full", 0.3, "only checkout failing with 500")
    if slow("/checkout") and not slow("/products"):
        return _result("upstream_payment_slow", None, 0.4, "only checkout slow")
    if slow("/products") and slow("/checkout"):
        return _result("database_slow", None, 0.4, "reads and writes slow, CPU normal")
    if slow("/products"):
        return _result("cache_failure", None, 0.3, "reads moderately slower")
    if inc.get("rps", 0) > 1.5 * base.get("rps", 1):
        return _result(NO_INCIDENT, None, 0.6, "traffic spike only, errors and latency normal")
    return _result(NO_INCIDENT, None, 0.3, "no clear signal")


def _result(label, secondary, confidence, explanation, component=None):
    comp = component or (FAULTS[label].component if label in FAULTS else "none")
    return {"root_cause": label, "secondary": secondary, "component": comp,
            "confidence": round(float(confidence), 3), "explanation": explanation}


# ------------------------------------------------------------------ LLM ---

SYSTEM_PROMPT = """You are an expert site reliability engineer doing root-cause analysis for an alert on "shop-api", an e-commerce web service.
Architecture: shop-api serves /products and /products/{pid} (cache first, then database) and /checkout (database, then an external payment provider, then writes a receipt file to local storage). /health returns service status.

You will get: the alert, metrics for a normal baseline window and the incident window (overall, per endpoint, HTTP status codes, CPU, service version), recent changes, and log lines from the incident window. Some evidence may be missing, generic or contain misleading noise. Base your answer on evidence, not on how often a component is mentioned.

Choose the root cause from EXACTLY these labels:
- database_outage: database unreachable, queries time out, requests fail
- database_slow: database degraded, queries slow, little or no errors
- upstream_payment_errors: payment provider returns errors
- upstream_payment_slow: payment provider slow or timing out
- resource_saturation: shop-api itself is overloaded (CPU), everything slows down
- bad_deployment: a recent shop-api release introduced a bug
- cache_failure: cache unavailable, reads fall back to the database
- disk_full: local storage out of space, writes fail
- no_incident: nothing is actually broken (for example only a harmless traffic change)

Respond with ONLY a JSON object, no other text:
{"root_cause": "<label>", "secondary": "<label or null if only one cause>", "component": "<component at fault>", "confidence": <0.0-1.0>, "explanation": "<one or two sentences citing the evidence>"}"""


def render_evidence(bundle: dict) -> str:
    m = bundle["metrics"]
    lines = [bundle["alert"], "",
             f"WINDOWS: baseline {bundle['window']['baseline_seconds']}s, incident {bundle['window']['incident_seconds']}s", "",
             "METRICS (baseline -> incident):"]

    def row(name, b, i):
        return (f"  {name:<18} req/s {b.get('rps', 0):>5} -> {i.get('rps', 0):<5} | 5xx {b.get('error_rate_5xx', 0):.1%} -> "
                f"{i.get('error_rate_5xx', 0):.1%} | p50 {b.get('p50_ms', 0)} -> {i.get('p50_ms', 0)} ms | "
                f"p95 {b.get('p95_ms', 0)} -> {i.get('p95_ms', 0)} ms")

    lines.append(row("overall", m["overall"]["baseline"], m["overall"]["incident"]))
    for path, e in m["endpoints"].items():
        lines.append(row(path, e["baseline"], e["incident"]))
    lines += [
        f"  status codes       baseline {json.dumps(m['status_codes']['baseline'])} -> incident {json.dumps(m['status_codes']['incident'])}",
        f"  cpu utilization    {m['cpu_utilization']['baseline']:.0%} -> {m['cpu_utilization']['incident']:.0%}",
        f"  service version    {m['service_version']['baseline']} -> {m['service_version']['incident']}",
        "",
        "RECENT CHANGES:",
        *([f"  {c}" for c in bundle["recent_changes"]] or ["  (none recorded)"]),
        "",
        f"LOGS ({len(bundle['logs'])} lines):",
        *([f"  {l}" for l in bundle["logs"]] or ["  (no logs available)"]),
    ]
    return "\n".join(lines)


def parse_answer(text: str) -> dict:
    """Pull the JSON object out of a model reply, tolerating code fences and chatter."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {"root_cause": "invalid", "secondary": None, "component": "", "confidence": 0.0,
                "explanation": f"unparseable: {text[:200] if text else ''}"}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"root_cause": "invalid", "secondary": None, "component": "", "confidence": 0.0,
                "explanation": f"bad json: {match.group(0)[:200]}"}
    label = str(data.get("root_cause", "")).strip().lower()
    secondary = data.get("secondary")
    secondary = str(secondary).strip().lower() if secondary not in (None, "", "null", "none") else None
    try:
        conf = min(1.0, max(0.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    return {
        "root_cause": label if label in LABELS else "invalid",
        "secondary": secondary if secondary in LABELS else None,
        "component": str(data.get("component", "")),
        "confidence": conf,
        "explanation": str(data.get("explanation", ""))[:500],
    }


class LLMMethod:
    def __init__(self, name: str, base_url: str, model: str, api_key_env: str | None = None,
                 temperature: float = 0.0, max_retries: int = 4, min_interval: float = 0.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = os.getenv(api_key_env, "") if api_key_env else ""
        self.temperature = temperature
        self.max_retries = max_retries
        self.min_interval = min_interval  # seconds between calls, for free-tier rate limits
        self._last = 0.0

    def __call__(self, bundle: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": render_evidence(bundle)}],
        }
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            started = time.time()
            self._last = started
            try:
                resp = httpx.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=120)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                body = resp.json()
                out = parse_answer(body["choices"][0]["message"]["content"])
                usage = body.get("usage") or {}
                out["_meta"] = {"latency_s": round(time.time() - started, 2),
                                "prompt_tokens": usage.get("prompt_tokens"),
                                "completion_tokens": usage.get("completion_tokens")}
                return out
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                if attempt == self.max_retries:
                    return {"root_cause": "error", "secondary": None, "component": "", "confidence": 0.0,
                            "explanation": f"API error: {exc}"}
                time.sleep(min(60, 2 ** attempt * 2))
        raise AssertionError("unreachable")


def mock_method(bundle: dict) -> dict:
    """Deterministic fake model for tests: rules, but wrong on purpose for noisy evidence."""
    out = rules_method(bundle)
    if any("[analytics]" in l for l in bundle["logs"]):
        out = _result("database_slow", None, 0.9, "mock: fooled by noise")
    return out


def majority_method(bundle: dict) -> dict:
    """Chance-level floor: always guess the single most common label."""
    return _result("database_outage", None, 1 / len(LABELS), "always guesses the same label")


def load_methods(names: list[str], models_file: str = "models.json") -> dict:
    registry = {"rules": rules_method, "majority": majority_method, "mock": mock_method}
    configs = {}
    if os.path.exists(models_file):
        with open(models_file) as fh:
            configs = {c["name"]: c for c in json.load(fh)}
    methods = {}
    for name in names:
        if name in registry:
            methods[name] = registry[name]
        elif name.startswith("llm:") and name[4:] in configs:
            cfg = dict(configs[name[4:]])
            cfg.pop("name")
            methods[name] = LLMMethod(name=name, **cfg)
        else:
            known = list(registry) + [f"llm:{n}" for n in configs]
            raise SystemExit(f"Unknown method {name!r}. Available: {', '.join(known)}")
    return methods
