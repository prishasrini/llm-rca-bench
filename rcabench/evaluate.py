"""Run methods over the dataset (with a cache) and score the predictions."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .faults import FAULTS, LABELS, NO_INCIDENT
from .methods import PROMPT_VERSION
from .simulate import CONDITIONS, public_view

# ------------------------------------------------------------- running ---


def run(dataset: list[dict], methods: dict, predictions_path: Path, workers: int = 1, progress=print) -> None:
    """Append predictions for every (bundle, method) not already cached."""
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    done = {(p["bundle_id"], p["method"], p["prompt_version"]) for p in load_predictions(predictions_path)
            if p["prediction"]["root_cause"] != "error"}  # API errors are retried on the next run
    for name, method in methods.items():
        todo = [b for b in dataset if (b["id"], name, PROMPT_VERSION) not in done]
        progress(f"{name}: {len(dataset) - len(todo)} cached, {len(todo)} to run")
        if not todo:
            continue

        def one(bundle):
            return bundle, method(public_view(bundle))

        with predictions_path.open("a") as fh, ThreadPoolExecutor(max(1, workers)) as pool:
            for i, (bundle, pred) in enumerate(pool.map(one, todo), 1):
                fh.write(json.dumps({"bundle_id": bundle["id"], "method": name,
                                     "prompt_version": PROMPT_VERSION, "prediction": pred}) + "\n")
                fh.flush()
                if i % 100 == 0 or i == len(todo):
                    progress(f"  {name}: {i}/{len(todo)}")


def load_predictions(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open() as fh:
        return [json.loads(l) for l in fh if l.strip()]


# ------------------------------------------------------------- scoring ---


def score_one(bundle: dict, pred: dict) -> dict:
    truth = set(bundle["truth"]["labels"])
    primary, secondary = pred.get("root_cause"), pred.get("secondary")
    comps = bundle["truth"]["components"]
    comp_pred = (pred.get("component") or "").lower()
    return {
        "bundle_id": bundle["id"],
        "trial_id": bundle["trial_id"],
        "condition": bundle["condition"],
        "truth": sorted(truth),
        "kind": "control" if truth == {NO_INCIDENT} else ("multi" if bundle["truth"]["multi_fault"] else "single"),
        "pred": primary,
        "secondary": secondary,
        "correct": primary in truth,
        "both_correct": bundle["truth"]["multi_fault"] and {primary, secondary} == truth,
        "component_correct": any(c.replace("-", "") in comp_pred.replace("-", "").replace("_", "") for c in comps),
        "invalid": primary in ("invalid", "error"),
        "confidence": float(pred.get("confidence") or 0.0),
        "latency_s": (pred.get("_meta") or {}).get("latency_s"),
        "tokens": ((pred.get("_meta") or {}).get("prompt_tokens") or 0) + ((pred.get("_meta") or {}).get("completion_tokens") or 0),
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def macro_f1(rows: list[dict]) -> float:
    labels = sorted({r["truth"][0] for r in rows})
    f1s = []
    for lab in labels:
        tp = sum(1 for r in rows if r["pred"] == lab and r["truth"][0] == lab)
        fp = sum(1 for r in rows if r["pred"] == lab and r["truth"][0] != lab)
        fn = sum(1 for r in rows if r["pred"] != lab and r["truth"][0] == lab)
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * p * rc / (p + rc) if p + rc else 0.0)
    return sum(f1s) / len(f1s) if f1s else 0.0


def ece(rows: list[dict], bins: int = 10) -> float:
    """Expected calibration error: does a 0.8-confidence answer turn out right 80% of the time?"""
    rows = [r for r in rows if not r["invalid"]]
    if not rows:
        return float("nan")
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inb = [r for r in rows if (lo <= r["confidence"] < hi) or (b == bins - 1 and r["confidence"] == 1.0)]
        if inb:
            acc = sum(r["correct"] for r in inb) / len(inb)
            conf = sum(r["confidence"] for r in inb) / len(inb)
            total += len(inb) / len(rows) * abs(acc - conf)
    return total


def mcnemar_p(a: list[bool], b: list[bool]) -> float:
    """Exact two-sided McNemar test on paired outcomes."""
    n01 = sum(1 for x, y in zip(a, b) if x and not y)
    n10 = sum(1 for x, y in zip(a, b) if y and not x)
    n = n01 + n10
    if n == 0:
        return 1.0
    k = min(n01, n10)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def summarize(dataset: list[dict], predictions: list[dict]) -> dict:
    by_id = {b["id"]: b for b in dataset}
    latest = {}
    for p in predictions:  # last prediction wins (e.g. after a retry)
        if p["bundle_id"] in by_id:
            latest[(p["bundle_id"], p["method"])] = p
    rows_by_method: dict[str, list[dict]] = defaultdict(list)
    for (bid, method), p in latest.items():
        rows_by_method[method].append(score_one(by_id[bid], p["prediction"]))

    def acc(rows):
        n = len(rows)
        k = sum(r["correct"] for r in rows)
        lo, hi = wilson(k, n)
        return {"n": n, "acc": k / n if n else float("nan"), "ci": [lo, hi]}

    out = {"methods": {}, "conditions": CONDITIONS, "labels": LABELS}
    for method, rows in sorted(rows_by_method.items()):
        single = [r for r in rows if r["kind"] in ("single", "control")]
        multi = [r for r in rows if r["kind"] == "multi"]
        control = [r for r in rows if r["kind"] == "control"]
        faulty = [r for r in rows if r["kind"] != "control"]
        res = {
            "overall": acc(single),
            "by_condition": {c: acc([r for r in single if r["condition"] == c]) for c in CONDITIONS},
            "macro_f1_by_condition": {c: macro_f1([r for r in single if r["condition"] == c]) for c in CONDITIONS},
            "by_fault": {f: acc([r for r in single if r["truth"] == [f]]) for f in LABELS},
            "by_fault_condition": {f: {c: acc([r for r in single if r["truth"] == [f] and r["condition"] == c])["acc"]
                                       for c in CONDITIONS} for f in LABELS},
            "component_acc": sum(r["component_correct"] for r in faulty) / len(faulty) if faulty else float("nan"),
            "multi_hit": acc(multi),
            "multi_both": sum(r["both_correct"] for r in multi) / len(multi) if multi else float("nan"),
            "false_alarm_rate": (sum(1 for r in control if r["pred"] != NO_INCIDENT) / len(control)) if control else float("nan"),
            "invalid_rate": sum(r["invalid"] for r in rows) / len(rows),
            "ece": ece(single),
            "mean_conf_correct": _mean([r["confidence"] for r in single if r["correct"]]),
            "mean_conf_wrong": _mean([r["confidence"] for r in single if not r["correct"] and not r["invalid"]]),
            "mean_latency_s": _mean([r["latency_s"] for r in rows if r["latency_s"] is not None]),
            "mean_tokens": _mean([r["tokens"] for r in rows if r["tokens"]]),
            "confusion": Counter((r["truth"][0], r["pred"]) for r in single),
            "_rows": rows,
        }
        out["methods"][method] = res

    # paired significance tests between every pair of methods, on identical bundles
    names = sorted(out["methods"])
    pairs = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ra = {r["bundle_id"]: r["correct"] for r in out["methods"][a]["_rows"]}
            rb = {r["bundle_id"]: r["correct"] for r in out["methods"][b]["_rows"]}
            common = sorted(set(ra) & set(rb))
            pairs[f"{a} vs {b}"] = {"n": len(common), "p": mcnemar_p([ra[k] for k in common], [rb[k] for k in common])}
    out["mcnemar"] = pairs
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")
