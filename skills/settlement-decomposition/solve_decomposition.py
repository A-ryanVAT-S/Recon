"""Verify, then attribute, then bounded-search: explain one credit or prove nothing explains it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import ZERO, fmt, load_dataset, money
from dataset import verify as funnel
from MCP.servers import data_root


# stage 1 and 2 for a settlement: recompute, then name the residual
def decompose_settlement(ds, settlement_id: str) -> dict:
    s = ds.settlements_by_id().get(settlement_id)
    if s is None:
        return {"error": f"no settlement {settlement_id}"}
    gross, fee, gst, net = funnel.recompute_settlement(ds, s)
    delta = money(net - s.net_inr)
    stages = [{"stage": "verify", "expected_net_inr": fmt(net),
               "observed_net_inr": fmt(s.net_inr), "delta_inr": fmt(delta)}]
    if delta == ZERO:
        cls, detail = "exact_match", "identity holds to the paisa"
    else:
        cls, detail = funnel.attribute_delta(ds, s, delta, fee)
        stages.append({"stage": "attribute", "detected_class": cls, "detail": detail})
    return {"record_id": settlement_id, "record_type": "settlement",
            "components": {"gross_inr": fmt(gross), "fee_inr": fmt(fee), "gst_inr": fmt(gst),
                           "refunds_inr": fmt(s.refunds_inr),
                           "disputes_inr": fmt(s.disputes_inr), "net_inr": fmt(net)},
            "payment_count": len(s.payment_ids), "stages": stages,
            "detected_class": cls, "detail": detail, "delta_inr": fmt(delta),
            "arithmetic_verified": delta == ZERO}


# stage 3 for a bank line: which unsettled payments add up to it, if any
def decompose_credit(ds, line_id: str) -> dict:
    b = next((x for x in ds.bank_lines if x.line_id == line_id), None)
    if b is None:
        return {"error": f"no bank line {line_id}"}
    near = [s for s in ds.settlements
            if s.net_inr == b.credit_inr
            and abs((b.value_date - s.created_at.date()).days) <= 3]
    stages = [{"stage": "verify", "credit_inr": fmt(b.credit_inr),
               "settlements_at_this_amount_and_date": [s.settlement_id for s in near]}]
    if near:
        cls = ("exact_match" if near[0].utr == b.utr else "utr_typo")
        stages.append({"stage": "attribute", "detected_class": cls})
        return {"record_id": line_id, "record_type": "bank_line", "stages": stages,
                "detected_class": cls, "detail": f"matches {near[0].settlement_id}",
                "explained_by": [near[0].settlement_id], "arithmetic_verified": True}

    found = funnel.search_payment_set(ds, b.credit_inr, b.value_date)
    stages.append({"stage": "search", "window_days": 3, "max_items": 8,
                   "result": found or "no subset explains this credit"})
    if found:
        return {"record_id": line_id, "record_type": "bank_line", "stages": stages,
                "detected_class": "timing_split", "detail": f"{len(found)} payments sum exactly",
                "explained_by": found, "arithmetic_verified": True}
    return {"record_id": line_id, "record_type": "bank_line", "stages": stages,
            "detected_class": "unmatched_bank_credit",
            "detail": "search exhausted; no payment subset explains this credit",
            "explained_by": [], "arithmetic_verified": False}


def run(record_id: str, ds=None) -> dict:
    ds = ds or load_dataset(data_root())
    if record_id.startswith("bank"):
        return decompose_credit(ds, record_id)
    return decompose_settlement(ds, record_id)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: solve_decomposition.py <settlement_id|line_id>", file=sys.stderr)
        return 2
    print(json.dumps(run(argv[0]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
