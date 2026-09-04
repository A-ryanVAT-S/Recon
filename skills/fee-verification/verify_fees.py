"""Recompute fee and GST from the contract for one payment or one settlement."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import ZERO, compute_fee, fmt, load_dataset, money, resolve_schedule
from dataset import verify as funnel
from MCP.servers import data_root


# fee and gst re-derived per payment, plus the schedule that decided them
def check_payment(ds, payment_id: str) -> dict:
    p = ds.payments_by_id().get(payment_id)
    if p is None:
        return {"error": f"no payment {payment_id}"}
    sched = resolve_schedule(ds.schedules, p.captured_at.date())
    fee, gst = compute_fee(p.amount_inr, p.method, ds.schedules, p.captured_at.date())
    return {"record_id": p.payment_id, "record_type": "payment", "method": p.method,
            "schedule": sched.name, "rate_pct": fmt(sched.rates[p.method]),
            "expected": {"fee_inr": fmt(fee), "gst_inr": fmt(gst),
                         "net_inr": fmt(money(p.amount_inr - fee - gst))},
            "observed": {"fee_inr": fmt(p.fee_inr), "gst_inr": fmt(p.gst_inr),
                         "net_inr": fmt(p.net_inr)},
            "delta_inr": fmt(money(p.fee_inr - fee)),
            "arithmetic_verified": money(p.fee_inr - fee) == ZERO}


# the whole batch re-derived payment by payment, then typed
def check_settlement(ds, settlement_id: str) -> dict:
    s = ds.settlements_by_id().get(settlement_id)
    if s is None:
        return {"error": f"no settlement {settlement_id}"}
    gross, fee, gst, net = funnel.recompute_settlement(ds, s)
    delta = money(net - s.net_inr)
    cls, detail = (("exact_match", "verified to the paisa") if delta == ZERO
                   else funnel.attribute_delta(ds, s, delta, fee))
    ratio = (s.fee_inr / fee) if fee > ZERO else None
    return {"record_id": s.settlement_id, "record_type": "settlement",
            "payment_count": len(s.payment_ids),
            "expected": {"gross_inr": fmt(gross), "fee_inr": fmt(fee), "gst_inr": fmt(gst),
                         "net_inr": fmt(net)},
            "observed": {"gross_inr": fmt(s.gross_inr), "fee_inr": fmt(s.fee_inr),
                         "gst_inr": fmt(s.gst_inr), "net_inr": fmt(s.net_inr)},
            "fee_ratio": (f"{ratio:.4f}" if ratio is not None else None),
            "delta_inr": fmt(delta), "detected_class": cls, "detail": detail,
            "arithmetic_verified": delta == ZERO}


# one record id, either kind
def run(record_id: str, ds=None) -> dict:
    ds = ds or load_dataset(data_root())
    if record_id.startswith("setl"):
        return check_settlement(ds, record_id)
    return check_payment(ds, record_id)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: verify_fees.py <payment_id|settlement_id>", file=sys.stderr)
        return 2
    print(json.dumps(run(argv[0]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
