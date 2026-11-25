# LLM-RCA-Bench: How much do logs matter for AI root-cause analysis?

A research project and paper draft. It injects known failures into a simulated web service, gives the evidence to AI models (and baselines), and measures how often they correctly name the root cause, especially when the logs are **noisy, generic or missing**.

**Research question:** *How does the quality of observability evidence affect the accuracy, robustness and calibration of LLMs doing incident root-cause analysis?*

Why this is publishable: real incident data rarely has certain ground truth, and you can't control how good its logs are. Here every incident has an exact known cause, and the **same incident** is shown to models in 4 evidence conditions. Differences between conditions are therefore clean, paired comparisons.

## What's in the box

| Part | File | What it does |
|---|---|---|
| Fault catalogue | `rcabench/faults.py` | 8 failure types plus a no-incident control, with ground truth |
| Simulated service | `rcabench/shop.py` | Real FastAPI app; failures change latency, errors and logs. A virtual clock makes each trial take under a second and reproducible from its seed |
| Dataset generator | `rcabench/simulate.py` | 120 trials × 4 evidence conditions = **480 bundles** |
| Methods | `rcabench/methods.py` | Any OpenAI-compatible LLM, an expert rule baseline, a chance baseline |
| Scoring | `rcabench/evaluate.py` | Accuracy with confidence intervals, macro-F1, component accuracy, multi-fault, false alarms, calibration (ECE), McNemar significance tests |
| Report | `rcabench/report.py` | `results/summary.md`, LaTeX tables, PDF/PNG figures |
| Paper | `paper/main.tex` | IEEE conference draft with method sections written and results marked TODO |

**The 4 evidence conditions**
- **full:** all warning and error logs
- **noisy:** 15% of the real logs, buried in 3× misleading distractors (some blame the wrong component)
- **generic:** logs reduced to "request failed status=500 path=/checkout", with no cause in the text
- **metrics_only:** no logs at all

The 8 faults include deliberately confusable pairs. For example, a bad deployment and a full disk both cause checkout 500s; a slow database and a saturated CPU both cause latency without errors.

## Setup (Windows, about 10 minutes)

```
pip install -r requirements.txt
python -m rcabench generate                 # builds data/dataset.jsonl (about 40 s)
python -m rcabench run --methods rules majority
python -m rcabench report                   # results/summary.md + tables + figures
```

See exactly what a model is given for one incident:
```
python -m rcabench show database_outage-s<number>-noisy
```
(Copy a real id from `data/dataset.jsonl`.)

## Running AI models (free options)

Models are listed in `models.json`. Any OpenAI-compatible API works.

**Groq (free tier, fast):** create a key at console.groq.com, then:
```
set GROQ_API_KEY=your-key
python -m rcabench run --methods llm:groq-llama-70b llm:groq-llama-8b
```

**Google Gemini (free tier):** create a key at aistudio.google.com, then `set GEMINI_API_KEY=...` and use `llm:gemini-flash`.

**Ollama (fully local, no key, no limits):** install from ollama.com, run `ollama pull qwen2.5:7b`, then use `llm:ollama-qwen-7b`. Great for comparing small models; slow on a laptop without a GPU.

Tips:
- Try a quick run first with `--limit 20`. Runs are **cached**: stop any time (Ctrl+C) and re-run the same command to continue. Failed API calls are retried automatically on the next run.
- Free tiers have rate limits; `min_interval` in `models.json` spaces out calls. 480 calls per model at 2 s apart is about 16 minutes.
- **Model names change.** If a provider rejects one, check its current model list and update `models.json`. Record the exact model names and the date you ran them: the paper needs them.
- Use `set` on Windows Command Prompt, `$env:GROQ_API_KEY="..."` in PowerShell, `export` on Mac/Linux.

**Suggested model set for the paper:** 2 large hosted models plus 2 small local ones (e.g. Llama 70B, Gemini Flash, Qwen 7B, Llama 3B). "Does a small local model hold up?" is an interesting question on its own.

## Writing the paper

1. Run everything, then `python -m rcabench report`.
2. Upload the whole project folder to **Overleaf** (New Project → Upload Project, as a zip) and open `paper/main.tex`. Overleaf includes the IEEE template. The tables and figures load automatically from `results/`.
3. Fill every red **TODO** using your real numbers from `results/summary.md`. **Never write a number you didn't produce.**
4. Related work: start from `paper/references-to-verify.md`, verify each paper on Google Scholar, read them, and cite only what you've read. RCAEval, OpenRCA and AIOpsLab are the closest existing work, so explain how yours differs.
5. Show it to your faculty guide early, ideally before running all the models, so they can shape the experiments.

## Honest limitations (say these in the paper; reviewers respect it)

- **The rule baseline is optimistic.** I wrote it knowing the fault catalogue, so it scores 100% with full logs. Present it as an "expert runbook" upper reference, not a competitor.
- **The task is constrained.** The prompt lists the 9 allowed answers, so it's classification, not open-ended diagnosis.
- **It's a simulation**, not production. The service is real code, but the faults are modelled.
- **10 seeds per fault** gives fairly wide confidence intervals. If your guide wants tighter ones, use `python -m rcabench generate --seeds-per-fault 20` (this doubles API calls).

## Ideas that would strengthen the paper (pick one with your guide)

- **Prompting:** add a "think step by step" variant, or few-shot examples, and compare.
- **Open-ended version:** remove the label list and grade free-text answers.
- **Live validation:** run a few faults on the real Docker stack from the observability-dashboard project and check the conclusions match the simulation.
- **More faults:** memory leaks, DNS failures, certificate expiry.

## Timeline (about 3 months)

| Weeks | Work |
|---|---|
| 1 | Read this code and 5–6 related papers; meet your guide |
| 2–3 | Choose models, run all experiments, look at failures by hand |
| 4–5 | Add one strengthening idea from above |
| 6–8 | Write the paper; guide reviews |
| 9–12 | Revise, pick a venue with your guide, submit; post to arXiv if allowed |

**Venues:** ask your guide. Look for IEEE conferences with an AI, software engineering or cloud track, and AIOps or LLM-for-systems workshops. **Avoid journals that ask you to pay to publish quickly**; many are predatory.

## Tests

```
pip install pytest
pytest -v
```
14 tests check reproducibility, every fault's signature, that ground truth never leaks into what models see, answer parsing, the statistics, retry on rate limits, and the full pipeline end to end.
