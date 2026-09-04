"""Owner, second signer and urgency for one exception. A lookup, never a judgement."""

from __future__ import annotations

import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import Proposal, fmt, load_dataset, money
from dataset import verify as funnel
from MCP.servers import data_root
from agents.policy.engine import load_matrix, route as route_owner

P1_INR = money("200000")
P2_INR = money("25000")


# orders the queue; never touches the band, which is the whole point
def urgency(amount: Decimal, counterparty_in_master: bool = True) -> str:
    if amount > P1_INR or not counterparty_in_master:
        return "P1"
    return "P2" if amount > P2_INR else "P3"


# the second signer, where the matrix demands one; duplicates are the only such class
def second_signer(detected_class: str, matrix: dict) -> str | None:
    for rule in matrix["routing"]:
        if detected_class in rule.get("if_class", []):
            return rule.get("dual")
    return None


def run(detected_class: str, amount: str | Decimal = "0",
        counterparty_in_master: bool = True, matrix=None) -> dict:
    matrix = matrix or load_matrix()
    amt = money(amount)
    p = Proposal(proposal_id="prop_" + uuid.uuid4().hex[:10], record_id="-",
                 record_type="-", proposed_class=detected_class,
                 proposed_action="resolve", amount_inr=amt)
    owner = route_owner(p, matrix)
    return {"detected_class": detected_class, "amount_inr": fmt(amt),
            "owner": owner, "second_signer": second_signer(detected_class, matrix),
            "urgency": urgency(amt, counterparty_in_master),
            "sla_hours": {"P1": 4, "P2": 24, "P3": 72}[urgency(amt, counterparty_in_master)],
            "source": "agents/policy/authority.yaml"}


# route a real open exception, taking its class and exposure from the funnel
def run_record(record_id: str, ds=None) -> dict:
    ds = ds or load_dataset(data_root())
    exc = next((e for e in funnel.run_funnel(ds).exceptions if e.record_id == record_id), None)
    if exc is None:
        return {"error": f"{record_id} is not an open exception"}
    out = run(exc.detected_class, abs(money(exc.delta_inr)))
    out |= {"record_id": record_id, "detail": exc.detail}
    return out


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--record":
        if len(argv) < 2:
            print("usage: route.py --record <record_id>", file=sys.stderr)
            return 2
        print(json.dumps(run_record(argv[1]), indent=2))
        return 0
    if not argv:
        print("usage: route.py <detected_class> [amount_inr] | --record <id>", file=sys.stderr)
        return 2
    print(json.dumps(run(argv[0], argv[1] if len(argv) > 1 else "0"), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
