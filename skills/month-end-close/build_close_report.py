"""Render a completed close run as the report a controller signs, in text or html."""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import trace
from core import money, rupees


def load_report(run_id: str | None = None) -> dict:
    run_id = run_id or trace.latest_run()
    if not run_id:
        return {"error": "no runs yet"}
    path = trace.run_dir(run_id) / "close_report.json"
    if not path.exists():
        return {"error": f"{run_id} has no close_report.json"}
    return json.loads(path.read_text(encoding="utf-8"))


# the three identities from the checklist, checked rather than asserted
def check_identities(r: dict) -> list[dict]:
    out = [
        {"identity": "records == matched + exceptions",
         "lhs": r["records_processed"],
         "rhs": r["matched_deterministically"] + r["exceptions"]},
        {"identity": "exceptions == auto_resolved + escalated",
         "lhs": r["exceptions"], "rhs": r["auto_resolved"] + r["escalated"]},
        {"identity": "auto_resolved == posted_to_ledger",
         "lhs": r["auto_resolved"], "rhs": r.get("posted_to_ledger", 0)},
    ]
    for row in out:
        row["holds"] = row["lhs"] == row["rhs"]
    return out


def to_text(r: dict) -> str:
    if "error" in r:
        return r["error"]
    auto_rate = r["matched_deterministically"] + r["auto_resolved"]
    L = [f"  RECON CLOSE  {r['month']}     run {r['run_id']}", "",
         f"  records processed         {r['records_processed']:>9,}",
         f"  matched deterministically {r['matched_deterministically']:>9,}",
         f"  exceptions                {r['exceptions']:>9,}",
         f"  auto-resolved             {r['auto_resolved']:>9,}",
         f"  posted to ledger          {r.get('posted_to_ledger', 0):>9,}",
         f"  escalated                 {r['escalated']:>9,}",
         f"  value under review        Rs {rupees(money(r['escalated_value_inr']))}",
         f"  handled without a human   {auto_rate / max(r['records_processed'], 1):>9.2%}", "",
         "  by band"]
    L += [f"    {b:20} {n:>6}" for b, n in r["by_band"].items() if n]
    if r.get("by_owner"):
        L += ["", "  routed to"]
        L += [f"    {o:20} {n:>6}" for o, n in r["by_owner"].items()]
    L += ["", "  identities"]
    for row in check_identities(r):
        L.append(f"    {'ok ' if row['holds'] else 'BROKEN'} {row['identity']:44} "
                 f"{row['lhs']} vs {row['rhs']}")
    L += ["", f"  trace  runs/{r['run_id']}/trace.jsonl"]
    return "\n".join(L)


def to_html(r: dict) -> str:
    if "error" in r:
        return f"<p>{html.escape(r['error'])}</p>"
    rows = "".join(
        f"<tr><td>{html.escape(e['record_id'])}</td><td>{html.escape(e['class'])}</td>"
        f"<td class=n>{html.escape(e['amount_inr'])}</td><td>{html.escape(e['band'])}</td>"
        f"<td>{html.escape(e.get('owner') or '-')}</td></tr>"
        for e in r.get("escalations", []))
    bands = "".join(f"<li>{html.escape(b)} <b>{n}</b></li>"
                    for b, n in r["by_band"].items() if n)
    return f"""<!doctype html><meta charset=utf-8>
<title>Recon close {html.escape(r['month'])}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;margin:2rem;max-width:60rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.4rem .6rem;text-align:left}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}code{{background:#f4f4f5;padding:.1rem .3rem}}</style>
<h1>Recon close &mdash; {html.escape(r['month'])}</h1>
<p>run <code>{html.escape(r['run_id'])}</code></p>
<ul><li>records <b>{r['records_processed']:,}</b></li>
<li>matched <b>{r['matched_deterministically']:,}</b></li>
<li>exceptions <b>{r['exceptions']}</b></li>
<li>auto-resolved <b>{r['auto_resolved']}</b>, posted <b>{r.get('posted_to_ledger', 0)}</b></li>
<li>escalated <b>{r['escalated']}</b>, value Rs {rupees(money(r['escalated_value_inr']))}</li></ul>
<h2>Bands</h2><ul>{bands}</ul>
<h2>Escalations</h2><table><tr><th>record<th>class<th class=n>exposure<th>band<th>owner</tr>
{rows}</table>"""


def run(run_id: str | None = None) -> dict:
    return load_report(run_id)


def main(argv: list[str]) -> int:
    run_id = next((a for a in argv if not a.startswith("--")), None)
    r = load_report(run_id)
    if "--html" in argv:
        out = to_html(r)
        if "error" not in r:
            path = trace.run_dir(r["run_id"]) / "close_report.html"
            path.write_text(out, encoding="utf-8")
            print(f"wrote {path}")
            return 0
        print(out)
        return 1
    print(to_text(r))
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
