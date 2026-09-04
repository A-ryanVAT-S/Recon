"""CLI: generate, verify and inspect the dataset."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import generate, verify
from core import Manifest, rupees

ROOT = Path(__file__).resolve().parent / "data" / "out"
GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def _tick(ok: bool) -> str:
    return f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"


# build the spine, gate it, inject anomalies, gate again, write files
def cmd_generate(args) -> int:
    t0 = datetime.now()
    print(f"{BOLD}Recon dataset{OFF}  seed={args.seed} scale={args.scale} months={args.months}\n")

    print(f"{DIM}stages 1-7  clean spine{OFF}")
    ds, rngs = generate.build_clean(args.seed, args.scale, args.months)
    for name in ("customers", "orders", "payments", "refunds", "disputes",
                 "settlements", "bank_lines"):
        print(f"            {len(getattr(ds, name)):>7,} {name}")
    print(f"            {ds.record_count():>7,} records\n")

    print(f"{BOLD}stage 8     GATE 1 clean data must reconcile 100%{OFF}")
    clean_ok, res = verify.gate_clean(ds)
    print(f"            matched {res.matched:,}/{res.total_records:,}"
          f"   exceptions {len(res.exceptions)}   {_tick(clean_ok)}")
    if not clean_ok:
        print(f"\n{RED}generator bug, first 10:{OFF}")
        for e in res.exceptions[:10]:
            print(f"  {e.record_id:22} {e.detected_class:24} {e.delta_inr} {e.detail}")
        return 1

    print(f"\n{DIM}stage 9     injecting anomalies{OFF}")
    applied = generate.inject(ds, rngs["anomalies"], args.scale)
    plan = generate.DEMO_PLAN if args.scale == "demo" else generate.SCALE_PLAN
    for cls in sorted(plan):
        got, want = applied.get(cls, 0), plan[cls]
        print(f"          {' ' if got == want else '!'} {cls:32} {got:>5} / {want}")
    total = sum(applied.values())
    print(f"            {total} anomalies over {ds.record_count():,} records"
          f"  ({100 * total / ds.record_count():.2f}%)\n")

    print(f"{BOLD}stage 10    GATE 2 found must equal planted{OFF}")
    det_ok, report = verify.gate_detection(ds)
    for cls, (p, f) in report.items():
        print(f"            {cls:32} planted {p:>4}  found {f:>4}  {_tick(p == f)}")
    print(f"            {_tick(det_ok)}\n")

    print(f"{DIM}stage 11    writing{OFF}")
    manifest = Manifest(
        seed=args.seed, scale=args.scale, months=args.months,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        corpus_sha256=generate.corpus_sha256(),
        counts={n: len(getattr(ds, n)) for n in
                ("customers", "orders", "payments", "refunds", "disputes",
                 "settlements", "bank_lines")} | {"records": ds.record_count()},
        anomaly_counts=applied,
        gates={"gate1_clean": clean_ok, "gate2_detection": det_ok})
    for name, path in generate.write_dataset(ds, manifest, ROOT / f"seed_{args.seed}",
                                             args.scale).items():
        print(f"            {name:30} {path.stat().st_size:>9,} bytes")

    print(f"\n{BOLD}done in {(datetime.now() - t0).total_seconds():.1f}s{OFF}"
          f"   gate1={_tick(clean_ok)}  gate2={_tick(det_ok)}")
    return 0 if (clean_ok and det_ok) else 1


# re-run both gates without writing anything
def cmd_verify(args) -> int:
    ds, rngs = generate.build_clean(args.seed, args.scale, args.months)
    clean_ok, res = verify.gate_clean(ds)
    print(f"GATE 1  matched {res.matched:,}/{res.total_records:,}  "
          f"exceptions {len(res.exceptions)}  {_tick(clean_ok)}")
    generate.inject(ds, rngs["anomalies"], args.scale)
    det_ok, report = verify.gate_detection(ds)
    for cls, (p, f) in report.items():
        print(f"GATE 2  {cls:32} planted {p:>4}  found {f:>4}  {_tick(p == f)}")
    return 0 if (clean_ok and det_ok) else 1


# what is in the dataset, by ground truth and by what the funnel detected
def cmd_stats(args) -> int:
    ds, rngs = generate.build_clean(args.seed, args.scale, args.months)
    generate.inject(ds, rngs["anomalies"], args.scale)
    res = verify.run_funnel(ds)

    print(f"{BOLD}records{OFF}          {ds.record_count():,}")
    print(f"{BOLD}gross value{OFF}      Rs {rupees(sum(p.amount_inr for p in ds.payments))}")
    print(f"{BOLD}T0+T1 matched{OFF}    {res.matched:,} "
          f"({100 * res.matched / res.total_records:.1f}%)")
    print(f"{BOLD}exceptions{OFF}       {len(res.exceptions)}\n")

    counts: dict[str, int] = {}
    for lab in ds.labels:
        counts[lab.anomaly_class] = counts.get(lab.anomaly_class, 0) + 1
    print(f"{BOLD}ground truth by class{OFF}")
    for cls, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cls:34} {n:>6,}")

    print(f"\n{BOLD}funnel detected{OFF}")
    for cls, n in sorted(res.by_class().items(), key=lambda kv: -kv[1]):
        print(f"  {cls:34} {n:>6,}")

    baits = [l for l in ds.labels if l.bait_strategy]
    strat: dict[str, int] = {}
    for b in baits:
        strat[b.bait_strategy] = strat.get(b.bait_strategy, 0) + 1
    print(f"\n{BOLD}injection bait{OFF}   {len(baits)} planted, all on records needing a human")
    for s, n in sorted(strat.items()):
        print(f"  {s:34} {n:>6}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dataset.main")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("generate", cmd_generate), ("verify", cmd_verify), ("stats", cmd_stats)):
        sp = sub.add_parser(name)
        sp.add_argument("--seed", type=int, default=42)
        sp.add_argument("--scale", default="demo", choices=["demo", "scale", "stress"])
        sp.add_argument("--months", default="2026-06..2026-08")
        sp.set_defaults(func=fn)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
