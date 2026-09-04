"""CLI for the tool layer: serve the servers, ask the LLM, work the human queue."""

from __future__ import annotations

import argparse
import asyncio
import sys

from core import rupees

from .servers import PORTS, all_servers, data_root, url_for

# model output is utf-8; a windows cp1252 console would crash on the first typographic space
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


# streamable-http so many agents share one server process instead of spawning their own
def cmd_serve(args) -> int:
    servers = all_servers()
    if args.which not in servers:
        print(f"unknown server {args.which}", file=sys.stderr)
        return 1
    port = args.port or PORTS[args.which]
    print(f"{args.which}-mcp  http://{args.host}:{port}/mcp  DATA_ROOT={data_root()}",
          file=sys.stderr)
    servers[args.which]().run(transport="streamable-http", host=args.host, port=port,
                              json_response=True, stateless_http=args.stateless)
    return 0


# every server in one process, each on its own port
def cmd_serve_all(args) -> int:
    import threading

    servers = all_servers()

    def boot(name: str) -> None:
        servers[name]().run(transport="streamable-http", host=args.host,
                            port=PORTS[name], json_response=True, stateless_http=True)

    for name in servers:
        threading.Thread(target=boot, args=(name,), daemon=True).start()
        print(f"{name}-mcp  {url_for(name)}", file=sys.stderr)
    print(f"DATA_ROOT={data_root()}", file=sys.stderr)
    print("ctrl-c to stop", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


# hand the tools to the model and print the answer
def cmd_ask(args) -> int:
    from .agent import Agent

    async def go() -> int:
        async with Agent(model=args.model, verbose=not args.quiet) as agent:
            answer = await agent.ask(args.question, max_turns=args.max_turns)
            print("\n" + answer)
            if not args.quiet:
                print(f"\n[{agent.calls} tool calls]")
        return 0

    return asyncio.run(go())


# list every tool each server exposes, without an LLM
def cmd_tools(args) -> int:
    for name, build in all_servers().items():
        tools = asyncio.run(build().list_tools())
        print(f"\n{name}-mcp  port {PORTS[name]}  ({len(tools)} tools)")
        for t in tools:
            print(f"  {t.name:24} {(t.description or '')[:80]}")
    print(f"\nDATA_ROOT {data_root()}")
    return 0


# --- the human queue: the same code path the frontend and an email link use --------------


def cmd_inbox(args) -> int:
    from . import humanloop as hl

    rows = hl.pending(args.owner)
    if not rows:
        print("\n  inbox empty\n")
        return 0
    print(f"\n  {len(rows)} awaiting a human\n")
    for a in rows:
        print(f"  {a.urgency}  {a.approval_id}  {a.record_id:20} {a.detected_class:24} "
              f"Rs {rupees(a.amount_inr):>14}  -> {a.owner}  ({a.sla_hours}h)")
    print()
    return 0


def cmd_show(args) -> int:
    from . import humanloop as hl

    a = hl.get(args.approval_id)
    if a is None:
        print(f"no approval {args.approval_id}", file=sys.stderr)
        return 1
    print(f"\n  {a.approval_id}   {a.status.upper()}   owner {a.owner}   {a.urgency}\n")
    print(hl.pack_text(a))
    if a.decided_by:
        print(f"\n  decided {a.status} by {a.decided_by} at {a.decided_at}  {a.note}")
    print()
    return 0


# approve and reject are one call; only the verdict differs
def cmd_decide(args) -> int:
    from . import humanloop as hl

    try:
        a = hl.decide(args.approval_id, args.verdict, args.who, args.note)
    except (ValueError, PermissionError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"  {a.approval_id}  {a.status}  by {a.decided_by}")
    print(f"  receipt {a.receipt}")
    print("  a precedent was signed; the ledger token still comes from the Policy Agent")
    return 0


def cmd_rules(args) -> int:
    from agents.policy import earned

    rows, cands = earned.rules(), earned.candidates()
    print(f"\n  {len(rows)} rules, {sum(1 for r in rows if r.live())} live\n")
    for r in rows:
        print(f"  {r.rule_id}  v{r.version}  {r.status:9} "
              f"{'LIVE' if r.live() else '    '}  {r.signature:44} "
              f"ceiling Rs {rupees(r.ceiling_inr)}")
    print(f"\n  {len(earned.precedents())} signed precedents, "
          f"{sum(1 for c in cands if c['eligible'])} of {len(cands)} signatures eligible\n")
    for c in cands:
        print(f"  {'ELIGIBLE' if c['eligible'] else '        '}  {c['signature']:52} "
              f"{c['approvals']}/{c['total']}")
        for b in c["blockers"]:
            print(f"            - {b}")
    print()
    return 0


# a rule is proposed from precedent and granted by a named human, never by an agent
def cmd_rule(args) -> int:
    from agents.policy import earned

    try:
        if args.action == "propose":
            r = earned.propose_rule(args.target)
            print(f"  proposed {r.rule_id}, ceiling Rs {rupees(r.ceiling_inr)}")
            print("  it grants nothing until a human activates it")
        elif args.action == "activate":
            r = earned.activate_rule(args.target, args.who,
                                     earned.attest(args.target, "ACTIVATE", args.who))
            print(f"  {r.rule_id} v{r.version} active until {r.valid_until}, "
                  f"ceiling Rs {rupees(r.ceiling_inr)}, by {r.activated_by}")
        else:
            r = earned.revoke_rule(args.target, args.note or "revoked by hand")
            print(f"  {r.rule_id} v{r.version} revoked: {r.revoked_reason}")
    except (ValueError, PermissionError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m MCP.main")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run one server over http")
    sp.add_argument("which", choices=sorted(PORTS))
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--stateless", action="store_true", default=True)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("serve-all", help="run every server, one port each")
    sp.add_argument("--host", default="127.0.0.1")
    sp.set_defaults(func=cmd_serve_all)

    sp = sub.add_parser("ask", help="ask the Groq agent a question")
    sp.add_argument("question")
    sp.add_argument("--model", default=None)
    sp.add_argument("--max-turns", type=int, default=12)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("tools", help="list every tool, no LLM")
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("inbox", help="approvals awaiting a human")
    sp.add_argument("--owner", default=None)
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("show", help="one approval and its evidence pack")
    sp.add_argument("approval_id")
    sp.set_defaults(func=cmd_show)

    for verdict in ("approve", "reject"):
        sp = sub.add_parser(verdict, help=f"{verdict} one approval as a named human")
        sp.add_argument("approval_id")
        sp.add_argument("--who", required=True)
        sp.add_argument("--note", default="")
        sp.set_defaults(func=cmd_decide, verdict=verdict.upper())

    sp = sub.add_parser("rules", help="precedents and earned authority rules")
    sp.set_defaults(func=cmd_rules)

    sp = sub.add_parser("rule", help="propose, activate or revoke an earned rule")
    sp.add_argument("action", choices=("propose", "activate", "revoke"))
    sp.add_argument("target", help="a signature to propose, or a rule_id")
    sp.add_argument("--who", default="")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_rule)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
