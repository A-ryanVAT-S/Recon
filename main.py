"""One entry point: bring the whole system up, or run one piece of it."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

from MCP.servers import PORTS as MCP_PORTS
from agents.cards import PORTS as AGENT_PORTS

API_PORT, UI_PORT = 8850, 8501

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def _run(name: str, argv: list[str], env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen(argv, cwd=ROOT, env={**os.environ, **(env or {})},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# a service is up when its port answers, which is the only signal worth waiting on
def _wait(port: int, timeout: float = 25.0) -> bool:
    import socket
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _data_ready() -> bool:
    from MCP.servers import data_root
    try:
        return (data_root() / "payments.csv").exists()
    except RuntimeError:
        return False


LAYERS = [
    ("tools", [PY, "-m", "MCP.main", "serve-all"], list(MCP_PORTS.values())),
    ("agents", [PY, "-m", "agents.main", "serve-all"], list(AGENT_PORTS.values())),
    ("api", [PY, "-m", "uvicorn", "frontend.api:api", "--host", "127.0.0.1",
             "--port", str(API_PORT), "--log-level", "warning"], [API_PORT]),
    # the blue-and-white theme is passed as flags, so there is no config file to maintain
    ("ui", [PY, "-m", "streamlit", "run", "frontend/app.py",
            "--server.port", str(UI_PORT), "--server.headless", "true",
            "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false",
            "--theme.base", "light", "--theme.primaryColor", "#1d4ed8",
            "--theme.backgroundColor", "#ffffff",
            "--theme.secondaryBackgroundColor", "#f8fafc",
            "--theme.textColor", "#0f172a"], [UI_PORT]),
]


def cmd_up(args) -> int:
    if not _data_ready():
        print("no dataset yet, generating...")
        subprocess.run([PY, "-m", "dataset.main", "generate", "--seed", args.seed,
                        "--scale", args.tier], cwd=ROOT, check=True)

    procs: list[tuple[str, subprocess.Popen]] = []
    wanted = [l for l in LAYERS if args.only is None or l[0] in args.only.split(",")]
    print()
    for name, argv, ports in wanted:
        procs.append((name, _run(name, argv)))
        ok = all(_wait(p) for p in ports)
        print(f"  {'up  ' if ok else 'FAIL'}  {name:8} {', '.join(str(p) for p in ports)}")
        if not ok:
            print(f"\n  {name} did not come up. Run it in the foreground to see why:")
            print(f"    {' '.join(argv)}\n")
            for _, p in procs:
                p.terminate()
            return 1

    print(f"""
  tool layer     psp {MCP_PORTS['psp']}  bank {MCP_PORTS['bank']}  """
          f"""ledger {MCP_PORTS['ledger']}  humanloop {MCP_PORTS['humanloop']}  """
          f"""runs {MCP_PORTS['runs']}
  agents         {'  '.join(f'{k} {v}' for k, v in AGENT_PORTS.items())}
  api            http://127.0.0.1:{API_PORT}/docs
  frontend       http://127.0.0.1:{UI_PORT}

  next:  python -m agents.main close --month 2026-08
         python -m eval.main score --write

  ctrl-c to stop everything
""")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n  stopping...")
    for name, p in reversed(procs):
        p.terminate()
    for _, p in procs:
        try:
            p.wait(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
    return 0


# the whole demo, start to finish, with nothing left running
def cmd_demo(args) -> int:
    steps = [
        ("generate the dataset", [PY, "-m", "dataset.main", "generate",
                                  "--seed", args.seed, "--scale", args.tier]),
        ("close the month", [PY, "-m", "agents.main", "close", "--month", args.month]),
        ("score it", [PY, "-m", "eval.main", "score", "--write"]),
    ]
    procs = []
    for name, argv, ports in LAYERS[:2]:
        procs.append(_run(name, argv))
        if not all(_wait(p) for p in ports):
            print(f"  {name} did not come up")
            return 1
    try:
        for label, argv in steps:
            print(f"\n=== {label} ===")
            if subprocess.run(argv, cwd=ROOT).returncode:
                return 1
        if args.ablations:
            print("\n=== ablations ===")
            subprocess.run([PY, "-m", "eval.main", "ablations"], cwd=ROOT)
    finally:
        for p in procs:
            p.terminate()
    print("\n  done. `python main.py up` to browse it in the frontend.\n")
    return 0


def cmd_status(args) -> int:
    print()
    for label, ports in (("tools", MCP_PORTS), ("agents", AGENT_PORTS)):
        for name, port in ports.items():
            print(f"  {'up  ' if _wait(port, 0.6) else 'down'}  {label}/{name:13} {port}")
    for name, port in (("api", API_PORT), ("frontend", UI_PORT)):
        print(f"  {'up  ' if _wait(port, 0.6) else 'down'}  {name:19} {port}")
    print(f"\n  dataset {'ready' if _data_ready() else 'not generated'}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python main.py",
        description="Recon - bounded-autonomy settlement reconciliation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("up", help="run the tool layer, the agents, the api and the frontend")
    sp.add_argument("--only", default=None, help="comma-separated: tools,agents,api,ui")
    sp.add_argument("--seed", default="42")
    sp.add_argument("--tier", default="demo", choices=("demo", "scale"))
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("demo", help="generate, close and score, then shut down")
    sp.add_argument("--month", default="2026-08")
    sp.add_argument("--seed", default="42")
    sp.add_argument("--tier", default="demo", choices=("demo", "scale"))
    sp.add_argument("--ablations", action="store_true")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("status", help="which ports are answering")
    sp.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
