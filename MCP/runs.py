"""runs-mcp: read-only tools over completed runs. No writes, and no ground truth."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

from mcp.server.mcpserver import MCPServer

import trace
from core import Proposal, RunContext, fmt, money
from agents.policy.engine import evaluate, load_matrix

MAX_ROWS = 100


def _run_id(run_id: str | None) -> Optional[str]:
    return run_id or trace.latest_close()


def report(run_id: str | None = None) -> dict:
    rid = _run_id(run_id)
    if not rid:
        return {"error": "no runs yet"}
    path = trace.run_dir(rid) / "close_report.json"
    if not path.exists():
        return {"error": f"{rid} has no close_report.json"}
    return json.loads(path.read_text(encoding="utf-8"))


# one line per record, written by the Controller: the run's own account of what it did
def decisions(run_id: str | None = None) -> list[dict]:
    rid = _run_id(run_id)
    if not rid:
        return []
    path = trace.run_dir(rid) / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# re-band a finished run at a different AUTO_RESOLVE ceiling, in code, from the run's own rows
def rebandable(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("action") != "MATCHED"]


def reband(rows: list[dict], ceiling: Decimal, matrix: dict | None = None) -> dict:
    m = json.loads(json.dumps(matrix or load_matrix(), default=str))
    m["bands"]["AUTO_RESOLVE"]["max_amount_inr"] = str(money(ceiling))
    ctx = RunContext(run_id="whatif")
    out: dict[str, int] = {}
    moved: list[dict] = []
    for r in rebandable(rows):
        p = Proposal(proposal_id="whatif", record_id=r["record_id"],
                     record_type=r.get("record_type", ""),
                     proposed_class=r.get("detected_class", ""),
                     proposed_action="resolve", amount_inr=money(r.get("exposure_inr", "0")),
                     arithmetic_verified=bool(r.get("arithmetic_verified")),
                     evidence_chain=[r["record_id"]],
                     source_texts=[r.get("source_text", "")])
        d = evaluate(p, ctx, m)
        out[d.band] = out.get(d.band, 0) + 1
        if d.allowed and r.get("band") not in ("AUTO_RESOLVE", "AUTO_WITH_NOTICE"):
            moved.append({"record_id": r["record_id"],
                          "detected_class": r.get("detected_class"),
                          "exposure_inr": r.get("exposure_inr"),
                          "was": r.get("band"), "now": d.band})
    return {"ceiling_inr": fmt(money(ceiling)), "by_band": out,
            "auto_count": sum(n for b, n in out.items()
                              if b in ("AUTO_RESOLVE", "AUTO_WITH_NOTICE")),
            "newly_automated": moved}


# what the Q&A agent may see: a run's own output, never the answer key
def build_runs() -> MCPServer:
    mcp = MCPServer(
        name="runs-mcp",
        instructions="Completed reconciliation runs: reports, per-record decisions and "
                     "decision traces. Every tool is read-only. This server holds no "
                     "labels and no ground truth, so it cannot tell you whether a "
                     "decision was correct - only what the decision was and why.",
    )

    @mcp.tool(description="Completed runs, newest last.")
    def list_runs(limit: int = 20) -> list[dict]:
        out = []
        base = trace.RUNS
        if not base.is_dir():
            return out
        for d in sorted(p for p in base.iterdir() if (p / "close_report.json").exists()):
            r = json.loads((d / "close_report.json").read_text(encoding="utf-8"))
            out.append({"run_id": r["run_id"], "month": r.get("month"),
                        "records": r.get("records_processed"),
                        "exceptions": r.get("exceptions"),
                        "auto_resolved": r.get("auto_resolved"),
                        "escalated": r.get("escalated")})
        return out[-min(limit, MAX_ROWS):]

    @mcp.tool(description="The close report for a run. Omit run_id for the latest run.")
    def get_close_report(run_id: str | None = None) -> dict:
        r = report(run_id)
        return {k: v for k, v in r.items() if k not in ("escalations", "auto", "investigated")}

    @mcp.tool(description="Exceptions from a run, filterable by band, owner or class. "
                          "Every row carries why it landed where it did.")
    def list_exceptions(run_id: str | None = None, band: str | None = None,
                        owner: str | None = None, detected_class: str | None = None,
                        limit: int = 50) -> list[dict]:
        rows = [r for r in decisions(run_id) if r.get("action") != "MATCHED"]
        if band:
            rows = [r for r in rows if r.get("band") == band]
        if owner:
            rows = [r for r in rows if r.get("owner") == owner]
        if detected_class:
            rows = [r for r in rows if r.get("detected_class") == detected_class]
        rows.sort(key=lambda r: money(r.get("exposure_inr", "0")), reverse=True)
        return rows[:min(limit, MAX_ROWS)]

    @mcp.tool(description="Every decision recorded for one record, in order. This is the "
                          "audit trail: who proposed what, and which rule decided it.")
    def get_record_trace(record_id: str, run_id: str | None = None) -> dict:
        rid = _run_id(run_id)
        if not rid:
            return {"error": "no runs yet"}
        events = trace.for_record(rid, record_id)
        return {"run_id": rid, "record_id": record_id, "event_count": len(events),
                "events": [{"at": e.at, "actor": e.actor, "kind": e.kind,
                            "elapsed_ms": e.elapsed_ms, **e.payload}
                           for e in events[:MAX_ROWS]]}

    @mcp.tool(description="Totals by detected class for a run.")
    def exception_totals(run_id: str | None = None) -> dict:
        rows = [r for r in decisions(run_id) if r.get("action") != "MATCHED"]
        by: dict[str, dict] = {}
        for r in rows:
            k = r.get("detected_class", "?")
            cell = by.setdefault(k, {"count": 0, "exposure_inr": money(0)})
            cell["count"] += 1
            cell["exposure_inr"] = money(cell["exposure_inr"] + money(r.get("exposure_inr", "0")))
        return {"run_id": _run_id(run_id),
                "by_class": {k: {"count": v["count"], "exposure_inr": fmt(v["exposure_inr"])}
                             for k, v in sorted(by.items())}}

    @mcp.tool(description="The written authority matrix the run was decided under.")
    def get_authority_matrix() -> dict:
        return json.loads(json.dumps(load_matrix(), default=str))

    @mcp.tool(description="Re-decide a finished run at a different AUTO_RESOLVE ceiling. "
                          "Recomputed by the policy engine over the run's own rows, so the "
                          "answer is exact rather than an estimate. Hard stops still hold.")
    def what_if_ceiling(ceiling_inr: str, run_id: str | None = None) -> dict:
        rows = decisions(run_id)
        if not rows:
            return {"error": "that run wrote no decisions.jsonl"}
        base = reband(rows, money("25000"))
        alt = reband(rows, money(ceiling_inr))
        return {"run_id": _run_id(run_id), "written_ceiling_inr": "25000.00",
                "asked_ceiling_inr": fmt(money(ceiling_inr)),
                "auto_at_written": base["auto_count"], "auto_at_asked": alt["auto_count"],
                "delta": alt["auto_count"] - base["auto_count"],
                "newly_automated": alt["newly_automated"][:MAX_ROWS],
                "by_band_at_asked": alt["by_band"]}

    return mcp
