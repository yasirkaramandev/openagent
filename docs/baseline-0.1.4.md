# Baseline measurements — v0.1.4

Recorded at the start of the stabilization program (Aşama 0), before any v0.1.5 code changed.

These are **comparison points, not thresholds.** Nothing in CI fails because a number here moved;
the point is that when v0.1.6 ships, "did this get slower?" and "did the wheel double in size?" are
questions with answers rather than opinions. The one number that *is* enforced is branch coverage,
via `scripts/coverage_floors.txt` — and it is enforced as a floor, not as this exact value.

**Measurement environment.** macOS 15.5 (Darwin 25.5.0), arm64, Python 3.12, local development
machine — not CI. Numbers taken on GitHub runners will differ, sometimes by a lot. Compare like
with like: re-measure on the same machine before drawing a conclusion.

Commit: `a077d42` (`release: bump version to 0.1.4`)

---

## Test suite

| Metric | Value |
|---|---|
| Tests passed | 1209 |
| Tests skipped | 8 |
| Tests failed | 0 |
| Wall time (no coverage) | 459.8 s (7 m 40 s) |
| Wall time (with branch coverage) | ~28 min |

The suite is green at baseline. That is worth stating explicitly, because it means every failure
seen during v0.1.5 and v0.1.6 work is a real regression introduced by that work — there is no
pre-existing red to hide behind.

The coverage overhead (roughly 4×) is why CI runs coverage inside the existing `test` job rather
than as a separate matrix: doing it once is affordable, doing it three times is not.

## Branch coverage (critical modules)

Measured with `coverage run --branch`; see `scripts/check_critical_coverage.py` for how these are
enforced and why the gate is a ratchet rather than a fixed threshold.

| Module | Branch % | Target | Gap |
|---|---:|---:|---:|
| `credentials/` | 79.7 | 95.0 | 15.3 |
| `security/` | 73.3 | 95.0 | 21.7 |
| `storage/repositories.py` | 75.0 | 95.0 | 20.0 |
| `storage/migrations.py` | 66.3 | 95.0 | 28.7 |
| `services/provider_service.py` | 73.4 | 95.0 | 21.6 |
| `services/agent_service.py` | 59.1 | 95.0 | 35.9 |
| `runtimes/cli/updates.py` | 54.6 | 95.0 | 40.4 |
| `tui/` | 62.0 | 75.0 | 13.0 |

Two of these deserve comment, because the numbers predicted where the bugs were before anyone went
looking:

* **`runtimes/cli/updates.py` (54.6%)** is the least-covered module in the critical set, and it is
  the module that reports a successful update when verification returned `UNKNOWN`
  (`updates.py:577`). Nearly half its branches were never executed by a test.
* **`services/agent_service.py` (59.1%)** is where the agent/provider binding is maintained without
  a foreign key. The `provider deleted while an agent still references it` path had no coverage at
  all.

## Package

| Metric | Value |
|---|---|
| Wheel size | 360 KB (367,216 bytes) |
| sdist size | 899 KB (920,396 bytes) |
| Build time (`python -m build`, isolated env) | 1.7 s |
| Install into a clean venv (`pip install dist/*.whl`) | 4 s |
| `openagent version` | works, prints `openagent 0.1.4` |
| `openagent doctor --json` | works, parseable, keys: `checks`, `exit_code` |

## Runtime

| Metric | Value |
|---|---|
| `openagent doctor --json` (cold, clean install) | 6 s |

The doctor target in the plan is "< 1 second cached". The 6 s here is a **cold** run in a venv with
no cached CLI detection and no populated database, so it is not directly comparable — it is
recorded as the honest cold-start number, and the cached number needs to be measured separately
once the discovery cache is populated. Do not treat 6 s as a regression baseline for the cached
path.

## Not yet measured

These are in the plan but were not captured at baseline, because the harness to measure them does
not exist yet. They should be added when the corresponding work lands rather than back-filled with
guesses:

* migration duration on a realistic database
* 10k event append duration
* 50k event replay peak memory
* DB and WAL file size after a realistic workload (WAL is not even enabled until migration 0012)
* CLI detection duration

Leaving these blank is deliberate. A baseline table with invented numbers in it is worse than one
with gaps, because the gaps are visible and the invented numbers are not.

---

## 0.1.6rc1 comparison

Measured on the same machine after the full v0.1.5 + v0.1.6 program. Numbers moved as expected; none
regressed in a way that matters.

| Metric | 0.1.4 | 0.1.6rc1 | Note |
|---|---:|---:|---|
| Tests passed | 1209 | 1333 | +124, all new regressions |
| Wheel size | 360 KB | 392 KB | +9%, the new modules |
| `migrations.py` branch coverage | 66.3% | 71.1% | ratchet caught a dip to 61.1% mid-work; migration 0012 tests recovered it |
| `updates.py` branch coverage | 54.6% | 57.7% | new verification/ASK tests |
| Migration 0011→0012 (20 providers, 50 agents) | — | 41 ms | well under the < 10 s target |

The coverage ratchet did its job once during this work: adding migration 0012 without tests dropped
`migrations.py` below its floor and CI would have failed. The fix was to write the migration tests,
not to lower the floor — which is the entire point of the mechanism.

## Reproducing

```bash
# Test suite
python -m pytest -q

# Branch coverage + the critical-module gate
coverage run -m pytest -q
coverage combine || true
coverage xml
python scripts/check_critical_coverage.py

# Package + dry release
python -m build
python -m venv /tmp/oa-rel
/tmp/oa-rel/bin/pip install dist/*.whl
/tmp/oa-rel/bin/openagent version
/tmp/oa-rel/bin/openagent doctor --json
```
