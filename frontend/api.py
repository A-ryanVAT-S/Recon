"""FastAPI backend for the frontend: read the run, work the queue, re-decide a ceiling."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import trace
from MCP import humanloop as hl
from MCP.runs import decisions, reband, report
from agents.policy import earned
from agents.policy.matrix import matrix_source
from core import fmt, money

PORT = 8850
api = FastAPI(title="Recon API", version="1.0.0",
              description="Read-only over completed runs, plus the human approval queue.")


class Decision(BaseModel):
    approval_id: str
    verdict: str
    who: str
    note: str = ""


class Ceiling(BaseModel):
    ceiling_inr: str
    run_id: str | None = None


class Close(BaseModel):
    month: str = "2026-08"
    investigate_top: int = 0
    ablations: list[str] = []


@api.get("/health")
def health() -> dict:
    rid = trace.latest_close()
    return {"ok": True, "latest_run": rid, "scored": bool(
        rid and (trace.run_dir(rid) / "scorecard.json").exists())}


@api.get("/runs")
def runs(limit: int = 25) -> list[dict]:
    base = trace.RUNS
    if not base.is_dir():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if (p / "close_report.json").exists()):
        r = json.loads((d / "close_report.json").read_text(encoding="utf-8"))
        out.append({"run_id": r["run_id"], "month": r.get("month"),
                    "tier": r.get("tier"), "ablations": r.get("ablations", []),
                    "records": r.get("records_processed"),
                    "exceptions": r.get("exceptions"),
                    "auto_resolved": r.get("auto_resolved"),
                    "escalated": r.get("escalated"),
                    "scored": (d / "scorecard.json").exists()})
    return out[-limit:][::-1]


@api.get("/close")
def close_report(run: str | None = None) -> dict:
    r = report(run)
    if "error" in r:
        raise HTTPException(404, r["error"])
    return {k: v for k, v in r.items() if k not in ("escalations", "auto", "investigated")}


@api.get("/exceptions")
def exceptions(run: str | None = None, band: str | None = None, owner: str | None = None,
               detected_class: str | None = None) -> list[dict]:
    rows = [r for r in decisions(run) if r.get("action") != "MATCHED"]
    if band:
        rows = [r for r in rows if r.get("band") == band]
    if owner:
        rows = [r for r in rows if r.get("owner") == owner]
    if detected_class:
        rows = [r for r in rows if r.get("detected_class") == detected_class]
    rows.sort(key=lambda r: money(r.get("exposure_inr", "0")), reverse=True)
    return rows


@api.get("/trace/{record_id}")
def record_trace(record_id: str, run: str | None = None) -> dict:
    rid = run or trace.latest_close()
    if not rid:
        raise HTTPException(404, "no runs yet")
    events = trace.for_record(rid, record_id)
    row = next((r for r in decisions(rid) if r["record_id"] == record_id), None)
    return {"run_id": rid, "record_id": record_id, "row": row,
            "events": [{"at": e.at, "actor": e.actor, "kind": e.kind,
                        "elapsed_ms": e.elapsed_ms, "payload": e.payload} for e in events]}


@api.get("/scorecard")
def scorecard(run: str | None = None) -> dict:
    rid = run or trace.latest_run("scorecard.json")
    path = trace.run_dir(rid) / "scorecard.json" if rid else None
    if not path or not path.exists():
        raise HTTPException(404, "no scorecard yet; run `python -m eval.main score --write`")
    return json.loads(path.read_text(encoding="utf-8"))


@api.get("/ablations")
def ablations(run: str | None = None) -> dict:
    rid = run or trace.latest_run("ablations.json")
    path = trace.run_dir(rid) / "ablations.json" if rid else None
    if not path or not path.exists():
        raise HTTPException(404, "no ablations yet; run `python -m eval.main ablations`")
    return json.loads(path.read_text(encoding="utf-8"))


@api.get("/approvals")
def approvals(owner: str | None = None, include_decided: bool = False) -> list[dict]:
    rows = hl.all_approvals() if include_decided else hl.pending(owner)
    if include_decided and owner:
        rows = [a for a in rows if a.owner == owner]
    return [a.model_dump(mode="json") for a in rows]


@api.get("/approvals/{approval_id}")
def approval(approval_id: str) -> dict:
    a = hl.get(approval_id)
    if a is None:
        raise HTTPException(404, f"no approval {approval_id}")
    return a.model_dump(mode="json") | {"pack_text": hl.pack_text(a)}


# the one write this API accepts, and it needs a named human on it
@api.post("/approvals/decide")
def decide(d: Decision) -> dict:
    try:
        a = hl.decide(d.approval_id, d.verdict, d.who.strip(), d.note.strip())
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    return a.model_dump(mode="json")


# posting a human-approved correction still goes through the Policy Agent for its token
@api.post("/approvals/{approval_id}/post")
async def post_approved(approval_id: str) -> dict:
    from agents.client import call_agent

    out = await call_agent("controller", {"skill": "post_approved",
                                          "approval_id": approval_id})
    return out


# a reviewer asking the investigator to look at this exact item before deciding. read-only:
# the investigator holds no write tool, and the advice it returns is gated but never acted on
@api.post("/approvals/{approval_id}/investigate")
async def investigate_approval(approval_id: str) -> dict:
    from agents.client import call_agent

    a = hl.get(approval_id)
    if a is None:
        raise HTTPException(404, f"no approval {approval_id}")
    return await call_agent("investigator", {"record_id": a.record_id,
                                             "record_type": a.record_type,
                                             "amount_inr": str(a.amount_inr),
                                             "run_id": a.run_id})


@api.post("/whatif")
def whatif(c: Ceiling) -> dict:
    rows = decisions(c.run_id)
    if not rows:
        raise HTTPException(404, "that run wrote no decisions")
    base, alt = reband(rows, money("25000")), reband(rows, money(c.ceiling_inr))
    return {"written_ceiling_inr": "25000.00", "asked_ceiling_inr": fmt(money(c.ceiling_inr)),
            "auto_at_written": base["auto_count"], "auto_at_asked": alt["auto_count"],
            "delta": alt["auto_count"] - base["auto_count"],
            "newly_automated": alt["newly_automated"], "by_band_at_asked": alt["by_band"]}


@api.get("/authority")
def authority() -> dict:
    return {"matrix_source": matrix_source(),
            "rules": [r.model_dump(mode="json") | {"live": r.live()} for r in earned.rules()],
            "candidates": earned.candidates(),
            "precedents": [p.model_dump(mode="json") for p in earned.precedents()],
            "min_precedents": earned.MIN_PRECEDENTS,
            "earned_max_inr": fmt(earned.EARNED_MAX_INR),
            "never_earnable": sorted(earned.NEVER_EARNABLE)}


@api.post("/close")
async def run_close(c: Close) -> dict:
    from agents.client import call_agent

    out = await call_agent("controller", {"skill": "close_month", "month": c.month,
                                          "investigate_top": c.investigate_top,
                                          "ablations": c.ablations})
    return {k: v for k, v in out.items()
            if k not in ("escalations", "auto", "investigated")}


@api.post("/ask")
async def ask(q: dict) -> dict:
    from agents.client import call_agent

    question = (q.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "ask a question")
    return await call_agent("qa", {"skill": "ask", "question": question})
