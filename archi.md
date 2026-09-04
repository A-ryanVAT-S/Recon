# Architecture

How Recon is wired, and why each boundary is where it is.
For what it does and why, see [project.md](project.md). For numbers, [results.md](results.md).

Everything below follows from one line:

> **The LLM proposes. Deterministic code disposes.**

---

## The stack

```
┌─────────────────────────────────────────────────────────────────┐
│  Streamlit UI  :8501  ──HTTP──▶  FastAPI  :8850                  │
│  5 screens                       16 endpoints, 1 of which writes │
│  The UI imports no project module. Its only path in is the API.  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  AGENTS — 5 processes, A2A, cards at /.well-known/               │
│                                                                  │
│   controller   :8824  plans the close, delegates      no model    │
│   matcher      :8822  the T0/T1 funnel                no model    │
│   policy       :8821  ★ THE GATE ★               no model, ever   │
│   investigator :8823  evidence chains, on demand      LLM (20b)   │
│   qa           :8825  read-only questions             LLM (120b)  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  TOOLS — 5 MCP servers over streamable-http, 34 tools            │
│                                                                  │
│   psp       :8811  payments · settlements · fee contract         │
│   bank      :8812  the statement                                 │
│   ledger    :8813  orders · counterparties · the gated write     │
│   humanloop :8814  packs · approvals · precedents                │
│   runs      :8815  finished runs · traces · what_if_ceiling      │
│                                                                  │
│   Every list tool row-capped at 100. No tool takes a path.       │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  dataset/data/out/seed_42/demo/          DATA_ROOT               │
│  dataset/data/out/seed_42/ground_truth/  SIBLING — unreachable   │
└─────────────────────────────────────────────────────────────────┘
```

`python main.py up` starts all twelve in dependency order, waiting on each port.

---

## The four trust boundaries

Everything security-relevant is one of these four lines.

| # | Boundary | Enforced by |
|---|---|---|
| 1 | **Model output is never an action** | an LLM emits a `Proposal`, an inert dataclass. Only `policy/engine.py` — which has no model call — turns one into a signed token |
| 2 | **The arithmetic is re-derived, never trusted** | fees recomputed from the contract with `Decimal`, per payment. Stored fees are marked `# observed, not recomputed` |
| 3 | **Tool output is data, never instructions** | `scan_for_instructions()` runs before any model reads a record; a hit is a `HARD_STOP` in deterministic code |
| 4 | **Ground truth is unreachable** | the answer key is a *sibling* of `DATA_ROOT`; every server does a realpath check; no tool signature takes a path or glob; only `eval/` may name it, and no agent imports `eval/` |

---

## Why A2A, not function calls

1. **The Policy Agent must be independently addressable.** It is the only thing that can authorise a ledger write. As a separate process, the gate cannot be bypassed by editing a call site. In a real company compliance would own it and deploy it separately — the seam is a protocol boundary because in the real world it *is* one.
2. **The trace is a protocol artifact.** Every delegation is a task, so the audit trail is a by-product of the transport.
3. **The human is just another participant.** A2A's `input-required` state *is* the escalation state.
4. **Components are swappable** without touching the controller.

## Why MCP, not direct database access

- Every list tool is **row-capped at 100** — a model cannot pull a whole table.
- **No tool takes a path or glob** — this is what makes boundary 4 structural.
- The same tools serve code and models. `verify_settlement` recomputes a batch in Python, so a model never adds 67 payments up itself.

Read/write is split at the tool-list level: the Q&A agent is built `read_only=True`, write tools are stripped before the model sees the list, and it refuses to start if one is reachable.

---

## The close, as a sequence

```
  CONTROLLER ───────────────────────────────────── run_start
     │
     │ A2A ── run_funnel ──▶ MATCHER
     │                         T0  join on UTR, then amount + date ±3d
     │                         T1  re-derive from the contract
     │                             attribute the residual (fixed table)
     │                             bounded subset-sum, last
     │                         dup  order_id + time gap
     │                         ▼
     │                    matched · typed exceptions · 100% coverage
     │
     ├─ per exception ───────────────────────────────────────────┐
     │  Proposal(exposure, class, verified, evidence, source_texts)│
     │                                                             │
     │  A2A ── evaluate_proposal ──▶ POLICY   ◄── no model call    │
     │                                 ▼                           │
     │                     band + reasons + owner                  │
     │                     + token, only if allowed                │
     │                                                             │
     │  allowed ─▶ ledger: propose → commit(token)                 │
     │  denied  ─▶ humanloop: pack + owner + SLA                   │
     └─────────────────────────────────────────────────────────────┘
     │
     │ optional: A2A ── investigate ──▶ INVESTIGATOR (LLM)
     │                                     │ advisory Proposal
     │                                     ▼
     │                                  POLICY ENGINE (advisory: no token)
     │                                     │ band · allowed · reasons
     │                                     ▼
     │                                  shown to the reviewer, posts nothing
     ▼
  decisions.jsonl · close_report.json · trace.jsonl ──── run_end
```

Nothing there is a model call unless the optional investigator line runs. That line fires two
ways: batched here during a close (`--investigate N`, largest escalations first), or on demand —
`POST /approvals/{id}/investigate` calls the same skill for one record, wired to an **Investigate**
button in the Approval Inbox so a reviewer can ask before deciding rather than only in a batch.

