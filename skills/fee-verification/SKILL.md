---
name: fee-verification
description: Recompute MDR and GST-on-MDR from the fee contract and compare against what the gateway claimed. Use whenever a fee, GST or net figure needs to be trusted.
script: verify_fees.py
tier: deterministic
---

# Fee verification

A `fee_inr` on a payment or a settlement is **what the gateway says it charged**. It is
evidence, never truth. This skill re-derives it from the contract.

## The arithmetic

```
fee  = round_half_up(amount x mdr_rate_pct / 100, 2)
gst  = round_half_up(fee x 18 / 100, 2)
net  = amount - fee - gst
```

Three conventions that decide whether a settlement closes to the paisa:

1. **Rounding is per payment, never per batch.** Summing exact fees and rounding once at the
   end drifts by a paisa per payment and shows up as a `rounding_paisa` residual.
2. **`ROUND_HALF_UP`, not banker's rounding.** Python's default is half-even; using it here
   makes roughly half the boundary cases off by 0.01.
3. **The schedule in force is the one effective on the payment's capture date**, not the
   settlement's payout date. A batch paid out after a rate change still bills the old rate on
   payments captured before it. That case is `fee_variance_within_contract`, and it is legal.

## Rates

`get_fee_schedule(on_date)` on psp-mcp is authoritative. UPI is 0.00% — a UPI-heavy batch
with a non-zero fee is an overcharge, not a rounding artefact.

## Reading the result

| Ratio observed/expected | Class |
|---|---|
| exactly 1.000 | `exact_match` |
| within 0.50 total over a 20+ fee-bearing batch | `rounding_paisa` — auto-resolvable |
| 1.05 – 1.30 | `fee_overcharge` — always a human, this is money leaving |
| anything else | `unexplained` — abstain and escalate |

## Run

```bash
python -m skills.main run fee-verification setl_00011
python -m skills.main run fee-verification pay_S3hlyosbohKagk
```
