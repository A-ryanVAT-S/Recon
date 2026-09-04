"""Earned authority: human decisions become precedents, precedents become expiring rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from core import fmt, money

REPO = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("RECON_STATE_DIR", REPO / "runs" / "_state"))
SECRET = os.environ.get("RECON_HUMANLOOP_SECRET", "dev-only-humanloop-secret").encode()
_LOCK = threading.Lock()

MIN_PRECEDENTS = 5              # below this a pattern is a coincidence, not a practice
EARNED_MAX_INR = money("100000")   # an earned rule can never outrank the written ceiling
DEFAULT_TTL_DAYS = 90           # authority that never expires is authority nobody re-reads

# a class whose correct outcome is a human can never be earned away, at any N
NEVER_EARNABLE = {"duplicate_payment", "fee_overcharge", "missing_bank_credit",
                  "unknown_remitter", "chargeback_deduction", "unexplained",
                  "payment_net_mismatch", "orphan_refund", "orphan_dispute",
                  "refund_over_payment"}


class Precedent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    precedent_id: str = Field(default_factory=lambda: "prec_" + uuid.uuid4().hex[:10])
    signature: str
    record_id: str
    record_type: str
    detected_class: str
    amount_inr: Decimal
    decision: str                  # APPROVE or REJECT, as the human gave it
    human_id: str
    counterparty_id: Optional[str] = None
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    attestation: str = ""          # hmac over the decision; text in tool output cannot forge it


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(default_factory=lambda: "rule_" + uuid.uuid4().hex[:8])
    version: int = 1
    signature: str
    detected_class: str
    record_type: str
    counterparty_id: Optional[str] = None
    ceiling_inr: Decimal
    status: str = "proposed"       # proposed -> active -> expired | revoked
    supported_by: list[str] = Field(default_factory=list)
    proposed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    activated_at: Optional[str] = None
    activated_by: Optional[str] = None
    valid_until: Optional[str] = None
    revoked_reason: str = ""

    def live(self, now: datetime | None = None) -> bool:
        if self.status != "active" or not self.valid_until:
            return False
        now = now or datetime.now(timezone.utc)
        return now <= datetime.fromisoformat(self.valid_until)


# the coarse pattern a precedent is about; amount is bucketed so near-misses still match
def signature(record_type: str, detected_class: str, amount: Decimal,
              counterparty_id: str | None = None) -> str:
    a = money(amount)
    for cap, name in ((money("1000"), "u1k"), (money("25000"), "u25k"),
                      (money("100000"), "u1L"), (money("500000"), "u5L")):
        if a <= cap:
            bucket = name
            break
    else:
        bucket = "over5L"
    return "|".join([record_type, detected_class, bucket, counterparty_id or "-"])


# what a human signed; nothing a model reads can produce this
def attest(record_id: str, decision: str, human_id: str) -> str:
    body = f"{record_id}|{decision}|{human_id}"
    return hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]


def verify_attestation(p: Precedent) -> bool:
    return hmac.compare_digest(p.attestation, attest(p.record_id, p.decision, p.human_id))


def _path(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / name


def _append(name: str, obj: BaseModel) -> None:
    with _LOCK:
        with _path(name).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(obj.model_dump_json() + "\n")


def _read(name: str, model):
    path = _path(name)
    if not path.exists():
        return []
    return [model(**json.loads(l)) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def precedents() -> list[Precedent]:
    return [p for p in _read("precedents.jsonl", Precedent) if verify_attestation(p)]


def rules() -> list[Rule]:
    latest: dict[str, Rule] = {}
    for r in _read("rules.jsonl", Rule):
        prior = latest.get(r.rule_id)
        if prior is None or r.version >= prior.version:
            latest[r.rule_id] = r
    return list(latest.values())


# the only door into the precedent store, and it needs a human's signature to open
def record_decision(record_id: str, record_type: str, detected_class: str,
                    amount: Decimal, decision: str, human_id: str,
                    attestation: str, counterparty_id: str | None = None) -> Precedent:
    if attestation != attest(record_id, decision, human_id):
        raise PermissionError("precedents require a human decision attestation")
    p = Precedent(signature=signature(record_type, detected_class, money(amount),
                                      counterparty_id),
                  record_id=record_id, record_type=record_type,
                  detected_class=detected_class, amount_inr=money(amount),
                  decision=decision, human_id=human_id,
                  counterparty_id=counterparty_id, attestation=attestation)
    _append("precedents.jsonl", p)
    return p


# N consistent approvals on one signature, from more than one person, and no rejection
def candidates() -> list[dict]:
    by_sig: dict[str, list[Precedent]] = {}
    for p in precedents():
        by_sig.setdefault(p.signature, []).append(p)

    known = {r.signature for r in rules() if r.status in ("proposed", "active")}
    out = []
    for sig, group in sorted(by_sig.items()):
        approvals = [p for p in group if p.decision == "APPROVE"]
        cls = group[0].detected_class
        blockers = []
        if cls in NEVER_EARNABLE:
            blockers.append(f"{cls} is never earnable")
        if any(p.decision != "APPROVE" for p in group):
            blockers.append("a human rejected this signature at least once")
        if len(approvals) < MIN_PRECEDENTS:
            blockers.append(f"{len(approvals)} approvals, needs {MIN_PRECEDENTS}")
        if len({p.human_id for p in approvals}) < 2:
            blockers.append("all approvals came from one person")
        if sig in known:
            blockers.append("a rule already exists for this signature")
        out.append({"signature": sig, "detected_class": cls,
                    "record_type": group[0].record_type,
                    "counterparty_id": group[0].counterparty_id,
                    "approvals": len(approvals), "total": len(group),
                    "max_amount_inr": fmt(max(p.amount_inr for p in approvals))
                    if approvals else "0.00",
                    "eligible": not blockers, "blockers": blockers,
                    "supported_by": [p.precedent_id for p in approvals]})
    return out


# a proposal only; it grants nothing until a human activates it
def propose_rule(sig: str) -> Rule:
    cand = next((c for c in candidates() if c["signature"] == sig), None)
    if cand is None:
        raise ValueError(f"no precedents for signature {sig}")
    if not cand["eligible"]:
        raise PermissionError("; ".join(cand["blockers"]))
    ceiling = min(money(cand["max_amount_inr"]), EARNED_MAX_INR)
    r = Rule(signature=sig, detected_class=cand["detected_class"],
             record_type=cand["record_type"], counterparty_id=cand["counterparty_id"],
             ceiling_inr=ceiling, supported_by=cand["supported_by"])
    _append("rules.jsonl", r)
    return r


# one human approval turns a proposal into authority, and it starts expiring immediately
def activate_rule(rule_id: str, human_id: str, attestation: str,
                  ttl_days: int = DEFAULT_TTL_DAYS) -> Rule:
    if attestation != attest(rule_id, "ACTIVATE", human_id):
        raise PermissionError("activating a rule requires a human decision attestation")
    r = next((x for x in rules() if x.rule_id == rule_id), None)
    if r is None:
        raise ValueError(f"no rule {rule_id}")
    if r.detected_class in NEVER_EARNABLE:
        raise PermissionError(f"{r.detected_class} is never earnable")
    nxt = r.model_copy(update={
        "version": r.version + 1, "status": "active",
        "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "activated_by": human_id,
        "ceiling_inr": min(r.ceiling_inr, EARNED_MAX_INR),
        "valid_until": (datetime.now(timezone.utc)
                        + timedelta(days=ttl_days)).isoformat(timespec="seconds")})
    _append("rules.jsonl", nxt)
    return nxt


def revoke_rule(rule_id: str, reason: str) -> Rule:
    r = next((x for x in rules() if x.rule_id == rule_id), None)
    if r is None:
        raise ValueError(f"no rule {rule_id}")
    nxt = r.model_copy(update={"version": r.version + 1, "status": "revoked",
                               "revoked_reason": reason})
    _append("rules.jsonl", nxt)
    return nxt


# the ceiling an active rule grants this proposal, or None. Never above EARNED_MAX_INR.
def ceiling_for(p, now: datetime | None = None) -> tuple[Optional[Decimal], Optional[str]]:
    if p.proposed_class in NEVER_EARNABLE:
        return None, None
    sig = signature(p.record_type, p.proposed_class, money(p.amount_inr))
    for r in rules():
        if not r.live(now):
            continue
        if r.detected_class != p.proposed_class or r.record_type != p.record_type:
            continue
        if r.signature != sig and r.counterparty_id is None:
            continue
        if money(p.amount_inr) > min(r.ceiling_inr, EARNED_MAX_INR):
            continue
        return min(r.ceiling_inr, EARNED_MAX_INR), r.rule_id
    return None, None


def clear() -> None:
    for name in ("precedents.jsonl", "rules.jsonl"):
        _path(name).unlink(missing_ok=True)
