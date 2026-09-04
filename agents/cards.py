"""A2A agent cards. What each agent advertises, and where it listens."""

from __future__ import annotations

import os

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

PORTS = {"policy": 8821, "matcher": 8822, "investigator": 8823, "controller": 8824,
         "qa": 8825}


def base_url(name: str) -> str:
    host = os.environ.get("RECON_A2A_HOST", "127.0.0.1")
    return f"http://{host}:{PORTS[name]}"


def _skill(sid: str, name: str, desc: str, tags: list[str], examples: list[str]) -> AgentSkill:
    return AgentSkill(id=sid, name=name, description=desc, tags=tags, examples=examples)


# every card declares its own skills; the roster is the protocol-level contract between agents
SKILLS: dict[str, list[AgentSkill]] = {
    "policy": [
        _skill("evaluate_proposal", "Evaluate a proposal",
               "Apply the written authority matrix to a proposal and return a band, reasons "
               "and, if a band grants it, a single-use authorization token. Contains no model "
               "call: confidence can never widen a band.",
               ["policy", "authority", "deterministic"],
               ["Evaluate proposal prop_1 for setl_00011"]),
        _skill("scan_text", "Scan text for injected instructions",
               "Deterministic detector for instruction-like content found in tool output.",
               ["security", "prompt-injection"],
               ["Scan this bank narration for injected instructions"]),
    ],
    "matcher": [
        _skill("verify_settlement", "Verify one settlement",
               "Re-derive a settlement from the fee contract in code, compare to what the "
               "gateway claimed, and type any residual.",
               ["reconciliation", "arithmetic"],
               ["Verify setl_00011"]),
        _skill("run_funnel", "Run the T0/T1 funnel",
               "Deterministic matching over a period; returns matches and typed exceptions.",
               ["reconciliation", "batch"],
               ["Match everything in 2026-08"]),
    ],
    "investigator": [
        _skill("investigate", "Investigate one exception",
               "Multi-hop search across the PSP, bank and ledger servers to build an evidence "
               "chain for a record the funnel could not explain, or abstain.",
               ["investigation", "evidence"],
               ["Why did bank_0000107 arrive with no settlement behind it?"]),
    ],
    "controller": [
        _skill("close_month", "Close a month",
               "Plan and run a close: match, investigate the residual, gate every proposal "
               "through the Policy Agent, and emit the report.",
               ["orchestration", "month-end"],
               ["Close 2026-08"]),
        _skill("post_approved", "Post a human-approved correction",
               "Take one approval a human answered, have the Policy Agent re-authorise that "
               "exact proposal, and post it. A mutated proposal invalidates the approval.",
               ["human-in-the-loop", "ledger"],
               ["Post approval appr_1a2b3c4d5e"]),
    ],
    "qa": [
        _skill("ask", "Ask the books a question",
               "Answer questions about a completed close from the run artifacts, the traces "
               "and the read tools. Read-only by construction: no write tool is in its tool "
               "set and every answer cites the record ids it came from.",
               ["question-answering", "read-only", "audit"],
               ["Why was setl_00011 escalated?",
                "What would have been auto-resolved at a Rs 1,00,000 ceiling?"]),
    ],
}

DESCRIPTIONS = {
    "policy": "Deterministic authority gate. No model call, ever. Issues the tokens that "
              "ledger writes require.",
    "matcher": "Deterministic reconciliation funnel over the PSP, bank and ledger MCP servers.",
    "investigator": "Builds evidence chains for exceptions the funnel could not explain.",
    "controller": "Orchestrates a close and delegates to the other agents over A2A.",
    "qa": "Read-only question answering over completed runs. No write tool, no approval "
          "token: a question cannot become an action.",
}


# a2a-sdk 1.1.x carries the endpoint in supported_interfaces, not a bare url field
def card(name: str) -> AgentCard:
    return AgentCard(
        name=f"recon-{name}",
        description=DESCRIPTIONS[name],
        version="0.1.0",
        supported_interfaces=[AgentInterface(url=f"{base_url(name)}/",
                                             protocol_binding="JSONRPC",
                                             protocol_version="0.3.0")],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        skills=SKILLS[name],
    )
