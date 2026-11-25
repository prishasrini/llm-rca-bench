import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rcabench import evaluate, report, simulate  # noqa: E402
from rcabench.faults import FAULTS, LABELS, NO_INCIDENT  # noqa: E402
from rcabench.methods import parse_answer, render_evidence, rules_method  # noqa: E402


@pytest.fixture(scope="module")
def bundles():
    out = {}
    for f in list(FAULTS) + [None]:
        out[f or NO_INCIDENT] = simulate.make_bundles(11, (f,) if f else ())
    return out


def by_cond(bs, cond):
    return next(b for b in bs if b["condition"] == cond)


def test_trials_are_reproducible():
    assert simulate.make_bundles(5, ("disk_full",)) == simulate.make_bundles(5, ("disk_full",))
    assert simulate.make_bundles(5, ("disk_full",)) != simulate.make_bundles(6, ("disk_full",))


def test_every_condition_shares_the_same_metrics(bundles):
    for bs in bundles.values():
        assert len({json.dumps(b["metrics"], sort_keys=True) for b in bs}) == 1
        assert [b["condition"] for b in bs] == simulate.CONDITIONS


def test_fault_signatures(bundles):
    inc = lambda f: by_cond(bundles[f], "full")["metrics"]["overall"]["incident"]
    base = lambda f: by_cond(bundles[f], "full")["metrics"]["overall"]["baseline"]
    assert inc("database_outage")["error_rate_5xx"] > 0.1
    assert inc("database_slow")["error_rate_5xx"] == 0 and inc("database_slow")["p95_ms"] > 750
    assert inc("bad_deployment")["error_rate_5xx"] > 0.05
    assert by_cond(bundles["bad_deployment"], "full")["metrics"]["service_version"]["incident"] == "1.4.0"
    assert by_cond(bundles["resource_saturation"], "full")["metrics"]["cpu_utilization"]["incident"] > 0.9
    assert "504" in by_cond(bundles["upstream_payment_slow"], "full")["metrics"]["status_codes"]["incident"]
    assert inc(NO_INCIDENT)["rps"] > 1.5 * base(NO_INCIDENT)["rps"]
    assert inc(NO_INCIDENT)["error_rate_5xx"] == 0


def test_conditions_degrade_evidence(bundles):
    bs = bundles["database_outage"]
    assert any("database connection timeout" in l for l in by_cond(bs, "full")["logs"])
    assert by_cond(bs, "metrics_only")["logs"] == []
    assert all("request failed" in l for l in by_cond(bs, "generic")["logs"])
    noisy = by_cond(bs, "noisy")["logs"]
    real = [l for l in noisy if "database connection timeout" in l]
    assert real and len(real) < len(noisy) / 2  # truth is present but outnumbered


def test_public_view_hides_truth(bundles):
    view = simulate.public_view(bundles["disk_full"][0])
    text = json.dumps(view) + render_evidence(view)
    assert "truth" not in view and "disk_full" not in text and "-s11" not in text


def test_rules_baseline_on_full_logs(bundles):
    for label, bs in bundles.items():
        assert rules_method(simulate.public_view(by_cond(bs, "full")))["root_cause"] == label


@pytest.mark.parametrize("text,expected", [
    ('{"root_cause": "disk_full", "confidence": 0.8}', "disk_full"),
    ('```json\n{"root_cause": "Database_Outage", "secondary": null}\n```', "database_outage"),
    ('Sure! Here is my answer: {"root_cause": "cache_failure", "confidence": "high"}', "cache_failure"),
    ('{"root_cause": "aliens"}', "invalid"),
    ("I think it is the database.", "invalid"),
])
def test_parse_answer(text, expected):
    out = parse_answer(text)
    assert out["root_cause"] == expected
    assert 0.0 <= out["confidence"] <= 1.0


def test_statistics():
    lo, hi = evaluate.wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    assert evaluate.mcnemar_p([True] * 10, [True] * 10) == 1.0
    assert evaluate.mcnemar_p([True] * 20, [False] * 20) < 0.001
    rows = [{"confidence": 0.9, "correct": True, "invalid": False}] * 9 + [{"confidence": 0.9, "correct": False, "invalid": False}]
    assert evaluate.ece(rows) < 1e-9  # 90% confident, 90% right: perfectly calibrated


def test_end_to_end(tmp_path):
    from rcabench.methods import load_methods
    data_path = tmp_path / "d.jsonl"
    simulate.build_dataset(data_path, seeds_per_fault=1, multi_trials=2, controls=2)
    data = simulate.load_dataset(data_path)
    assert len(data) == (len(FAULTS) + 2 + 2) * len(simulate.CONDITIONS)
    preds = tmp_path / "p.jsonl"
    methods = load_methods(["rules", "majority", "mock"], models_file=str(tmp_path / "none.json"))
    evaluate.run(data, methods, preds, progress=lambda *_: None)
    evaluate.run(data, methods, preds, progress=lambda *_: None)  # second run is fully cached
    assert len(evaluate.load_predictions(preds)) == 3 * len(data)
    summary = evaluate.summarize(data, evaluate.load_predictions(preds))
    assert summary["methods"]["rules"]["by_condition"]["full"]["acc"] == 1.0
    assert summary["methods"]["mock"]["by_condition"]["noisy"]["acc"] < summary["methods"]["rules"]["by_condition"]["noisy"]["acc"]
    files = report.write_all(summary, tmp_path / "out")
    assert all(f.exists() for f in files)
    assert r"\begin{table}" in (tmp_path / "out" / "tables.tex").read_text()


def test_llm_method_against_fake_server(tmp_path, monkeypatch):
    import httpx
    from rcabench.methods import LLMMethod

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        req = httpx.Request("POST", url)
        if calls["n"] == 1:  # first call is rate-limited, method must retry
            return httpx.Response(429, request=req)
        assert "root cause" in json["messages"][0]["content"].lower()
        content = '{"root_cause": "disk_full", "component": "storage", "confidence": 0.7, "explanation": "ENOSPC"}'
        return httpx.Response(200, request=req, json={"choices": [{"message": {"content": content}}],
                                                       "usage": {"prompt_tokens": 900, "completion_tokens": 40}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    method = LLMMethod("llm:test", "http://fake/v1", "m")
    bundle = simulate.public_view(simulate.make_bundles(3, ("disk_full",))[0])
    out = method(bundle)
    assert out["root_cause"] == "disk_full" and out["_meta"]["prompt_tokens"] == 900 and calls["n"] == 2
