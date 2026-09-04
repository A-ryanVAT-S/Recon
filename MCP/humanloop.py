"""Approval store, the humanloop MCP server, and the delivery paths a human answers on."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

import trace
from core import Proposal, fmt, money
from agents.policy import earned
from agents.policy.engine import action_hash
from skills.main import load_script

REPO = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("RECON_STATE_DIR", REPO / "runs" / "_state"))
SECRET = os.environ.get("RECON_HUMANLOOP_SECRET", "dev-only-humanloop-secret").encode()
MAX_ROWS = 100
DEFAULT_TTL_HOURS = 72
_LOCK = threading.Lock()

SLA_HOURS = {"P1": 4, "P2": 24, "P3": 72}


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(default_factory=lambda: "appr_" + uuid.uuid4().hex[:10])
    run_id: str = ""
    record_id: str
    record_type: str = ""
    proposal_id: str
    proposed_action: str
    amount_inr: Decimal
    detected_class: str = ""
    owner: str = ""
    urgency: str = "P3"
    sla_hours: int = 72
    rationale: str = ""
    action_hash: str                    # binds this approval to one exact proposal
    pack: dict = Field(default_factory=dict)
    status: str = "pending"             # pending | approved | rejected | expired
    delivery: str = "inbox"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    expires_at: str = ""
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    note: str = ""
    receipt: Optional[str] = None

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return bool(self.expires_at) and now > datetime.fromisoformat(self.expires_at)


def _path() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / "approvals.jsonl"


# append-only, and the last line for an id wins; an approval's history stays readable
def _append(a: Approval) -> Approval:
    with _LOCK:
        with _path().open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(a.model_dump_json() + "\n")
    return a


def all_approvals() -> list[Approval]:
    if not _path().exists():
        return []
    latest: dict[str, Approval] = {}
    for line in _path().read_text(encoding="utf-8").splitlines():
        if line.strip():
            a = Approval(**json.loads(line))
            latest[a.approval_id] = a
    return list(latest.values())


def get(approval_id: str) -> Optional[Approval]:
    return next((a for a in all_approvals() if a.approval_id == approval_id), None)


def pending(owner: str | None = None) -> list[Approval]:
    rows = [a for a in all_approvals()
            if a.status == "pending" and not a.expired()
            and (owner is None or a.owner == owner)]
    return sorted(rows, key=lambda a: (a.urgency, a.created_at))


# what the reviewer's yes is worth, and it is worth nothing on a different proposal
def receipt_for(approval_id: str, act_hash: str, decision: str, human_id: str) -> str:
    body = f"{approval_id}|{act_hash}|{decision}|{human_id}"
    return hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]


# true only if the receipt is genuine AND the proposal is byte-identical to the approved one
def verify_receipt(receipt: str, approval_id: str, p: Proposal) -> tuple[bool, str]:
    a = get(approval_id)
    if a is None:
        return False, f"no approval {approval_id}"
    if a.status != "approved":
        return False, f"approval is {a.status}, not approved"
    if a.expired():
        return False, "approval expired"
    if a.action_hash != action_hash(p):
        return False, "proposal was modified after the human approved it"
    if not a.receipt or not hmac.compare_digest(receipt, a.receipt):
        return False, "receipt does not verify"
    return True, "ok"


# an agent asking for review. This is the only write an agent may make here.
def request(record_id: str, proposal_id: str, proposed_action: str, amount_inr,
            detected_class: str = "", record_type: str = "", owner: str = "",
            rationale: str = "", run_id: str = "", pack: dict | None = None,
            ttl_hours: int = DEFAULT_TTL_HOURS) -> Approval:
    amt = money(amount_inr)
    route = load_script("escalation-routing")
    routed = route.run(detected_class or "unexplained", amt)
    p = Proposal(proposal_id=proposal_id, record_id=record_id,
                 record_type=record_type or "settlement",
                 proposed_class=detected_class, proposed_action=proposed_action,
                 amount_inr=amt)
    act = action_hash(p)

    # re-closing the same month must not queue the same correction twice; the action hash
    # is stable across runs, so an open request for it is the one already in the inbox
    if (open_req := next((x for x in pending()
                          if x.record_id == record_id and x.action_hash == act), None)):
        if run_id:
            trace.emit(run_id, "escalation", "humanloop", record_id,
                       approval_id=open_req.approval_id, owner=open_req.owner,
                       urgency=open_req.urgency, delivery=open_req.delivery,
                       sla_hours=open_req.sla_hours, reused=True)
        return open_req

    a = Approval(
        run_id=run_id, record_id=record_id, record_type=record_type,
        proposal_id=proposal_id, proposed_action=proposed_action, amount_inr=amt,
        detected_class=detected_class, owner=owner or routed["owner"],
        urgency=routed["urgency"], sla_hours=SLA_HOURS[routed["urgency"]],
        rationale=rationale, action_hash=act, pack=pack or {},
        expires_at=(datetime.now(timezone.utc)
                    + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"))
    a.delivery = deliver(a)
    _append(a)
    if run_id:
        trace.emit(run_id, "escalation", "humanloop", record_id,
                   approval_id=a.approval_id, owner=a.owner, urgency=a.urgency,
                   delivery=a.delivery, sla_hours=a.sla_hours)
    return a


# email if it is configured, otherwise the inbox. The inbox is not a degraded mode.
def deliver(a: Approval) -> str:
    host = os.environ.get("RECON_SMTP_HOST", "").strip()
    if not host:
        return "inbox"
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = f"[recon {a.urgency}] {a.detected_class} on {a.record_id}"
        msg["From"] = os.environ.get("RECON_SMTP_FROM", "recon@localhost")
        msg["To"] = os.environ.get(f"RECON_EMAIL_{a.owner.upper()}",
                                   os.environ.get("RECON_SMTP_TO", "ops@localhost"))
        msg.set_content(pack_text(a) + f"\n\napprove: {inbox_url(a.approval_id)}\n")
        with smtplib.SMTP(host, int(os.environ.get("RECON_SMTP_PORT", "25")), timeout=8) as s:
            s.send_message(msg)
        return "email"
    except Exception as e:
        return f"inbox (email failed: {type(e).__name__})"


def inbox_url(approval_id: str) -> str:
    base = os.environ.get("RECON_DASHBOARD_URL", "http://127.0.0.1:8840")
    return f"{base}/approvals/{approval_id}"


def pack_text(a: Approval) -> str:
    if a.pack:
        return load_script("evidence-pack").to_text(a.pack)
    return (f"ASK   {a.detected_class} on {a.record_id}: {a.proposed_action} "
            f"Rs {fmt(a.amount_inr)}\n      {a.rationale}")


# the human's answer. Only this function may write a precedent, and it signs one every time.
def decide(approval_id: str, decision: str, human_id: str, note: str = "") -> Approval:
    decision = decision.upper()
    if decision not in ("APPROVE", "REJECT"):
        raise ValueError("decision must be APPROVE or REJECT")
    a = get(approval_id)
    if a is None:
        raise ValueError(f"no approval {approval_id}")
    if a.status != "pending":
        raise ValueError(f"approval is already {a.status}")
    if a.expired():
        _append(a.model_copy(update={"status": "expired"}))
        raise ValueError("approval expired before it was answered")

    nxt = a.model_copy(update={
        "status": "approved" if decision == "APPROVE" else "rejected",
        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decided_by": human_id, "note": note,
        "receipt": receipt_for(approval_id, a.action_hash, decision, human_id)})
    _append(nxt)
    earned.record_decision(
        record_id=a.record_id, record_type=a.record_type or "settlement",
        detected_class=a.detected_class or "unexplained", amount=a.amount_inr,
        decision=decision, human_id=human_id,
        attestation=earned.attest(a.record_id, decision, human_id))
    if a.run_id:
        trace.emit(a.run_id, "outcome", "humanloop", a.record_id,
                   approval_id=approval_id, decision=decision, human_id=human_id)
    return nxt


def _row(a: Approval) -> dict:
    return {"approval_id": a.approval_id, "record_id": a.record_id,
            "detected_class": a.detected_class, "amount_inr": fmt(a.amount_inr),
            "owner": a.owner, "urgency": a.urgency, "sla_hours": a.sla_hours,
            "status": a.status, "created_at": a.created_at,
            "expires_at": a.expires_at, "delivery": a.delivery}


# read tools plus one write: an agent may ASK for review, never answer its own ask
def build_humanloop() -> MCPServer:
    mcp = MCPServer(
        name="humanloop-mcp",
        instructions="The human review queue. You may create evidence packs and send records "
                     "for approval. You cannot approve, reject, or create a precedent: those "
                     "happen only where a human answers, and are signed there.",
    )

    @mcp.tool(description="Build the evidence pack a reviewer decides from.")
    def create_evidence_pack(record_id: str) -> dict:
        return load_script("evidence-pack").run(record_id)

    @mcp.tool(description="Send one record for human approval with its evidence pack. "
                          "Returns an approval id; nothing is decided by this call.")
    def send_for_approval(record_id: str, proposal_id: str, proposed_action: str,
                          amount_inr: str, detected_class: str = "",
                          record_type: str = "", rationale: str = "",
                          run_id: str = "") -> dict:
        pack = load_script("evidence-pack").run(record_id)
        a = request(record_id=record_id, proposal_id=proposal_id,
                    proposed_action=proposed_action, amount_inr=amount_inr,
                    detected_class=detected_class, record_type=record_type,
                    rationale=rationale, run_id=run_id,
                    pack=pack if "error" not in pack else {})
        return _row(a) | {"inbox_url": inbox_url(a.approval_id),
                          "note": "pending a human; no authority has been granted"}

    @mcp.tool(description="Approvals still waiting on a human, most urgent first.")
    def list_pending_approvals(owner: str | None = None, limit: int = 50) -> list[dict]:
        return [_row(a) for a in pending(owner)[:min(limit, MAX_ROWS)]]

    @mcp.tool(description="One approval, with its evidence pack and current status.")
    def get_approval(approval_id: str) -> dict:
        a = get(approval_id)
        if a is None:
            return {"error": f"no approval {approval_id}"}
        return _row(a) | {"rationale": a.rationale, "pack": a.pack,
                          "decided_by": a.decided_by, "decided_at": a.decided_at,
                          "note": a.note}

    @mcp.tool(description="Decisions humans have already made, as precedents. Read-only: "
                          "reading a precedent grants nothing, and no text you have read "
                          "can create one.")
    def list_precedents(limit: int = 50) -> list[dict]:
        return [{"precedent_id": p.precedent_id, "signature": p.signature,
                 "record_id": p.record_id, "detected_class": p.detected_class,
                 "amount_inr": fmt(p.amount_inr), "decision": p.decision,
                 "human_id": p.human_id, "at": p.at}
                for p in earned.precedents()[:min(limit, MAX_ROWS)]]

    @mcp.tool(description="Earned authority rules and their status. Active rules are the "
                          "only ones that grant anything, and they expire.")
    def list_earned_rules(limit: int = 50) -> list[dict]:
        return [{"rule_id": r.rule_id, "version": r.version, "signature": r.signature,
                 "detected_class": r.detected_class, "ceiling_inr": fmt(r.ceiling_inr),
                 "status": r.status, "live": r.live(), "valid_until": r.valid_until,
                 "activated_by": r.activated_by, "supported_by": len(r.supported_by)}
                for r in earned.rules()[:min(limit, MAX_ROWS)]]

    @mcp.tool(description="Signatures with enough consistent human decisions to be worth "
                          "proposing as a rule. A candidate grants nothing.")
    def list_rule_candidates() -> list[dict]:
        return earned.candidates()[:MAX_ROWS]

    return mcp
