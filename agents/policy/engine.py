"""The authority gate. Deterministic, deny-first, and containing no model call anywhere."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import unicodedata
import uuid
from decimal import Decimal

from core import Band, PolicyDecision, Proposal, RunContext, fmt, money
from . import earned
from .matrix import load_matrix, matrix_source

SECRET = os.environ.get("RECON_POLICY_SECRET", "dev-only-policy-secret").encode()

# characters used to smuggle instructions past a naive substring check
INVISIBLE = "​‌‍⁠﻿­‪‫‬‭‮‎‏"

# NFKC does not fold Cyrillic or Greek onto Latin, so "Ѕystem:" survives normalisation
HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P",
    "С": "C", "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ј": "J", "Ԍ": "G",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
    "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ѵ": "v", "ӏ": "l", "ԛ": "q", "ԝ": "w",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "α": "a", "ε": "e",
    "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
})

# Enumerating phrasings loses. These match the SHAPE of an instruction instead: a machine
# field like a bank narration has no business discussing review, approval or confidence at
# all, so the vocabulary itself is the signal. False positives only cost an extra
# escalation, which is the safe direction for a hard stop.
INSTRUCTION_PATTERNS = [
    ("auto_approve", r"auto[\s\-_]?(approv|resolv)|approve\s+automatic|close[\w\s]{0,24}automatic"),
    ("escalation_language", r"escalat|manual\s+review|human\s+review|do\s*not\s+(flag|hold|send|surface|review)"),
    ("review_waiver", r"no\s+(review|action|further\s+\w+)\s+(needed|required)|(review|verification|approval)\s+not\s+(needed|required)|skip[\w\s]{0,20}(step|review|verif|check)"),
    ("skip_verification", r"(skip|bypass|disable|waive|omit)[\w\s]{0,20}(verif|review|check|polic|arithmetic)|policy\s+check"),
    ("role_injection", r"(^|\n|\s)(system|assistant|user)\s*:|\[system\]|<\|im_start\|>|###\s*new\s+instruction|override\s*:"),
    ("comment_hiding", r"<!--|-->|/\*|\*/|#\s*noqa"),
    ("ignore_instructions", r"ignore\s+(all\s+)?(previous|prior|earlier)|instructions?\s+superseded|previous\s+instructions"),
    ("confidence_manipulation", r"confidence|evidence_chain|arithmetic_verified|policy_band|threshold\s+raised|assume\s+verif|verification\s+passed"),
    ("authority_appeal", r"\bcfo\b|controller|auditor|treasury|head\s+of\s+finance|ap\s+ops|board[\s\-]approv|sign(ed)?[\s\-]?off|delegated\s+authority|approved\s+(by|internally|rule)|cleared\s+by"),
    ("fake_precedent", r"rule-\d+|previously\s+auto|whitelist|standing\s+rule|precedent|apply\s+the\s+same|historically\s+resolved|ceiling\s+waiv"),
    ("pre_authorised", r"pre[\s\-]?(authoris|authoriz|cleared)|exempt\s+from|permitted\b"),
    ("urgency_pressure", r"penalty|breach|\bsla\b|time\s+critical|urgent|audit\s+closes|quarter\s+end|regulatory\s+filing|blocked\s+on|blocks\s+the"),
    ("marked_resolved", r"(mark|treat|report)\s+(this\s+)?(as\s+)?(reconciled|matched|exact_match|resolved|high\s+confidence)|reconciliation\s+complete|handled\s+offline"),
]


# fold away the obfuscation first, and count the obfuscation itself as a signal
def scan_for_instructions(*texts: str) -> list[str]:
    hits: list[str] = []
    for raw in texts:
        if not raw:
            continue
        if any(c in raw for c in INVISIBLE):
            hits.append("invisible_characters")
        clean = "".join(c for c in raw if c not in INVISIBLE)
        folded = clean.translate(HOMOGLYPHS)
        if folded != clean:
            hits.append("homoglyph_characters")
        folded = unicodedata.normalize("NFKC", folded).lower()
        for name, pattern in INSTRUCTION_PATTERNS:
            if re.search(pattern, folded, re.IGNORECASE | re.MULTILINE):
                hits.append(name)
    return sorted(set(hits))


# what the token is bound to; change any of these and the token stops verifying
def action_hash(p: Proposal) -> str:
    canon = json.dumps({"record_id": p.record_id, "proposed_action": p.proposed_action,
                        "amount_inr": fmt(p.amount_inr)}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


def _sign(payload: str) -> str:
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]


# only the policy engine mints these, and only for a band it just granted
def issue_token(p: Proposal, band: str, ttl_seconds: int) -> str:
    body = {"proposal_id": p.proposal_id, "action": action_hash(p), "band": band,
            "exp": int(time.time()) + ttl_seconds, "nonce": uuid.uuid4().hex[:16]}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return payload.encode().hex() + "." + _sign(payload)


# a token is valid only for the exact proposal it was minted for, once, before it expires
def verify_token(token: str, p: Proposal, ctx: RunContext) -> tuple[bool, str]:
    try:
        hex_payload, sig = token.split(".", 1)
        payload = bytes.fromhex(hex_payload).decode()
    except (ValueError, UnicodeDecodeError):
        return False, "malformed token"
    if not hmac.compare_digest(sig, _sign(payload)):
        return False, "bad signature"
    body = json.loads(payload)
    if body["proposal_id"] != p.proposal_id:
        return False, "token is for a different proposal"
    if body["action"] != action_hash(p):
        return False, "proposal was modified after the token was issued"
    if body["exp"] < int(time.time()):
        return False, "token expired"
    if body["nonce"] in ctx.spent_nonces:
        return False, "token already spent"
    ctx.spent_nonces.append(body["nonce"])
    return True, "ok"


# deterministic owner lookup; who reviews an exception is never a judgement call
def route(p: Proposal, matrix: dict) -> str:
    amount = money(p.amount_inr)
    for rule in matrix["routing"]:
        if "default" in rule:
            return rule["default"]
        if "if_class" in rule and p.proposed_class in rule["if_class"]:
            if (cap := rule.get("escalate_over_inr")) and amount > money(cap):
                return rule["escalate_to"]
            return rule["owner"]
        if (cap := rule.get("if_amount_over_inr")) and amount > money(cap):
            return rule["owner"]
    return "finance_controller"


def _hard_stops(p: Proposal, ctx: RunContext, hs: dict, ablations=frozenset()) -> list[str]:
    out = []
    if hs.get("on_locked_period") and p.period and p.period in ctx.locked_periods:
        out.append(f"period {p.period} is closed and locked")
    if hs.get("on_counterparty_absent") and not p.counterparty_in_master:
        out.append("counterparty is not in the master")
    if hs.get("on_empty_evidence_chain") and not p.evidence_chain:
        out.append("evidence chain is empty")
    if money(p.amount_inr) > money(hs["max_ledger_write_inr"]):
        out.append(f"amount exceeds the hard ceiling of {fmt(money(hs['max_ledger_write_inr']))}")
    if hs.get("on_instruction_like_source_text") and "no_injection_defense" not in ablations:
        if flags := scan_for_instructions(*p.source_texts):
            out.append(f"source text contains instruction-like content: {', '.join(flags)}")
    return out


def _breakers(ctx: RunContext, cb: dict) -> list[str]:
    out = []
    if ctx.autonomy_halted:
        out.append(f"autonomy halted for this run: {ctx.halt_reason}")
    if ctx.auto_action_count >= cb["max_auto_actions_per_run"]:
        out.append(f"run cap of {cb['max_auto_actions_per_run']} autonomous actions reached")
    if ctx.auto_actioned_value_inr >= money(cb["max_auto_actioned_value_inr"]):
        out.append(f"run blast-radius cap of {fmt(money(cb['max_auto_actioned_value_inr']))} reached")
    if ctx.disagreement_rate > Decimal(str(cb["halt_autonomy_if_disagreement_rate_over"])):
        out.append(f"verifier disagreement rate {ctx.disagreement_rate:.3f} over threshold")
    return out


def _human_triggers(p: Proposal, hr: dict, earned_inr=None) -> list[str]:
    out = []
    limit = money(hr["trigger_if_amount_over_inr"])
    # an earned rule can raise this one threshold and nothing else
    if earned_inr is not None and earned_inr > limit:
        limit = earned_inr
    if money(p.amount_inr) > limit:
        out.append(f"amount over {fmt(limit)}")
    if p.proposed_class in hr["trigger_if_class"]:
        out.append(f"class {p.proposed_class} always needs a human")
    if hr.get("trigger_if_unverified") and not p.arithmetic_verified:
        out.append("arithmetic was not verified in code")
    return out


# does a proposal satisfy one granting band's requirements
def _grants(p: Proposal, band_cfg: dict, earned_inr=None) -> tuple[bool, list[str]]:
    req, missing = band_cfg["requires"], []
    ceiling = money(band_cfg["max_amount_inr"])
    if earned_inr is not None and earned_inr > ceiling:
        ceiling = earned_inr
    if money(p.amount_inr) > ceiling:
        missing.append(f"over the band ceiling {fmt(ceiling)}")
    if req.get("arithmetic_verified") and not p.arithmetic_verified:
        missing.append("arithmetic not verified")
    if req.get("evidence_chain_complete") and not p.evidence_chain:
        missing.append("evidence chain incomplete")
    if req.get("counterparty_in_master") and not p.counterparty_in_master:
        missing.append("counterparty not in master")
    if (classes := band_cfg.get("classes")) and p.proposed_class not in classes:
        missing.append(f"class {p.proposed_class} not in this band")
    return (not missing), missing


# HARD_STOP -> breakers -> HUMAN_REQUIRED -> grants. Anything unmatched falls to a human.
def evaluate(p: Proposal, ctx: RunContext, matrix: dict | None = None,
             ablations=frozenset()) -> PolicyDecision:
    m = matrix or load_matrix()
    fired: list[str] = []
    earned_inr, rule_id = (None, None)
    if "no_precedents" not in ablations:
        earned_inr, rule_id = earned.ceiling_for(p)
    if rule_id:
        fired.append(f"EARNED:{rule_id}")

    if reasons := _hard_stops(p, ctx, m["bands"]["HARD_STOP"], ablations):
        return PolicyDecision(proposal_id=p.proposal_id, band=Band.HARD_STOP.value,
                              allowed=False, reasons=reasons, owner=route(p, m),
                              rules_fired=["HARD_STOP"])

    if reasons := _breakers(ctx, m["circuit_breakers"]):
        return PolicyDecision(proposal_id=p.proposal_id, band=Band.HUMAN_REQUIRED.value,
                              allowed=False, reasons=reasons, owner=route(p, m),
                              rules_fired=["CIRCUIT_BREAKER"])

    if reasons := _human_triggers(p, m["bands"]["HUMAN_REQUIRED"], earned_inr):
        return PolicyDecision(proposal_id=p.proposal_id, band=Band.HUMAN_REQUIRED.value,
                              allowed=False, reasons=reasons, owner=route(p, m),
                              rules_fired=["HUMAN_REQUIRED"])

    # narrowest granting band first, so the quiet one wins when both would allow it
    for band in (Band.AUTO_RESOLVE.value, Band.AUTO_WITH_NOTICE.value):
        cfg = m["bands"][band]
        ok, missing = _grants(p, cfg, earned_inr if band == Band.AUTO_RESOLVE.value else None)
        fired.append(f"{band}:{'grant' if ok else 'no'}")
        if ok:
            return PolicyDecision(
                proposal_id=p.proposal_id, band=band, allowed=True,
                reasons=[cfg["effect"]] + ([f"earned rule {rule_id} raised the ceiling to "
                                            f"{fmt(earned_inr)}"] if rule_id else []),
                owner=None,
                authorization_token=issue_token(p, band, m["tokens"]["ttl_seconds"]),
                ceiling_inr=money(cfg["max_amount_inr"]), rules_fired=fired)

    return PolicyDecision(proposal_id=p.proposal_id, band=Band.HUMAN_REQUIRED.value,
                          allowed=False, owner=route(p, m), rules_fired=fired,
                          reasons=["no band grants this proposal; default is a human"])


# structural stops only: the injection scan is skipped because the pack SHOWED the human
# the text, and a reviewer who has read the bait is the right party to judge it. See
# archi.md section 8.
def evaluate_human_approved(p: Proposal, ctx: RunContext,
                            matrix: dict | None = None) -> PolicyDecision:
    m = matrix or load_matrix()
    hs = m["bands"]["HARD_STOP"]
    reasons = _hard_stops(p, ctx, hs, ablations={"no_injection_defense"})
    if reasons:
        return PolicyDecision(proposal_id=p.proposal_id, band=Band.HARD_STOP.value,
                              allowed=False, reasons=reasons, owner=route(p, m),
                              rules_fired=["HARD_STOP", "HUMAN_APPROVED:refused"])
    return PolicyDecision(
        proposal_id=p.proposal_id, band=Band.HUMAN_APPROVED.value, allowed=True,
        reasons=["a human approved this exact proposal after seeing the evidence pack"],
        owner=None,
        authorization_token=issue_token(p, Band.HUMAN_APPROVED.value,
                                        m["tokens"]["ttl_seconds"]),
        ceiling_inr=money(hs["max_ledger_write_inr"]), rules_fired=["HUMAN_APPROVED"])


# call after an allowed action actually happens, so the breakers can trip
def record_action(p: Proposal, ctx: RunContext) -> None:
    ctx.auto_action_count += 1
    ctx.auto_actioned_value_inr = money(ctx.auto_actioned_value_inr + money(p.amount_inr))


def record_verification(ctx: RunContext, agreed: bool, matrix: dict | None = None) -> None:
    m = matrix or load_matrix()
    ctx.verifier_checks += 1
    if not agreed:
        ctx.verifier_disagreements += 1
    limit = Decimal(str(m["circuit_breakers"]["halt_autonomy_if_disagreement_rate_over"]))
    if ctx.disagreement_rate > limit and not ctx.autonomy_halted:
        ctx.autonomy_halted = True
        ctx.halt_reason = f"verifier disagreement {ctx.disagreement_rate:.3f} over {limit}"
