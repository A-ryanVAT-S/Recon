"""CLI and loader for the skill folders: list, show, run, check."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SKILLS_DIR = Path(__file__).resolve().parent
ORDER = ["settlement-decomposition", "fee-verification", "duplicate-detection",
         "exception-investigation", "evidence-pack", "escalation-routing",
         "month-end-close"]

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


# minimal yaml front matter: key: value, one per line, between --- fences
def parse(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    meta = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            v = v.strip()
            meta[k.strip()] = None if v in ("null", "") else v
    return meta, body.strip()


def catalog() -> list[dict]:
    out = []
    for name in ORDER:
        card = SKILLS_DIR / name / "SKILL.md"
        if not card.exists():
            continue
        meta, body = parse(card)
        out.append({"name": meta.get("name", name), "dir": name,
                    "description": meta.get("description", ""),
                    "script": meta.get("script"), "tier": meta.get("tier", ""),
                    "path": str(card.relative_to(SKILLS_DIR.parents[0])),
                    "words": len(body.split())})
    return out


# hyphenated folders are not importable, so load the script by path
def load_script(dir_name: str) -> ModuleType:
    meta, _ = parse(SKILLS_DIR / dir_name / "SKILL.md")
    script = meta.get("script")
    if not script:
        raise ValueError(f"{dir_name} is a judgment skill and carries no script")
    path = SKILLS_DIR / dir_name / script
    spec = importlib.util.spec_from_file_location(f"recon_skill_{dir_name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_list(args) -> int:
    print()
    for s in catalog():
        script = s["script"] or "-- judgment, no script"
        print(f"  {s['dir']:26} {s['tier']:14} {script}")
        print(f"  {'':26} {s['description'][:96]}")
    print(f"\n  {len(catalog())} skills in {SKILLS_DIR}")
    return 0


def cmd_show(args) -> int:
    card = SKILLS_DIR / args.which / "SKILL.md"
    if not card.exists():
        print(f"no skill {args.which}", file=sys.stderr)
        return 1
    print(card.read_text(encoding="utf-8"))
    return 0


def cmd_run(args) -> int:
    try:
        module = load_script(args.which)
    except (ValueError, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        return 1
    return module.main(args.rest)


# every skill has a card with front matter, and every declared script imports and runs
def cmd_check(args) -> int:
    bad = 0
    for s in catalog():
        if not s["description"] or not s["tier"]:
            print(f"  FAIL  {s['dir']:26} incomplete front matter")
            bad += 1
            continue
        if s["script"] is None:
            print(f"  ok    {s['dir']:26} judgment skill, {s['words']} words")
            continue
        try:
            module = load_script(s["dir"])
            assert callable(module.main) and callable(module.run)
            print(f"  ok    {s['dir']:26} {s['script']} loads, {s['words']} words")
        except Exception as e:
            print(f"  FAIL  {s['dir']:26} {type(e).__name__}: {e}")
            bad += 1
    print(f"\n{len(catalog()) - bad}/{len(catalog())} skills healthy")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m skills.main")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="every skill and its script")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="print a SKILL.md")
    sp.add_argument("which", choices=ORDER)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("run", help="run a skill's deterministic script")
    sp.add_argument("which", choices=ORDER)
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("check", help="every card parses and every script loads")
    sp.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