**The advisory loop is the only place a model's proposal meets the gate.** The investigator ends
its reply with a structured advisory; code parses it, builds a `Proposal`, and evaluates it
through `evaluate()` — the same function, the same matrix, the same deny-first order a close
uses. Three things make it safe to point a model at that gate:

- `arithmetic_verified` is set to `False` by code, always. A model cannot claim code re-derived
  something, so no model proposal can satisfy the two automatic bands.
- `counterparty_in_master` is re-checked against the customer master, and `source_texts` are
  re-read from the dataset — never taken from the caller, so the injection scanner sees the
  record's real narration.
- `advisory: True` suppresses token minting and skips `record_action`, so the evaluation spends
  none of the run's circuit-breaker budget and produces nothing the ledger would accept.

---

## The gate

`agents/policy/engine.py`, evaluated **deny-first**, defaulting to a human.

```
  HARD_STOP          > ₹5,00,000 · locked period · counterparty absent
                     · empty evidence chain · instruction-like source text
        │ pass
  CIRCUIT BREAKER    >100 auto actions · >₹5,00,000 auto value
                     · verifier disagreement > 2% → autonomy halts
        │ pass
  HUMAN_REQUIRED     > ₹2,00,000 · never-auto class · unverified
        │ pass
  AUTO_RESOLVE       ≤ ₹25,000 · verified · evidence complete
                     · counterparty in master · class in the allowed 8
        │ no grant
  AUTO_WITH_NOTICE   ≤ ₹2,00,000 · same proof · told + reversible 24h
        │ no grant
  HUMAN_REQUIRED     ← the default. It fails closed.
```

- **Confidence never widens a band.** Necessary in some, sufficient in none.
- **The narrower granting band is checked first**, so the quieter one wins.
- **Exposure, not record value.** A paisa error on a ₹1,76,087 settlement is a ₹0.03 exposure.

The rulebook is `agents/policy/matrix.py` — a plain dict, money as strings so nothing becomes a float. **Editing that file is the only way to widen what runs alone.**

---

## Ledger writes are two steps

```
POLICY mints  token = HMAC( proposal_id
                          , sha256(record_id, action, amount)
                          , band, expiry +300s, nonce )

propose_ledger_entry(...)          records intent, posts nothing
commit_ledger_entry(id, token)     REBUILDS the proposal from what was proposed
                                   and re-verifies the token ITSELF
                                   signature · same action hash · not expired · nonce unspent
```

Change the amount by a paisa and the hash breaks. Replay it and the nonce is spent. **A token minted for a ₹0.03 correction cannot post a ₹4,00,000 one, and cannot post twice.**

A human's approval produces a **receipt**, not a token. It re-enters the same gate, which re-checks the structural hard stops before minting anything — so an approved ₹8,75,000 credit still will not post.

---

## Repository layout

```
main.py              one entry point: up · demo · status
core.py              money, record shapes, the fee engine, the loader
trace.py             the append-only decision trail

dataset/
  corpus.py          names, narrations, the 56 injection bait texts
  generate.py        the seeded generator and the anomaly injectors
  verify.py          ★ THE FUNNEL. T0, T1, duplicates, subset-sum, both gates
  main.py            generate · stats · verify

MCP/
  servers.py         psp · bank · ledger, and the two-step gated write
  humanloop.py       approval store, receipts, and its server
  runs.py            read-only over finished runs
  agent.py           the Groq tool-calling client  ← the only LLM plumbing
  main.py            serve · ask · inbox · approve · rules

agents/
  cards.py           what each agent advertises, and its port
  client.py          how one agent calls another
  executors.py       the five agents
  main.py            serve · close · ask · trace · cards
  policy/
    matrix.py        ★ THE RULEBOOK. The only way to widen authority
    engine.py        ★ THE GATE. Deny-first. Mints tokens. No model call
    earned.py        precedents → candidates → expiring, revocable rules

skills/              7 folders: SKILL.md + the script that runs the procedure
eval/                the scorecard. Imports no agent; no agent imports it
frontend/            api.py (:8850) · app.py (:8501, reads only via the API)
runs/<run_id>/       trace.jsonl · decisions.jsonl · close_report.json · scorecard.*
```

- **The repo root is the package root.** Imports are absolute (`from core import money`), relative inside a subpackage.
- **`MCP` is capitalised** so it cannot shadow the installed `mcp` SDK. Case-sensitive here — do not lowercase it.
- **`core.py` and `trace.py` live at the root** as the shared contract layer.
- No `__init__.py` (namespace packages). No `__main__.py` — each subpackage's CLI is `main.py`.

---

## Technology

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Money | `decimal.Decimal` + `ROUND_HALF_UP`, never `float` |
| Validation | Pydantic v2, `extra="forbid"` on every record type |
| Agent protocol | `a2a-sdk` 1.1.2 |
| Tool protocol | MCP 2.x over streamable-http |
| LLM | Groq — Q&A on `openai/gpt-oss-120b`, investigator on the smaller `openai/gpt-oss-20b` |
| API · UI | FastAPI + uvicorn · Streamlit |
| Config | **plain Python.** No YAML, no TOML. `.env` is the one exception |
| Tests | none — the two generator gates and the eval harness stand in |

The config choice is deliberate: the authority matrix is the most security-relevant file in the repo, and it should be read as code by whoever owns it.
