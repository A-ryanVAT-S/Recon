---
name: escalation-routing
description: Decide which human owns an exception, and how urgently. Use on every escalation - an unowned exception is an exception nobody works.
script: route.py
tier: deterministic
---

# Escalation routing

Routing is a **lookup, never a judgement**. The table lives in `agents/policy/authority.yaml`
and the code that reads it contains no model call. Two people running the same exception must
get the same owner, or the routing is not a control.

## The table

| Class | Owner | Escalates |
|---|---|---|
| `duplicate_payment` | `payments_ops` | dual sign-off with `finance_controller` |
| `fee_overcharge`, `missing_bank_credit` | `finance_controller` | — |
| `unmatched_bank_credit`, `unknown_remitter`, `utr_typo` | `settlement_ops` | to `finance_controller` over Rs 1,00,000 |
| anything over Rs 5,00,000 | `finance_controller` | — |
| everything else | `payments_ops` | — |

First matching rule wins, and the last rule is a default, so **every exception gets an owner**.
There is no unrouted state.

## Why class comes before amount

A Rs 900 duplicate charge and a Rs 9,00,000 duplicate charge are the same *kind* of problem and
want the same reviewer: the person who can look at the gateway's retry log. Amount decides
urgency and sign-off, not expertise. Routing by amount first sends small fraud to whoever
handles small things.

## Urgency, and what it does not do

```
P1   exposure > Rs 2,00,000, or the counterparty is not in the master
P2   exposure > Rs 25,000
P3   everything else
```

Urgency orders the queue. **It never changes the band.** A P1 exception is not more
auto-resolvable for being urgent — that is exactly the lever injected text reaches for
("audit closes today", "SLA breach"), and it is why the urgency vocabulary is on the hard-stop
pattern list.

## Dual sign-off

`duplicate_payment` names a second owner. One person can propose the refund; the same person
cannot approve it. This is the only place in the matrix where two humans are required, and it
is there because a duplicate is the one class where the correction moves real money outward.

## Run

```bash
python -m skills.main run escalation-routing unmatched_bank_credit 150000
python -m skills.main run escalation-routing --record bank_0000107
```
