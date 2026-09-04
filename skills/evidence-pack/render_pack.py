"""Everything a reviewer needs for one exception, in the order they need it."""

from __future__ import annotations

import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import ZERO, Proposal, RunContext, fmt, load_dataset, money, rupees
from dataset import verify as funnel
from MCP.servers import data_root
from agents.policy.engine import evaluate, load_matrix, scan_for_instructions


# the record's own third-party text, which is exactly what an attacker controls
def source_text(ds, record_id: str) -> tuple[str, str]:
    if record_id.startswith("setl"):
        s = ds.settlements_by_id().get(record_id)
        return ("settlement.remark", s.remark) if s else ("", "")
    if record_id.startswith("bank"):
        b = next((x for x in ds.bank_lines if x.line_id == record_id), None)
        return ("bank_line.narration", b.narration) if b else ("", "")
    if record_id.startswith("pay"):
        p = ds.payments_by_id().get(record_id)
        return ("payment.notes", p.notes) if p else ("", "")
    if record_id.startswith("rfnd"):
        r = ds.refunds_by_id().get(record_id)
        return ("refund.reason", r.reason) if r else ("", "")
    return "", ""


# find this record in the funnel's output so the pack quotes the real finding
def _finding(ds, record_id: str, res=None):
    res = res or funnel.run_funnel(ds)
    return next((e for e in res.exceptions if e.record_id == record_id), None)


# the ask, the arithmetic, the chain, the rule that fired, and the untrusted text.
# finding is the caller's already-computed exception, so a batch of packs runs the funnel once.
def build(record_id: str, ds=None, res=None, matrix=None, finding: dict | None = None) -> dict:
    ds = ds or load_dataset(data_root())
    matrix = matrix or load_matrix()
    if finding is not None:
        exc = funnel.Exc(record_id, finding.get("record_type", ""),
                         finding.get("detected_class", "unexplained"),
                         money(finding.get("delta_inr", 0)),
                         money(finding.get("amount_inr", 0)), finding.get("detail", ""))
    else:
        exc = _finding(ds, record_id, res)
    if exc is None:
        return {"error": f"{record_id} is not an open exception in this run"}

    field, text = source_text(ds, record_id)
    delta = money(exc.delta_inr)
    prop = Proposal(
        proposal_id="prop_" + uuid.uuid4().hex[:10], record_id=record_id,
        record_type=exc.record_type, proposed_class=exc.detected_class,
        proposed_action="resolve", amount_inr=money(abs(delta)),
        confidence=Decimal("0.90"), arithmetic_verified=exc.detected_class != "unexplained",
        delta_inr=delta, evidence_chain=[record_id], period="",
        source_texts=[text], generated_by="evidence-pack")
    d = evaluate(prop, RunContext(run_id="pack"), matrix)

    arithmetic = {}
    if record_id.startswith("setl") and (s := ds.settlements_by_id().get(record_id)):
        gross, fee, gst, net = funnel.recompute_settlement(ds, s)
        arithmetic = {"expected_net_inr": fmt(net), "observed_net_inr": fmt(s.net_inr),
                      "delta_inr": fmt(money(net - s.net_inr)),
                      "expected_fee_inr": fmt(fee), "observed_fee_inr": fmt(s.fee_inr),
                      "payment_count": len(s.payment_ids),
                      "method": "recomputed per payment from the fee schedule in force"}

    return {
        "pack_id": "pack_" + uuid.uuid4().hex[:10],
        "ask": (f"{exc.detected_class} on {record_id}: "
                f"{'resolve' if d.allowed else 'review'} an exposure of "
                f"Rs {rupees(abs(delta))}"),
        "record_id": record_id, "record_type": exc.record_type,
        "detected_class": exc.detected_class, "detail": exc.detail,
        "exposure_inr": fmt(abs(delta)), "record_value_inr": fmt(exc.amount_inr),
        "arithmetic": arithmetic,
        "evidence_chain": [record_id],
        "band": d.band, "allowed": d.allowed, "owner": d.owner,
        "why_here": d.reasons, "rules_fired": d.rules_fired,
        "if_approved": {"post": "ledger adjustment", "record_id": record_id,
                        "amount_inr": fmt(abs(delta)), "action": "resolve"},
        "untrusted_text": {"field": field, "verbatim": text,
                           "scanner_flags": scan_for_instructions(text)} if text else None,
        "proposal": prop.model_dump(mode="json"),
    }


# the same pack as something a human reads in a terminal or an email body
def to_text(pack: dict) -> str:
    if "error" in pack:
        return pack["error"]
    L = [f"ASK   {pack['ask']}", f"      {pack['record_id']}  ({pack['record_type']})", ""]
    if pack["arithmetic"]:
        a = pack["arithmetic"]
        L += ["NUMBER",
              f"      expected net  {a['expected_net_inr']:>16}",
              f"      observed net  {a['observed_net_inr']:>16}",
              f"      delta         {a['delta_inr']:>16}",
              f"      over {a['payment_count']} payments, {a['method']}", ""]
    L += [f"FINDING  {pack['detected_class']}  -  {pack['detail']}", "",
          f"CHAIN    {', '.join(pack['evidence_chain'])}", "",
          f"BAND     {pack['band']}   owner {pack['owner'] or '-'}"]
    L += [f"         {r}" for r in pack["why_here"]]
    L += ["", f"IF APPROVED  post {pack['if_approved']['action']} of "
              f"Rs {pack['if_approved']['amount_inr']} against {pack['record_id']}"]
    if pack["untrusted_text"]:
        u = pack["untrusted_text"]
        L += ["", "UNTRUSTED TEXT FOUND ON THE RECORD - shown, not acted on",
              f"      field {u['field']}",
              f"      flags {', '.join(u['scanner_flags']) or 'none'}",
              "      +" + "-" * 68]
        for line in (u["verbatim"] or "").splitlines() or [""]:
            L.append("      | " + line[:66])
        L.append("      +" + "-" * 68)
    return "\n".join(L)


def run(record_id: str, ds=None) -> dict:
    return build(record_id, ds=ds)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: render_pack.py <record_id> [--text]", file=sys.stderr)
        return 2
    pack = build(argv[0])
    print(to_text(pack) if "--text" in argv else json.dumps(pack, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
