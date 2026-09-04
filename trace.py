"""Append-only decision trail. Written before the agents exist so no step goes unrecorded."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

REPO = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("RECON_RUNS_DIR", REPO / "runs"))
_LOCK = threading.Lock()

KINDS = ("run_start", "a2a", "tool_call", "proposal", "verification", "policy",
         "outcome", "escalation", "note", "run_end")


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: "ev_" + uuid.uuid4().hex[:12])
    run_id: str
    record_id: Optional[str] = None
    kind: str
    actor: str
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    elapsed_ms: Optional[int] = None
    payload: dict[str, Any] = Field(default_factory=dict)


def run_dir(run_id: str) -> Path:
    d = RUNS / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


# one line per event; append-only so a crash cannot lose what already happened
def emit(run_id: str, kind: str, actor: str, record_id: str | None = None,
         elapsed_ms: int | None = None, **payload) -> TraceEvent:
    ev = TraceEvent(run_id=run_id, record_id=record_id, kind=kind, actor=actor,
                    elapsed_ms=elapsed_ms, payload=payload)
    with _LOCK:
        with (run_dir(run_id) / "trace.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(ev.model_dump_json() + "\n")
    return ev


def read(run_id: str) -> list[TraceEvent]:
    path = run_dir(run_id) / "trace.jsonl"
    if not path.exists():
        return []
    return [TraceEvent(**json.loads(l)) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# the whole chain for one record, in order: this is what the Trace Viewer renders
def for_record(run_id: str, record_id: str) -> list[TraceEvent]:
    return [e for e in read(run_id) if e.record_id == record_id]


# needs= names the artifact that makes a run interesting; a close leaves close_report.json
def latest_run(needs: str = "trace.jsonl") -> Optional[str]:
    if not RUNS.is_dir():
        return None
    runs = sorted(d.name for d in RUNS.iterdir() if (d / needs).exists())
    return runs[-1] if runs else None


def latest_close() -> Optional[str]:
    return latest_run("decisions.jsonl")


# times a block and emits one event when it finishes, however it finishes
class span:
    def __init__(self, run_id: str, kind: str, actor: str, record_id: str | None = None,
                 **payload):
        self.args = (run_id, kind, actor, record_id)
        self.payload = payload
        self.t0 = 0.0

    def __enter__(self) -> "span":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        run_id, kind, actor, record_id = self.args
        if exc is not None:
            self.payload["error"] = f"{exc_type.__name__}: {exc}"
        emit(run_id, kind, actor, record_id,
             elapsed_ms=int((time.perf_counter() - self.t0) * 1000), **self.payload)
        return False
