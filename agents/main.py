"""CLI: serve the agents, run a close, ask a question, inspect a trace."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import trace
from core import rupees, money
from .cards import PORTS, card

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


# one fastapi app per agent: a2a routes plus the well-known card
def build_app(name: str):
    from fastapi import FastAPI
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, \
        create_jsonrpc_routes
    from a2a.server.tasks import InMemoryTaskStore

    from .executors import EXECUTORS

    ac = card(name)
    handler = DefaultRequestHandler(agent_executor=EXECUTORS[name](),
                                    task_store=InMemoryTaskStore(), agent_card=ac)
    app = FastAPI(title=ac.name)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card=ac),
        jsonrpc_routes=create_jsonrpc_routes(request_handler=handler, rpc_url="/",
                                             enable_v0_3_compat=True),
    )
    return app


def cmd_serve(args) -> int:
    import uvicorn
    port = args.port or PORTS[args.which]
    print(f"recon-{args.which}  http://{args.host}:{port}/  "
          f"card /.well-known/agent-card.json", file=sys.stderr)
    uvicorn.run(build_app(args.which), host=args.host, port=port, log_level="warning")
    return 0


# all four agents in one process, each on its own port
def cmd_serve_all(args) -> int:
    import threading
    import uvicorn

    def boot(name: str) -> None:
        uvicorn.run(build_app(name), host=args.host, port=PORTS[name], log_level="warning")

    for name in PORTS:
        threading.Thread(target=boot, args=(name,), daemon=True).start()
        print(f"recon-{name:13} http://{args.host}:{PORTS[name]}/", file=sys.stderr)
    print("ctrl-c to stop", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_cards(args) -> int:
    async def go():
        from .client import fetch_card
        for name in PORTS:
            c = await fetch_card(name)
            if "error" in c:
                print(f"  {name:13} {c['error']}")
            else:
                skills = [s["id"] for s in c.get("skills", [])]
                print(f"  {name:13} {c.get('name'):22} skills={skills}")
    asyncio.run(go())
    return 0


def cmd_close(args) -> int:
    async def go():
        from .client import call_agent
        out = await call_agent("controller", {"skill": "close_month", "month": args.month,
                                              "investigate_top": args.investigate})
        if "error" in out:
            print(json.dumps(out, indent=2))
            return 1
        print(f"\n  RECON CLOSE  {out['month']}     run {out['run_id']}\n")
        print(f"  records processed        {out['records_processed']:>8,}")
        print(f"  matched deterministically{out['matched_deterministically']:>8,}")
        print(f"  exceptions               {out['exceptions']:>8,}")
        print(f"  auto-resolved            {out['auto_resolved']:>8,}")
        print(f"  escalated                {out['escalated']:>8,}")
        print(f"  value under review       Rs {rupees(money(out['escalated_value_inr']))}\n")
        print("  by band")
        for b, n in out["by_band"].items():
            if n:
                print(f"    {b:20} {n:>6}")
        if out["by_owner"]:
            print("\n  routed to")
            for o, n in out["by_owner"].items():
                print(f"    {o:20} {n:>6}")
        print(f"\n  trace  runs/{out['run_id']}/trace.jsonl")
        return 0
    return asyncio.run(go())


def cmd_trace(args) -> int:
    run_id = args.run or trace.latest_run()
    if not run_id:
        print("no runs yet")
        return 1
    events = trace.for_record(run_id, args.record) if args.record else trace.read(run_id)
    for e in events[:args.limit]:
        rec = f" {e.record_id}" if e.record_id else ""
        ms = f" {e.elapsed_ms}ms" if e.elapsed_ms is not None else ""
        print(f"  {e.at[11:23]} {e.actor:13} {e.kind:13}{rec}{ms}")
        for k, v in e.payload.items():
            if v not in (None, [], {}, ""):
                print(f"      {k}: {str(v)[:110]}")
    print(f"\n  {len(events)} events in {run_id}")
    return 0


# the four questions the read-only agent is measured against; see results.md
DEMO_QUESTIONS = [
    "Why was setl_00011 escalated? Quote the band and the reason.",
    "Show me every fee overcharge in the latest run and what it cost us in total.",
    "Which owner has the most unresolved exceptions, and what is the largest one?",
    "What would have been auto-resolved at a Rs 1,00,000 ceiling instead of Rs 25,000?",
]


# read-only question answering over the finished run; every answer cites its record ids
def cmd_ask(args) -> int:
    from .client import call_agent

    questions = DEMO_QUESTIONS if args.demo else [args.question]
    for q in questions:
        out = asyncio.run(call_agent("qa", {"skill": "ask", "question": q}))
        if "error" in out:
            print(f"  {out['error']}", file=sys.stderr)
            return 1
        print(f"\n  Q  {q}\n")
        for line in (out.get("answer") or "").splitlines():
            print("     " + line)
        print(f"\n  [{out.get('tool_calls', 0)} tool calls over "
              f"{out.get('tools_available', 0)} read-only tools]")
        print(f"  cited: {', '.join(out.get('citations', [])[:10]) or 'NOTHING - uncited'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agents.main")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run one agent")
    sp.add_argument("which", choices=sorted(PORTS))
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("serve-all", help="run every agent, one port each")
    sp.add_argument("--host", default="127.0.0.1")
    sp.set_defaults(func=cmd_serve_all)

    sp = sub.add_parser("cards", help="fetch every published agent card")
    sp.set_defaults(func=cmd_cards)

    sp = sub.add_parser("close", help="run a month-end close")
    sp.add_argument("--month", default="2026-08")
    sp.add_argument("--investigate", type=int, default=0,
                    help="investigate the N largest escalations with the LLM")
    sp.set_defaults(func=cmd_close)

    sp = sub.add_parser("ask", help="ask the read-only Q&A agent about a finished run")
    sp.add_argument("question", nargs="?", default="")
    sp.add_argument("--demo", action="store_true", help="the four questions from the plan")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("trace", help="print a run's decision trail")
    sp.add_argument("--run", default=None)
    sp.add_argument("--record", default=None)
    sp.add_argument("--limit", type=int, default=60)
    sp.set_defaults(func=cmd_trace)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
