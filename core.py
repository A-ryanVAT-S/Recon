"""Money, record shapes and the fee engine."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

PAISA = Decimal("0.01")


# coerce anything to a 2dp Decimal, half-up
def money(value) -> Decimal:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(PAISA, rounding=ROUND_HALF_UP)


# percent% of amount, rounded to paisa
def pct_of(amount: Decimal, percent: Decimal) -> Decimal:
    return money(amount * percent / Decimal("100"))


# canonical string form for files
def fmt(value: Decimal) -> str:
    return f"{money(value):.2f}"


# indian grouping for display only, e.g. 8,75,000.00
def rupees(value: Decimal) -> str:
    d = money(value)
    neg = d < 0
    whole, _, frac = f"{abs(d):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + whole + "." + frac


ZERO = money(0)


class Method(str, Enum):
    UPI = "upi"
    CARD_DEBIT = "card_debit"
    CARD_CREDIT = "card_credit"
    NETBANKING = "netbanking"
    WALLET = "wallet"


class AnomalyClass(str, Enum):
    EXACT_MATCH = "exact_match"
    ROUNDING_PAISA = "rounding_paisa"
    FEE_VARIANCE_WITHIN_CONTRACT = "fee_variance_within_contract"
    TIMING_SPLIT = "timing_split"
    PARTIAL_REFUND_OFFSET = "partial_refund_offset"
    UTR_TYPO = "utr_typo"
    COUNTERPARTY_NAME_DRIFT = "counterparty_name_drift"
    FEE_OVERCHARGE = "fee_overcharge"
    DUPLICATE_PAYMENT = "duplicate_payment"
    LEGIT_NEAR_DUPLICATE = "legit_near_duplicate"
    MISSING_BANK_CREDIT = "missing_bank_credit"
    UNMATCHED_BANK_CREDIT = "unmatched_bank_credit"
    INJECTION_BAIT = "injection_bait"


class Outcome(str, Enum):
    AUTO = "AUTO"
    ESCALATE = "ESCALATE"
    ABSTAIN_OK = "ABSTAIN_OK"


class Owner(str, Enum):
    PAYMENTS_OPS = "payments_ops"
    SETTLEMENT_OPS = "settlement_ops"
    FINANCE_CONTROLLER = "finance_controller"


_CFG = ConfigDict(extra="forbid", use_enum_values=True)


class _Rec(BaseModel):
    model_config = _CFG


class FeeSchedule(_Rec):
    name: str
    effective_from: date
    rates: dict[str, Decimal]
    gst_on_fee_pct: Decimal = Decimal("18.0")


class Customer(_Rec):
    customer_id: str
    legal_name: str
    display_name: str
    in_master: bool = True
    first_seen: date
    internal_note: str = ""


class Order(_Rec):
    order_id: str
    customer_id: str
    amount_inr: Decimal
    created_at: datetime
    status: str = "paid"
    invoice_id: str
    description: str = ""


class Payment(_Rec):
    payment_id: str
    order_id: str
    customer_id: str
    amount_inr: Decimal
    method: str
    status: str = "captured"
    captured_at: datetime
    fee_inr: Decimal          # observed, not recomputed
    gst_inr: Decimal          # observed, not recomputed
    net_inr: Decimal
    settlement_id: Optional[str] = None
    notes: str = ""           # injection surface


class Refund(_Rec):
    refund_id: str
    payment_id: str
    amount_inr: Decimal
    created_at: datetime
    settlement_id: Optional[str] = None
    speed: str = "normal"
    reason: str = ""          # injection surface


class Dispute(_Rec):
    dispute_id: str
    payment_id: str
    amount_inr: Decimal
    raised_at: datetime
    status: str = "lost"
    deducted_in_settlement_id: Optional[str] = None


class Settlement(_Rec):
    settlement_id: str
    created_at: datetime
    utr: str
    status: str = "processed"
    gross_inr: Decimal
    fee_inr: Decimal          # observed, not recomputed
    gst_inr: Decimal          # observed, not recomputed
    refunds_inr: Decimal
    disputes_inr: Decimal
    adjustments_inr: Decimal
    net_inr: Decimal          # must equal the bank credit
    payment_ids: list[str]
    refund_ids: list[str] = Field(default_factory=list)
    dispute_ids: list[str] = Field(default_factory=list)
    remark: str = ""          # injection surface


class BankLine(_Rec):
    line_id: str
    value_date: date
    narration: str            # injection surface
    utr: str                  # blank on ~15%
    credit_inr: Decimal
    debit_inr: Decimal
    balance_inr: Decimal


class Label(_Rec):
    record_id: str
    record_type: str
    anomaly_class: str
    expected_outcome: str
    expected_owner: Optional[str] = None
    amount_inr: Decimal
    linked_records: list[str] = Field(default_factory=list)
    rationale: str = ""
    is_adversarial: bool = False
    bait_strategy: Optional[str] = None


class Band(str, Enum):
    HARD_STOP = "HARD_STOP"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    HUMAN_APPROVED = "HUMAN_APPROVED"      # a human saw the pack and said yes
    AUTO_WITH_NOTICE = "AUTO_WITH_NOTICE"
    AUTO_RESOLVE = "AUTO_RESOLVE"


class Proposal(_Rec):
    proposal_id: str
    record_id: str
    record_type: str
    proposed_class: str
    proposed_action: str
    amount_inr: Decimal
    arithmetic_verified: bool = False      # code re-derived it, not the model's opinion
    delta_inr: Decimal = Decimal("0")
    evidence_chain: list[str] = Field(default_factory=list)
    counterparty_in_master: bool = True
    period: str = ""
    source_texts: list[str] = Field(default_factory=list)  # raw text seen in tool output
    generated_by: str = ""


class PolicyDecision(_Rec):
    proposal_id: str
    band: str
    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    owner: Optional[str] = None
    authorization_token: Optional[str] = None
    ceiling_inr: Optional[Decimal] = None
    rules_fired: list[str] = Field(default_factory=list)


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    auto_action_count: int = 0
    auto_actioned_value_inr: Decimal = Decimal("0")
    verifier_checks: int = 0
    verifier_disagreements: int = 0
    autonomy_halted: bool = False
    halt_reason: str = ""
    locked_periods: list[str] = Field(default_factory=list)
    spent_nonces: list[str] = Field(default_factory=list)

    @property
    def disagreement_rate(self) -> Decimal:
        if self.verifier_checks == 0:
            return Decimal("0")
        return Decimal(self.verifier_disagreements) / Decimal(self.verifier_checks)


class Manifest(_Rec):
    seed: int
    scale: str
    months: str
    generated_at: str
    corpus_sha256: str
    counts: dict[str, int]
    anomaly_counts: dict[str, int]
    gates: dict[str, bool]


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedules: list[FeeSchedule]
    customers: list[Customer]
    orders: list[Order]
    payments: list[Payment]
    refunds: list[Refund]
    disputes: list[Dispute]
    settlements: list[Settlement]
    bank_lines: list[BankLine]
    labels: list[Label] = Field(default_factory=list)
    # generator bookkeeping, never written to disk: the bank does not know this
    bank_link: dict[str, str] = Field(default_factory=dict)

    def payments_by_id(self) -> dict[str, Payment]:
        return {p.payment_id: p for p in self.payments}

    def refunds_by_id(self) -> dict[str, Refund]:
        return {r.refund_id: r for r in self.refunds}

    def disputes_by_id(self) -> dict[str, Dispute]:
        return {d.dispute_id: d for d in self.disputes}

    def settlements_by_id(self) -> dict[str, Settlement]:
        return {s.settlement_id: s for s in self.settlements}

    # what a close processes; customers and orders are masters, not records
    def record_count(self) -> int:
        return (len(self.payments) + len(self.refunds) + len(self.disputes)
                + len(self.settlements) + len(self.bank_lines))


SCHEDULES: list[FeeSchedule] = [
    FeeSchedule(
        name="SCHEDULE_A",
        effective_from=date(2025, 1, 1),
        rates={"upi": Decimal("0.00"), "card_debit": Decimal("0.90"),
               "card_credit": Decimal("2.00"), "netbanking": Decimal("1.60"),
               "wallet": Decimal("1.80")},
        gst_on_fee_pct=Decimal("18.0"),
    ),
    FeeSchedule(
        name="SCHEDULE_A2",
        effective_from=date(2026, 7, 16),
        rates={"upi": Decimal("0.00"), "card_debit": Decimal("0.85"),
               "card_credit": Decimal("1.90"), "netbanking": Decimal("1.50"),
               "wallet": Decimal("1.80")},
        gst_on_fee_pct=Decimal("18.0"),
    ),
]


# latest schedule whose effective_from is on or before the given date
def resolve_schedule(schedules: list[FeeSchedule], on: date) -> FeeSchedule:
    eligible = [s for s in schedules if s.effective_from <= on]
    if not eligible:
        raise ValueError(f"no fee schedule in force on {on}")
    return max(eligible, key=lambda s: s.effective_from)


# (fee, gst) for one payment, rounded per payment not per batch
def compute_fee(amount: Decimal, method: str, schedules: list[FeeSchedule],
                on: date) -> tuple[Decimal, Decimal]:
    sched = resolve_schedule(schedules, on)
    rate = sched.rates.get(method)
    if rate is None:
        raise ValueError(f"no rate for method {method!r} in {sched.name}")
    fee = pct_of(money(amount), rate)
    return fee, pct_of(fee, sched.gst_on_fee_pct)


def net_of(amount: Decimal, fee: Decimal, gst: Decimal) -> Decimal:
    return money(money(amount) - money(fee) - money(gst))


# blank strings in CSV mean None, not empty, for optional foreign keys
def _blank_to_none(row: dict, keys: tuple[str, ...]) -> dict:
    return {k: (None if k in keys and v == "" else v) for k, v in row.items()}


def _read_csv(path: Path, model, none_keys: tuple[str, ...] = ()) -> list:
    with path.open(encoding="utf-8", newline="") as fh:
        return [model(**_blank_to_none(row, none_keys)) for row in csv.DictReader(fh)]


def _read_jsonl(path: Path, model) -> list:
    with path.open(encoding="utf-8") as fh:
        return [model(**json.loads(line)) for line in fh if line.strip()]


# load a written dataset back from DATA_ROOT; labels and bank_link are never on disk here
def load_dataset(data_root: Path) -> Dataset:
    return Dataset(
        schedules=_read_jsonl(data_root / "fee_schedules.jsonl", FeeSchedule),
        customers=_read_csv(data_root / "customers.csv", Customer),
        orders=_read_csv(data_root / "orders.csv", Order),
        payments=_read_csv(data_root / "payments.csv", Payment, ("settlement_id",)),
        refunds=_read_csv(data_root / "refunds.csv", Refund, ("settlement_id",)),
        disputes=_read_csv(data_root / "disputes.csv", Dispute,
                           ("deducted_in_settlement_id",)),
        settlements=_read_jsonl(data_root / "settlements.jsonl", Settlement),
        bank_lines=_read_csv(data_root / "bank_statement.csv", BankLine),
    )
