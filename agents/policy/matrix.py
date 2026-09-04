"""The written authority matrix. Editing this file is the only way to widen what runs alone."""

from __future__ import annotations

import copy
from pathlib import Path

# Money is written as a string everywhere, so nothing here can become a float by accident.
# engine.py reads this dict and nothing else; it is enforced in code and printed in the report.
MATRIX = {

    "bands": {

        # the quiet band: write, log, tell nobody. Only classes code can prove.
        "AUTO_RESOLVE": {
            "max_amount_inr": "25000",
            "requires": {
                "arithmetic_verified": True,
                "evidence_chain_complete": True,
                "counterparty_in_master": True,
                "min_confidence": "0.85",
            },
            "classes": [
                "exact_match",
                "rounding_paisa",
                "fee_variance_within_contract",
                "timing_split",
                "partial_refund_offset",
                "utr_typo",
                "counterparty_name_drift",
                "legit_near_duplicate",
            ],
            "effect": "write to ledger, log, no notification",
        },

        # same proof, more money, so somebody is told and it stays reversible
        "AUTO_WITH_NOTICE": {
            "max_amount_inr": "200000",
            "requires": {
                "arithmetic_verified": True,
                "evidence_chain_complete": True,
                "counterparty_in_master": True,
                "min_confidence": "0.85",
            },
            "classes": [],          # empty means any class a denying band above did not catch
            "effect": "write to ledger, include in the daily digest, reversible for 24h",
        },

        # any one of these triggers is enough; this is also the fallthrough default
        "HUMAN_REQUIRED": {
            "trigger_if_amount_over_inr": "200000",
            "trigger_if_class": [
                "duplicate_payment",
                "unknown_remitter",
                "chargeback_deduction",
                "fee_overcharge",
                "missing_bank_credit",
                "unmatched_bank_credit",
                "unexplained",
            ],
            "trigger_if_unverified": True,
            "trigger_if_confidence_below": "0.85",
            "effect": "propose only, route to a human, book nothing",
        },

        # structural. Not a threshold a confident model can argue with, and a human's
        # approval clears everything here except the injection scan - see MCP/humanloop.py
        "HARD_STOP": {
            "max_ledger_write_inr": "500000",
            "on_locked_period": True,
            "on_counterparty_absent": True,
            "on_empty_evidence_chain": True,
            "on_instruction_like_source_text": True,
            "effect": "never automatable, at any confidence",
        },
    },

    # blast radius. A systematic bug must not be able to compound across a whole run.
    "circuit_breakers": {
        "max_auto_actions_per_run": 100,
        "max_auto_actioned_value_inr": "500000",
        "halt_autonomy_if_disagreement_rate_over": "0.02",
    },

    # first matching rule wins, and the last rule is a default, so nothing goes unowned
    "routing": [
        {"if_class": ["duplicate_payment"],
         "owner": "payments_ops", "dual": "finance_controller"},
        {"if_class": ["fee_overcharge", "missing_bank_credit"],
         "owner": "finance_controller"},
        {"if_amount_over_inr": "500000", "owner": "finance_controller"},
        {"if_class": ["unmatched_bank_credit", "unknown_remitter", "utr_typo"],
         "owner": "settlement_ops",
         "escalate_over_inr": "100000", "escalate_to": "finance_controller"},
        {"default": "payments_ops"},
    ],

    "tokens": {
        "ttl_seconds": 300,
        "single_use": True,
        "bound_to": ["record_id", "proposed_action", "amount_inr"],
    },
}


# a copy, so a caller that rewrites a ceiling for a what-if cannot edit the real matrix
def load_matrix() -> dict:
    return copy.deepcopy(MATRIX)


# this file's own text, for the report and the Authority screen
def matrix_source() -> str:
    return Path(__file__).read_text(encoding="utf-8")
