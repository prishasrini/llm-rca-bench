# Results

Accuracy = the predicted root cause is correct (single-fault and control trials). 95% Wilson confidence intervals in brackets.

## Accuracy by evidence condition

| Method | Full logs | Noisy logs | Generic logs | Metrics only | Overall |
|---|---:|---:|---:|---:|---:|
| majority | 10.0 | 10.0 | 10.0 | 10.0 | 10.0 [7.4–13.3] (n=400) |
| rules | 100.0 | 74.0 | 90.0 | 90.0 | 88.5 [85.0–91.3] (n=400) |

## Other measures

| Method | Macro-F1 (full) | Macro-F1 (metrics only) | Component acc. | Multi-fault hit | Multi-fault both | False alarms | Invalid output | ECE | Conf. right / wrong | Latency (s) | Tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| majority | 2.0 | 2.0 | 36.0 | 50.0 | 0.0 | 100.0 | 0.0 | 0.011 | 0.11 / 0.11 | – | – |
| rules | 100.0 | 85.2 | 92.5 | 97.5 | 23.8 | 22.5 | 0.0 | 0.266 | 0.67 / 0.53 | – | – |

## Accuracy by fault (all conditions)

| Fault | majority | rules |
|---|---:|---:|
| bad_deployment | 0.0 | 100.0 |
| cache_failure | 0.0 | 100.0 |
| database_outage | 100.0 | 100.0 |
| database_slow | 0.0 | 100.0 |
| disk_full | 0.0 | 47.5 |
| resource_saturation | 0.0 | 85.0 |
| upstream_payment_errors | 0.0 | 100.0 |
| upstream_payment_slow | 0.0 | 97.5 |
| no_incident | 0.0 | 77.5 |

## Paired significance (exact McNemar, same bundles)

| Comparison | n | p-value |
|---|---:|---:|
| majority vs rules | 480 | 0.0000 |
