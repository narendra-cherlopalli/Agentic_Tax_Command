"""
tax_command/agents/vat_agent.py

Module 1 — Indirect Tax / VAT Agent.

Classifies transactions for VAT/GST treatment across jurisdictions and
builds periodic VAT returns. This is the most "mature" category in the
market (Vertex, Avalara, TaxJar already do this well) — Tax Command's
differentiation here is integration into the same agentic pipeline as
the other three modules, not novel VAT logic.

Architectural rule: the rule engine (deterministic) makes every VAT
classification. The ML classifier (see ml/vat_classifier.py) only
SUGGESTS a treatment for transactions the rule engine cannot classify
with certainty — it never overrides a rule engine result.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Deterministic VAT rule table (extend per jurisdiction) ────────────────────
# In production this would be loaded from master data, not hardcoded.
DEFAULT_VAT_RATES = {
    "GB": {"standard": 20.0, "reduced": 5.0, "zero": 0.0},
    "DE": {"standard": 19.0, "reduced": 7.0, "zero": 0.0},
    "FR": {"standard": 20.0, "reduced": 10.0, "zero": 0.0},
    "NL": {"standard": 21.0, "reduced": 9.0, "zero": 0.0},
    "IE": {"standard": 23.0, "reduced": 13.5, "zero": 0.0},
    "SG": {"standard": 9.0, "reduced": 9.0, "zero": 0.0},
}

# Account codes that are typically exempt or out of scope (illustrative — configure per tenant)
EXEMPT_ACCOUNT_PREFIXES = ("INSURANCE", "FINANCIAL_SERVICES", "PAYROLL")


class VATAgent:
    """
    Classifies VAT transactions and produces periodic VAT returns.

    Pipeline position: runs independently per period/entity — does not
    depend on the other three Tax Command agents. Can run standalone
    or as part of the full LangGraph orchestration.
    """

    def __init__(self, db, ml_classifier=None, retriever=None) -> None:
        self.db = db
        self.ml_classifier = ml_classifier   # optional — see ml/vat_classifier.py
        self.retriever = retriever            # optional — RAG for jurisdiction rule lookup

    def run(self, tenant_id: str, entity_code: str, transactions: list[dict],
            period_start: str, period_end: str, actor: str = "system") -> dict:
        """
        Classify a batch of transactions and produce a VAT return.

        Parameters
        ----------
        transactions : list of dicts with keys: txn_id, jurisdiction,
            transaction_date, counterparty, counterparty_vat_number,
            amount_net, currency, description, account_code

        Returns
        -------
        dict with keys: classified_transactions, vat_return, flagged_count
        """
        self.db.append_audit_event(
            tenant_id, "VAT_AGENT_START", actor,
            agent_name="vat", entity_code=entity_code,
            payload={"period_start": period_start, "period_end": period_end,
                      "transaction_count": len(transactions)},
        )

        classified = []
        flagged_count = 0
        output_vat_total = 0.0
        input_vat_total = 0.0

        for txn in transactions:
            result = self._classify_transaction(tenant_id, entity_code, txn)
            classified.append(result)
            if result["requires_review"]:
                flagged_count += 1
            if result.get("vat_amount"):
                if float(txn.get("amount_net", 0)) >= 0:
                    output_vat_total += result["vat_amount"]
                else:
                    input_vat_total += abs(result["vat_amount"])

        self.db.save_vat_transactions(tenant_id, classified)

        vat_return = {
            "return_id": str(uuid.uuid4())[:12],
            "entity_code": entity_code,
            "jurisdiction": transactions[0]["jurisdiction"] if transactions else "GB",
            "period_start": period_start,
            "period_end": period_end,
            "output_vat": round(output_vat_total, 2),
            "input_vat": round(input_vat_total, 2),
            "net_vat_payable": round(output_vat_total - input_vat_total, 2),
            "transaction_count": len(transactions),
            "flagged_count": flagged_count,
            "status": "DRAFT",
        }
        self.db.save_vat_return(tenant_id, vat_return)

        if flagged_count > 0:
            self.db.raise_escalation(tenant_id, {
                "escalation_id": str(uuid.uuid4())[:12],
                "agent_name": "vat",
                "entity_code": entity_code,
                "period": period_start[:7],
                "escalation_type": "VAT_JURISDICTION_UNCLEAR",
                "description": f"{flagged_count} of {len(transactions)} transactions "
                                f"require manual VAT treatment review.",
                "amount_usd": None,
            })

        self.db.append_audit_event(
            tenant_id, "VAT_AGENT_COMPLETE", "system",
            agent_name="vat", entity_code=entity_code,
            payload={"flagged_count": flagged_count, "net_vat_payable": vat_return["net_vat_payable"]},
        )

        return {
            "classified_transactions": classified,
            "vat_return": vat_return,
            "flagged_count": flagged_count,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Deterministic classification (the rule engine)
    # ─────────────────────────────────────────────────────────────────────────

    def _classify_transaction(self, tenant_id: str, entity_code: str, txn: dict) -> dict:
        """
        Deterministic rule engine. ML is consulted ONLY when the rule
        engine cannot determine a treatment with confidence — and even
        then, the ML suggestion is recorded as classification_source =
        "ML_SUGGESTED" with requires_review = True. It never silently
        becomes the final treatment.
        """
        jurisdiction = txn.get("jurisdiction", "GB")
        account_code = str(txn.get("account_code", ""))
        description = str(txn.get("description", "")).upper()
        amount_net = float(txn.get("amount_net", 0))

        rates = DEFAULT_VAT_RATES.get(jurisdiction, {"standard": 20.0, "reduced": 0.0, "zero": 0.0})

        treatment = None
        rate_pct = None
        confidence = "HIGH"
        source = "RULE_ENGINE"
        requires_review = False

        # Rule 1: exempt categories by description keyword
        if any(kw in description for kw in EXEMPT_ACCOUNT_PREFIXES):
            treatment, rate_pct = "EXEMPT", 0.0

        # Rule 2: reverse charge for cross-border B2B services (simplified — extend per tenant)
        elif txn.get("counterparty_vat_number") and not txn["counterparty_vat_number"].startswith(jurisdiction):
            treatment, rate_pct = "REVERSE_CHARGE", 0.0

        # Rule 3: zero-rated exports (no VAT number, international counterparty signal)
        elif "EXPORT" in description:
            treatment, rate_pct = "ZERO_RATED", 0.0

        # Rule 4: standard domestic rate
        elif (txn.get("counterparty_vat_number") or "").startswith(jurisdiction) or not txn.get("counterparty_vat_number"):
            treatment, rate_pct = "STANDARD", rates["standard"]

        # Fallback: consult ML classifier if available, else flag for manual review
        else:
            if self.ml_classifier and self.ml_classifier.is_available():
                suggestion = self.ml_classifier.suggest(txn, jurisdiction)
                treatment = suggestion.get("suggested_treatment", "STANDARD")
                rate_pct = suggestion.get("suggested_rate_pct", rates["standard"])
                confidence = suggestion.get("confidence", "LOW")
                source = "ML_SUGGESTED"
                requires_review = True
            else:
                treatment, rate_pct = "STANDARD", rates["standard"]
                confidence = "LOW"
                requires_review = True

        vat_amount = round(amount_net * (rate_pct / 100.0), 2) if rate_pct else 0.0

        return {
            **txn,
            "entity_code": entity_code,
            "vat_treatment": treatment,
            "vat_rate_pct": rate_pct,
            "vat_amount": vat_amount,
            "place_of_supply": jurisdiction,
            "classification_source": source,
            "classification_confidence": confidence,
            "requires_review": int(requires_review),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # File the return (HITL gate — requires explicit human approval)
    # ─────────────────────────────────────────────────────────────────────────

    def file_return(self, tenant_id: str, return_id: str, entity_code: str,
                     filed_by: str) -> dict:
        """
        Mark a VAT return as filed. This is a HITL action — never called
        automatically by run(). A human must explicitly invoke this after
        reviewing flagged transactions.
        """
        returns = [r for r in self.db.get_vat_transactions(tenant_id, entity_code=entity_code)]
        flagged_remaining = sum(1 for r in returns if r.get("requires_review"))

        if flagged_remaining > 0:
            return {
                "filed": False,
                "reason": f"{flagged_remaining} transactions still require review before filing.",
            }

        self.db.append_audit_event(
            tenant_id, "VAT_RETURN_FILED", filed_by,
            agent_name="vat", entity_code=entity_code,
            payload={"return_id": return_id},
        )
        return {"filed": True, "filed_at": datetime.utcnow().isoformat(), "filed_by": filed_by}
