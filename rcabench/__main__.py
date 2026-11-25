"""Command line:

  python -m rcabench generate                       build data/dataset.jsonl
  python -m rcabench show <bundle-id>               print exactly what a model sees
  python -m rcabench run --methods rules llm:groq-llama-70b
  python -m rcabench report                         results/summary.md, tables.tex, figures/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import evaluate, report, simulate
from .methods import load_methods, render_evidence

DATA = Path("data/dataset.jsonl")
PREDS = Path("results/predictions.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(prog="rcabench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="simulate all trials and write the dataset")
    g.add_argument("--seeds-per-fault", type=int, default=10)
    g.add_argument("--multi", type=int, default=20, help="number of two-fault trials")
    g.add_argument("--controls", type=int, default=20, help="number of no-incident trials")
    g.add_argument("--out", type=Path, default=DATA)

    s = sub.add_parser("show", help="print the prompt evidence for one bundle")
    s.add_argument("bundle_id")

    r = sub.add_parser("run", help="run methods over the dataset (cached)")
    r.add_argument("--methods", nargs="+", required=True)
    r.add_argument("--models", default="models.json")
    r.add_argument("--workers", type=int, default=1)
    r.add_argument("--limit", type=int, default=0, help="only the first N bundles (for a quick try)")
    r.add_argument("--conditions", nargs="+", choices=simulate.CONDITIONS)

    rp = sub.add_parser("report", help="score predictions and write tables and figures")
    rp.add_argument("--out", type=Path, default=Path("results"))

    args = ap.parse_args()

    if args.cmd == "generate":
        n = simulate.build_dataset(args.out, seeds_per_fault=args.seeds_per_fault,
                                   multi_trials=args.multi, controls=args.controls)
        print(f"wrote {n} evidence bundles to {args.out}")
    elif args.cmd == "show":
        bundle = next((b for b in simulate.load_dataset(DATA) if b["id"] == args.bundle_id), None)
        if bundle is None:
            raise SystemExit("no such bundle id (ids look like database_outage-s123-full)")
        print(render_evidence(simulate.public_view(bundle)))
        print("\nTRUTH:", json.dumps(bundle["truth"]))
    elif args.cmd == "run":
        data = simulate.load_dataset(DATA)
        if args.conditions:
            data = [b for b in data if b["condition"] in args.conditions]
        if args.limit:
            data = data[: args.limit]
        evaluate.run(data, load_methods(args.methods, args.models), PREDS, workers=args.workers)
        print("done. next: python -m rcabench report")
    elif args.cmd == "report":
        summary = evaluate.summarize(simulate.load_dataset(DATA), evaluate.load_predictions(PREDS))
        if not summary["methods"]:
            raise SystemExit("no predictions yet: run `python -m rcabench run --methods rules` first")
        for path in report.write_all(summary, args.out):
            print("wrote", path)
        print()
        print((args.out / "summary.md").read_text())


if __name__ == "__main__":
    main()
