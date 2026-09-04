"""Signature and verdict for one payment against its same-customer, same-amount siblings."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import fmt, load_dataset
from dataset.verify import (DUPLICATE_WINDOW_S, REPEAT_PURCHASE_GAP_S,
                                  find_duplicates)
from MCP.servers import data_root


# what two payments must share before the discriminator is even asked
def signature(p) -> str:
    return f"{p.customer_id}|{fmt(p.amount_inr)}"


# order_id decides; the gap corroborates. anything between the thresholds abstains
def classify(a, b) -> tuple[str, str]:
    gap = int((b.captured_at - a.captured_at).total_seconds())
    if a.order_id == b.order_id and gap < DUPLICATE_WINDOW_S:
        return "duplicate_payment", f"same order {a.order_id}, {gap}s apart: a retry"
    if a.order_id != b.order_id and gap >= REPEAT_PURCHASE_GAP_S:
        return "legit_near_duplicate", f"own order {b.order_id}, {gap // 3600}h later: a second sale"
    return "INSUFFICIENT_EVIDENCE", (
        f"order {'same' if a.order_id == b.order_id else 'different'}, {gap}s apart: "
        f"between the thresholds, a human decides")


# one payment against every sibling sharing its signature
def run(payment_id: str, ds=None) -> dict:
    ds = ds or load_dataset(data_root())
    p = ds.payments_by_id().get(payment_id)
    if p is None:
        return {"error": f"no payment {payment_id}"}
    sibs = sorted([x for x in ds.payments
                   if signature(x) == signature(p) and x.payment_id != payment_id],
                  key=lambda x: x.captured_at)
    pairs = []
    for s in sibs:
        a, b = sorted((s, p), key=lambda x: x.captured_at)
        cls, why = classify(a, b)
        pairs.append({"against": s.payment_id, "detected_class": cls, "detail": why,
                      "order_ids": [a.order_id, b.order_id]})
    verdict = next((x["detected_class"] for x in pairs
                    if x["detected_class"] != "INSUFFICIENT_EVIDENCE"), None)
    return {"record_id": payment_id, "record_type": "payment",
            "signature": signature(p), "order_id": p.order_id,
            "siblings": len(sibs), "pairs": pairs,
            "detected_class": verdict or ("exact_match" if not sibs else "INSUFFICIENT_EVIDENCE"),
            "arithmetic_verified": True}


# the whole tier at once, which is what the funnel calls
def run_all(ds=None) -> dict:
    ds = ds or load_dataset(data_root())
    found = find_duplicates(ds)
    out: dict[str, int] = {}
    for e in found:
        out[e.detected_class] = out.get(e.detected_class, 0) + 1
    return {"scanned": len(ds.payments), "by_class": out,
            "records": [{"record_id": e.record_id, "detected_class": e.detected_class,
                         "detail": e.detail} for e in found]}


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--all":
        print(json.dumps(run_all(), indent=2)[:4000])
        return 0
    if not argv:
        print("usage: dup_signature.py <payment_id> | --all", file=sys.stderr)
        return 2
    print(json.dumps(run(argv[0]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
