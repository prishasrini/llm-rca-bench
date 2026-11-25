# Starting points for Related Work

These are papers I believe exist and are relevant, but **verify every one on Google Scholar before citing it**: check the title, authors, venue and year, and read at least the abstract. Never cite something you haven't checked. Export BibTeX from Google Scholar (Cite → BibTeX) into `paper/references.bib`.

## LLMs for incident root-cause analysis
- Ahmed et al., "Recommending Root-Cause and Mitigation Steps for Cloud Incidents using Large Language Models" (Microsoft; ICSE 2023)
- Chen et al., "Automatic Root Cause Analysis via Large Language Models for Cloud Incidents" (RCACopilot; EuroSys 2024)
- Wang et al., "RCAgent: Cloud Root Cause Analysis by Autonomous Agents with Tool-Augmented Large Language Models" (arXiv 2023 / CIKM 2024)

## RCA benchmarks and AIOps
- Pham et al., "RCAEval: A Benchmark for Root Cause Analysis of Microservice Systems with Telemetry Data" (2025). **Read this one closely:** it's the closest existing benchmark, and your paper must say how yours differs (yours: controlled, paired evidence-quality conditions with exact ground truth, focused on LLMs).
- Search Google Scholar for: "AIOps survey root cause analysis", "log-based anomaly detection survey", "microservice root cause localization".

## Chaos engineering / fault injection
- Basiri et al., "Chaos Engineering" (IEEE Software, 2016, Netflix)

## Searches to run for newer work
- "LLM root cause analysis benchmark" (filter to the last 2 years)
- "LLM incident diagnosis logs metrics"
- "OpenRCA" and "AIOpsLab", which are recent LLM-for-operations benchmarks you should check and compare against
