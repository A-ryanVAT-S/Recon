---
name: duplicate-detection
description: Tell a double charge apart from a customer who genuinely bought twice. Use on any pair of payments sharing a customer and an amount.
script: dup_signature.py
tier: deterministic
---

# Duplicate detection

This is the discriminator the whole safety story rests on, because the two cases are
**identical on `(customer_id, amount_inr, date)`** — the three fields a naive matcher would
use. Flagging every near-duplicate scores 100% on one class and 0% on the other.

## The two fields that actually separate them

| | `duplicate_payment` | `legit_near_duplicate` |
|---|---|---|
| `order_id` | **the same** | **different** |
| gap | seconds to minutes | hours |
| what happened | the gateway retried after a timeout | the customer came back and bought again |
| outcome | ESCALATE to `payments_ops` — money to refund | AUTO — nothing is wrong |

**`order_id` is the discriminator; the time gap only corroborates it.** One order that produced
two captures is a system fault. Two orders that produced two captures is two sales. A customer
buying the same thing twice in a day is ordinary; the same order being charged twice never is.

## Thresholds

```
DUPLICATE_WINDOW_S    = 300     same order within 5 min  -> duplicate
REPEAT_PURCHASE_GAP_S = 7200    different order, 2h+     -> legitimate repeat
```

The band between them is deliberately left unnamed. Same order 20 minutes apart, or a different
order 10 minutes apart, is neither pattern — abstain and escalate rather than guess. **The gap
is not a hole; it is where the discriminator admits it does not know.**

## Naming the legitimate case explicitly

A repeat purchase must be **positively identified**, not merely left unflagged. "Nothing
matched it" and "its own order id and a three-hour gap prove it is a second sale" look the same
in a match rate and completely different in an audit. The skill emits an explicit
`legit_near_duplicate` finding with the second `order_id` as its evidence.

## Run

```bash
python -m skills.main run duplicate-detection pay_dup000004965242
python -m skills.main run duplicate-detection --all
```
