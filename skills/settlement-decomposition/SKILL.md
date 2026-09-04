---
name: settlement-decomposition
description: Explain one bank credit in terms of the payments, fees, refunds and chargebacks behind it, or prove that no subset of payments explains it. Use on any unmatched credit or any settlement whose net does not tie out.
script: solve_decomposition.py
tier: deterministic
---

# Settlement decomposition

One bank credit stands for hundreds of payments. The identity that must hold:

```
bank_credit = SUM(payments) - fee - gst_on_fee - refunds - chargebacks
```

Every term is re-derived in code. Nothing here is a model's opinion.

## The order of operations — do not reorder it

1. **Verify.** Recompute the settlement from the contract and take the residual.
   `delta == 0` ends the investigation; most of the tier clears here.
2. **Attribute.** Try to name the residual from a fixed decision table before searching for
   anything. A residual that equals a known refund or dispute amount is explained, not
   mysterious.
3. **Search.** Only if 1 and 2 fail: bounded subset-sum over unsettled payments in a +/-3 day
   window, at most 8 items from a 40-payment pool.

Search is last because it is the only step that can produce a *coincidence*. A subset that
happens to add up is not evidence on its own; it is a hypothesis that still needs a date and a
counterparty to agree.

## Bounds, and why they are what they are

| Bound | Value | Reason |
|---|---|---|
| window | +/- 3 days | settlement T+2 plus a weekend |
| max items | 8 | a real partial settlement splits a day, it does not scatter |
| pool | 40 largest | keeps the worst case tractable; a match needing the 41st is not credible |

**"No subset explains this" is a result, not a failure.** It is the correct output for a
genuine unmatched credit, and it is what routes the record to a human with the search already
proven exhausted.

## Reading the result

| Outcome | Class | Where it goes |
|---|---|---|
| `delta == 0`, credit found | `exact_match` | matched, no action |
| residual equals a refund | `partial_refund_offset` | auto-resolvable |
| residual equals a dispute | `chargeback_deduction` | human |
| a subset sums exactly | `timing_split` | auto with the subset as evidence |
| nothing sums | `unmatched_bank_credit` | human, with the exhausted search attached |

## Run

```bash
python -m skills.main run settlement-decomposition setl_00011
python -m skills.main run settlement-decomposition bank_0000107
```
