---
name: month-end-close
description: Run a close and render the report a controller signs. Use at period end, or to re-render the report for a run that already happened.
script: build_close_report.py
tier: deterministic
---

# Month-end close

A close is a sequence with a fixed order. Each step's output is the next step's input, and no
step may be skipped because the previous one looked clean.

## The checklist

1. **Freeze the period.** Everything after this reads a fixed set of records. A close that
   races new data is not reproducible and cannot be signed.
2. **Match.** T0 join, then T1 verify-attribute-search. Deterministic, no model.
3. **Examine every record, not just the interesting ones.** Coverage is
   `matched + exceptions == total`. If those do not agree, the difference is records nobody
   looked at, and the auto-rate is measured against a denominator that quietly shrank.
4. **Propose.** One proposal per exception, carrying the class, the exposure and the chain.
5. **Gate.** Every proposal through the Policy Agent. No exceptions, including the ones the
   arithmetic proved — proof is an input to the decision, not a substitute for it.
6. **Post what was allowed.** Propose then commit, with the token the gate issued.
7. **Escalate the rest with packs.** Owner, urgency and evidence attached at escalation time.
8. **Report.** Numbers, bands, owners, and the run id that reproduces all of it.

## What the report has to state

- **The tier.** DEMO is enriched with anomalies; its rates are not the honest ones. A report
  that does not name its tier will be quoted as if it were SCALE.
- **Exposure, not record value.** A paisa rounding error on a Rs 1,76,087 settlement is a
  Rs 0.03 exposure. Reporting the settlement's value makes the number look enormous and the
  auto-rate look reckless, and both would be wrong.
- **Every escalation's owner.** An escalation without a name is a number in a table.
- **What the system did not do.** Abstentions and hard stops are results. Hiding them makes the
  auto-rate look better and the report dishonest.

## The identity that must hold at the end

```
records == matched + exceptions
exceptions == auto_resolved + escalated
auto_resolved == posted_to_ledger
```

The third one is the one people forget. A proposal the gate allowed but that never reached the
ledger is not resolved; it is lost.

## Run

```bash
python -m agents.main close --month 2026-08
python -m skills.main run month-end-close                 # render the latest run
python -m skills.main run month-end-close <run_id> --html
```
