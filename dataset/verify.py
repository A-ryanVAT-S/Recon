"""T0/T1 funnel and the two generator gates. No LLM in this file, ever."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from core import (SCHEDULES, ZERO, AnomalyClass, Dataset, Settlement,
                    compute_fee, money)

DUPLICATE_WINDOW_S = 300      # same order inside five minutes is a retry, not a new sale
REPEAT_PURCHASE_GAP_S = 7200  # different order, hours later: a second sale, not a double charge


@dataclass
class Exc:
    record_id: str
    record_type: str
    detected_class: str
    delta_inr: Decimal
    amount_inr: Decimal
    detail: str


@dataclass
class FunnelResult:
    t0_matched: list[str] = field(default_factory=list)
    t1_matched: list[str] = field(default_factory=list)
    exceptions: list[Exc] = field(default_factory=list)
    total_records: int = 0

    # a record is matched or an exception, never both: a settlement whose money arrived but
    # whose arithmetic did not tie is an exception, however well T0 did
    def matched_ids(self) -> set[str]:
        return ((set(self.t0_matched) | set(self.t1_matched))
                - {e.record_id for e in self.exceptions})

    @property
    def matched(self) -> int:
        return len(self.matched_ids())

    @property
    def coverage(self) -> float:
        seen = self.matched_ids() | {e.record_id for e in self.exceptions}
        return len(seen) / self.total_records if self.total_records else 0.0

    def by_class(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.exceptions:
            out[e.detected_class] = out.get(e.detected_class, 0) + 1
        return out


# re-derive gross/fee/gst/net from the contract, never from the stored fee fields
def recompute_settlement(ds: Dataset, s: Settlement, pays=None, refs=None,
                         disp=None) -> tuple[Decimal, ...]:
    pays = ds.payments_by_id() if pays is None else pays
    refs = ds.refunds_by_id() if refs is None else refs
    disp = ds.disputes_by_id() if disp is None else disp
    gross = fee = gst = ZERO
    for pid in s.payment_ids:
        p = pays.get(pid)
        if p is None:
            continue
        f, g = compute_fee(p.amount_inr, p.method, SCHEDULES, p.captured_at.date())
        gross, fee, gst = money(gross + p.amount_inr), money(fee + f), money(gst + g)
    ref = money(sum((refs[r].amount_inr for r in s.refund_ids if r in refs), ZERO))
    dis = money(sum((disp[d].amount_inr for d in s.dispute_ids if d in disp), ZERO))
    return gross, fee, gst, money(gross - fee - gst - ref - dis)


# name the residual from a fixed decision table
def attribute_delta(ds: Dataset, s: Settlement, delta: Decimal, expected_fee: Decimal,
                    pays=None, refund_amts=None, dispute_amts=None) -> tuple[str, str]:
    a = abs(delta)
    pays = ds.payments_by_id() if pays is None else pays
    refund_amts = ({r.amount_inr: r.refund_id for r in ds.refunds}
                   if refund_amts is None else refund_amts)
    dispute_amts = ({d.amount_inr: d.dispute_id for d in ds.disputes}
                    if dispute_amts is None else dispute_amts)
    n_fee_bearing = sum(1 for pid in s.payment_ids
                        if (p := pays.get(pid)) and p.method != "upi")

    if a <= money("0.50") and n_fee_bearing > 20:
        return AnomalyClass.ROUNDING_PAISA.value, f"paisa delta {delta} over {n_fee_bearing} fee-bearing"
    if expected_fee > ZERO:
        ratio = s.fee_inr / expected_fee
        if Decimal("1.05") <= ratio <= Decimal("1.30"):
            return AnomalyClass.FEE_OVERCHARGE.value, f"observed fee is {ratio:.3f}x contract"
    if a in refund_amts:
        return AnomalyClass.PARTIAL_REFUND_OFFSET.value, f"delta equals refund {refund_amts[a]}"
    if a in dispute_amts:
        return "chargeback_deduction", f"delta equals dispute {dispute_amts[a]}"
    return "unexplained", f"unexplained delta {delta}"


# same order + minutes apart is a retry; different order + hours apart is a real repeat buy
def find_duplicates(ds: Dataset) -> list[Exc]:
    groups: dict[tuple[str, Decimal], list] = {}
    for p in ds.payments:
        groups.setdefault((p.customer_id, p.amount_inr), []).append(p)

    out: list[Exc] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda p: (p.captured_at, p.payment_id))
        for a, b in zip(group, group[1:]):
            gap = (b.captured_at - a.captured_at).total_seconds()
            if a.order_id == b.order_id and gap < DUPLICATE_WINDOW_S:
                out.append(Exc(
                    b.payment_id, "payment", AnomalyClass.DUPLICATE_PAYMENT.value,
                    b.amount_inr, b.amount_inr,
                    f"same order {a.order_id} as {a.payment_id}, {int(gap)}s apart"))
            elif a.order_id != b.order_id and gap >= REPEAT_PURCHASE_GAP_S:
                # named positively, not merely left alone: the second order IS the evidence
                out.append(Exc(
                    b.payment_id, "payment", AnomalyClass.LEGIT_NEAR_DUPLICATE.value,
                    ZERO, b.amount_inr,
                    f"own order {b.order_id}, {int(gap) // 3600}h after {a.payment_id}"))
    return out


# bounded subset-sum: which unsettled payments in the window add up to this credit
def search_payment_set(ds: Dataset, target: Decimal, on: date,
                       window_days: int = 3, max_items: int = 8) -> list[str] | None:
    pool = [p for p in ds.payments
            if p.settlement_id is None
            and abs((p.captured_at.date() - on).days) <= window_days]
    if not pool or target <= ZERO:
        return None
    pool.sort(key=lambda p: p.net_inr, reverse=True)
    pool = pool[:40]                      # keep the search space small on purpose

    best: list[str] | None = None

    def walk(i: int, remaining: Decimal, picked: list) -> bool:
        nonlocal best
        if remaining == ZERO and picked:
            best = [p.payment_id for p in picked]
            return True
        if i >= len(pool) or len(picked) >= max_items or remaining < ZERO:
            return False
        if walk(i + 1, money(remaining - pool[i].net_inr), picked + [pool[i]]):
            return True
        return walk(i + 1, remaining, picked)

    walk(0, money(target), [])
    return best


# refunds and disputes are records too; not examining them makes coverage a lie
def check_deductions(ds: Dataset, res: FunnelResult) -> None:
    pays, setl = ds.payments_by_id(), ds.settlements_by_id()

    for r in ds.refunds:
        p = pays.get(r.payment_id)
        if p is None:
            res.exceptions.append(Exc(r.refund_id, "refund", "orphan_refund", r.amount_inr,
                                      r.amount_inr, f"payment {r.payment_id} does not exist"))
        elif r.amount_inr > p.amount_inr:
            res.exceptions.append(Exc(r.refund_id, "refund", "refund_over_payment",
                                      money(r.amount_inr - p.amount_inr), r.amount_inr,
                                      f"refund exceeds payment {p.payment_id}"))
        elif r.settlement_id is not None and r.settlement_id not in setl:
            res.exceptions.append(Exc(r.refund_id, "refund", "orphan_refund", ZERO,
                                      r.amount_inr, f"settlement {r.settlement_id} missing"))
        else:
            res.t0_matched.append(r.refund_id)

    for d in ds.disputes:
        if d.payment_id not in pays:
            res.exceptions.append(Exc(d.dispute_id, "dispute", "orphan_dispute", d.amount_inr,
                                      d.amount_inr, f"payment {d.payment_id} does not exist"))
        elif (d.deducted_in_settlement_id is not None
              and d.deducted_in_settlement_id not in setl):
            res.exceptions.append(Exc(d.dispute_id, "dispute", "orphan_dispute", ZERO,
                                      d.amount_inr, "deducting settlement missing"))
        else:
            res.t0_matched.append(d.dispute_id)


# T0 join then T1 verify/attribute over every record.
# ablations are for the eval harness only: they remove a control on purpose. See results.md 8.
def run_funnel(ds: Dataset, ablations=frozenset()) -> FunnelResult:
    res = FunnelResult(total_records=ds.record_count())
    settlements = ds.settlements_by_id()
    pays, refs, disp = ds.payments_by_id(), ds.refunds_by_id(), ds.disputes_by_id()
    refund_amts = {r.amount_inr: r.refund_id for r in ds.refunds}
    dispute_amts = {d.amount_inr: d.dispute_id for d in ds.disputes}

    lines_by_utr: dict[str, list] = {}
    lines_by_amt: dict[Decimal, list] = {}
    setl_by_net: dict[Decimal, list] = {}
    for b in ds.bank_lines:
        if b.utr:
            lines_by_utr.setdefault(b.utr, []).append(b)
        lines_by_amt.setdefault(b.credit_inr, []).append(b)
    for s in ds.settlements:
        setl_by_net.setdefault(s.net_inr, []).append(s)

    consumed: set[str] = set()
    typos: list[Exc] = []

    for s in ds.settlements:
        hit = None
        if s.utr in lines_by_utr:
            hit = lines_by_utr[s.utr][0]
            res.t0_matched.append(s.settlement_id)
        else:
            for cand in lines_by_amt.get(s.net_inr, []):
                if cand.line_id in consumed:
                    continue
                if abs((cand.value_date - s.created_at.date()).days) <= 3:
                    hit = cand
                    res.t0_matched.append(s.settlement_id)
                    # amount and date found it, so a UTR that is present and different is a typo
                    if cand.utr and cand.utr != s.utr:
                        typos.append(Exc(
                            cand.line_id, "bank_line", AnomalyClass.UTR_TYPO.value, ZERO,
                            cand.credit_inr,
                            f"utr {cand.utr} vs {s.utr} on {s.settlement_id}; "
                            f"amount and value date agree"))
                    break
        if hit is None:
            res.exceptions.append(Exc(
                s.settlement_id, "settlement", AnomalyClass.MISSING_BANK_CREDIT.value,
                s.net_inr, s.net_inr, "settlement processed but no bank credit found"))
            continue

        consumed.add(hit.line_id)
        res.t0_matched.append(hit.line_id)
        if "no_verifier" in ablations:
            # the join found the money, so the batch is declared matched without re-deriving it
            res.t1_matched.append(s.settlement_id)
            continue
        _, fee, _, net = recompute_settlement(ds, s, pays, refs, disp)
        delta, bank_delta = money(net - s.net_inr), money(hit.credit_inr - s.net_inr)
        if delta == ZERO and bank_delta == ZERO:
            res.t1_matched.append(s.settlement_id)
        else:
            d = delta if delta != ZERO else bank_delta
            cls, detail = (("unexplained", f"unexplained delta {d}") if "no_t1" in ablations
                           else attribute_delta(ds, s, d, fee, pays, refund_amts, dispute_amts))
            res.exceptions.append(Exc(s.settlement_id, "settlement", cls, d, s.net_inr, detail))

    for b in ds.bank_lines:
        if b.line_id in consumed or b.credit_inr == ZERO:
            continue
        near = [s for s in setl_by_net.get(b.credit_inr, [])
                if abs((b.value_date - s.created_at.date()).days) <= 3]
        if near:
            res.exceptions.append(Exc(
                b.line_id, "bank_line", AnomalyClass.UTR_TYPO.value, ZERO, b.credit_inr,
                f"amount and date match {near[0].settlement_id} but UTR does not"))
        else:
            found = (None if "no_t1" in ablations
                     else search_payment_set(ds, b.credit_inr, b.value_date))
            if found:
                res.t1_matched.append(b.line_id)
                continue
            res.exceptions.append(Exc(
                b.line_id, "bank_line", AnomalyClass.UNMATCHED_BANK_CREDIT.value,
                b.credit_inr, b.credit_inr,
                "credit with no settlement behind it; no payment subset explains it"))

    for p in ds.payments:
        if money(p.amount_inr - p.fee_inr - p.gst_inr) != p.net_inr:
            res.exceptions.append(Exc(
                p.payment_id, "payment", "payment_net_mismatch",
                money(p.amount_inr - p.fee_inr - p.gst_inr - p.net_inr),
                p.amount_inr, "net does not equal amount - fee - gst"))
        elif p.settlement_id not in settlements:
            res.exceptions.append(Exc(p.payment_id, "payment", "unsettled_payment",
                                      ZERO, p.amount_inr, "payment in no settlement"))
        else:
            res.t0_matched.append(p.payment_id)

    named = find_duplicates(ds) + typos
    flagged = {e.record_id for e in named}
    res.t0_matched = [r for r in res.t0_matched if r not in flagged]
    res.exceptions.extend(named)

    check_deductions(ds, res)
    return res


# gate 1: a failure here is a generator bug, not a matcher bug
def gate_clean(ds: Dataset) -> tuple[bool, FunnelResult]:
    res = run_funnel(ds)
    return len(res.exceptions) == 0, res


# every class the funnel can name from arithmetic and joins alone
DETERMINISTIC = {
    AnomalyClass.ROUNDING_PAISA.value,
    AnomalyClass.FEE_OVERCHARGE.value,
    AnomalyClass.MISSING_BANK_CREDIT.value,
    AnomalyClass.UNMATCHED_BANK_CREDIT.value,
    AnomalyClass.DUPLICATE_PAYMENT.value,
    AnomalyClass.LEGIT_NEAR_DUPLICATE.value,
    AnomalyClass.UTR_TYPO.value,
}


# gate 2: for arithmetic-findable classes, found must equal planted
def gate_detection(ds: Dataset) -> tuple[bool, dict[str, tuple[int, int]]]:
    found = run_funnel(ds).by_class()
    planted: dict[str, int] = {}
    for lab in ds.labels:
        if lab.anomaly_class in DETERMINISTIC:
            planted[lab.anomaly_class] = planted.get(lab.anomaly_class, 0) + 1
        # a name-drift line is an unmatched credit until a name is resolved, which is agent work
        if lab.anomaly_class == AnomalyClass.COUNTERPARTY_NAME_DRIFT.value:
            k = AnomalyClass.UNMATCHED_BANK_CREDIT.value
            planted[k] = planted.get(k, 0) + 1

    report, ok = {}, True
    for cls in sorted(DETERMINISTIC):
        p, f = planted.get(cls, 0), found.get(cls, 0)
        report[cls] = (p, f)
        if p != f:
            ok = False
    return ok, report
