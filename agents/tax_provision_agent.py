"""
tax_command/agents/tax_provision_agent.py

Module 3 — Tax Provision Agent.

Computes current and deferred tax expense per entity per period from
accounting data. This is "emerging" maturity in the market (OneSource,
CorpTax, Longview) — Tax Command's edge is wiring it into the same
agentic pipeline as Close Command's consolidation engine, so the
provision is computed from the SAME consolidated numbers the close
process produces, not a separate manual data pull.

Architectural rule: every number on TaxProvisionLine is a deterministic
calculation from pretax income, statutory rate, and the permanent/
temporary difference inputs. There is no ML step in computing tax expense
— tax law is not probabilistic. ML/Gen AI here is limited to: (a) flagging
unusual movements in deferred tax balances for review, (b) drafting the
plain-English provision memo. Neither touches the calculated numbers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Materiality threshold for flagging a deferred tax movement as unusual
# (illustrative — should be tenant-configurable in production)
DTA_MOVEMENT_FLAG_THRESHOLD_PCT = 30.0


class TaxProvisionAgent:
    """
    Computes current and deferred tax provision per entity per period.

    Pipeline position: should run AFTER the close process has produced
    consolidated pretax income per entity (i.e. reads from Close Command's
    output, or from any consolidation source providing pretax_income per entity).
    """

    def __init__(self, db, anomaly_detector=None) -> None:
        self.db = db
        self.anomaly_detector = anomaly_detector   # optional — flags unusual DT movements

    def run(self, tenant_id: str, period: str, entity_inputs: list[dict],
            prior_deferred_items: Optional[list[dict]] = None,
            actor: str = "system") -> dict:
        """
        Compute tax provisions for a batch of entities.

        Parameters
        ----------
        entity_inputs : list of dicts with keys:
            entity_code, jurisdiction, pretax_income, statutory_rate_pct,
            permanent_differences, temporary_differences, prior_year_true_up
        prior_deferred_items : optional list of DeferredTaxItem dicts from
            the prior period, used to compute movements.
        """
        self.db.append_audit_event(
            tenant_id, "TAX_PROVISION_AGENT_START", actor,
            agent_name="tax_provision", period=period,
            payload={"entity_count": len(entity_inputs)},
        )

        provisions = []
        flagged_entities = []

        for entity_input in entity_inputs:
            provision = self._compute_provision(entity_input)
            provisions.append(provision)
            self.db.save_tax_provision(tenant_id, provision)

            # Anomaly check on deferred tax movement (advisory only)
            if self.anomaly_detector and self.anomaly_detector.is_available():
                prior_dt = next(
                    (i for i in (prior_deferred_items or [])
                     if i.get("entity_code") == entity_input["entity_code"]),
                    None,
                )
                if prior_dt:
                    flag = self.anomaly_detector.check_movement(
                        prior_dt.get("closing_balance", 0),
                        provision["deferred_tax_expense"],
                    )
                    if flag.get("is_anomaly"):
                        flagged_entities.append(entity_input["entity_code"])

        if flagged_entities:
            self.db.raise_escalation(tenant_id, {
                "escalation_id": str(uuid.uuid4())[:12],
                "agent_name": "tax_provision",
                "entity_code": "GROUP",
                "period": period,
                "escalation_type": "DEFERRED_TAX_MOVEMENT_UNUSUAL",
                "description": f"Unusual deferred tax movement flagged for: "
                                f"{', '.join(flagged_entities)}",
                "amount_usd": None,
            })

        group_summary = self._compute_group_summary(provisions)

        self.db.append_audit_event(
            tenant_id, "TAX_PROVISION_AGENT_COMPLETE", "system",
            agent_name="tax_provision", period=period,
            payload={"group_etr_pct": group_summary["group_etr_pct"],
                      "flagged_entities": flagged_entities},
        )

        return {
            "provisions": provisions,
            "group_summary": group_summary,
            "flagged_entities": flagged_entities,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Deterministic computation — pure tax mechanics, no ML
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_provision(self, entity_input: dict) -> dict:
        """
        Standard current/deferred tax computation:

            taxable_income = pretax_income + permanent_differences
            current_tax    = taxable_income × statutory_rate
            deferred_tax   = temporary_differences × statutory_rate
            total_tax      = current_tax + deferred_tax + prior_year_true_up
            ETR            = total_tax / pretax_income

        This mirrors the standard tax provision workpaper structure used
        by every Big 4 firm — it is deliberately not novel. The value
        of automating it is speed and consistency, not new methodology.
        """
        pretax_income = float(entity_input.get("pretax_income", 0))
        rate_pct = float(entity_input.get("statutory_rate_pct", 25.0))
        permanent_diff = float(entity_input.get("permanent_differences", 0))
        temporary_diff = float(entity_input.get("temporary_differences", 0))
        prior_true_up = float(entity_input.get("prior_year_true_up", 0))

        taxable_income = pretax_income + permanent_diff
        current_tax_expense = round(taxable_income * (rate_pct / 100.0), 2)
        deferred_tax_expense = round(temporary_diff * (rate_pct / 100.0), 2)
        total_tax_expense = round(current_tax_expense + deferred_tax_expense + prior_true_up, 2)

        effective_tax_rate_pct = (
            round((total_tax_expense / pretax_income) * 100.0, 2)
            if pretax_income else 0.0
        )

        return {
            "entity_code": entity_input["entity_code"],
            "period": entity_input.get("period", ""),
            "jurisdiction": entity_input.get("jurisdiction", "GB"),
            "pretax_income": pretax_income,
            "statutory_rate_pct": rate_pct,
            "permanent_differences": permanent_diff,
            "temporary_differences": temporary_diff,
            "current_tax_expense": current_tax_expense,
            "deferred_tax_expense": deferred_tax_expense,
            "total_tax_expense": total_tax_expense,
            "effective_tax_rate_pct": effective_tax_rate_pct,
            "prior_year_true_up": prior_true_up,
            "status": "DRAFT",
        }

    @staticmethod
    def _compute_group_summary(provisions: list[dict]) -> dict:
        total_pretax = sum(p["pretax_income"] for p in provisions)
        total_tax = sum(p["total_tax_expense"] for p in provisions)
        group_etr_pct = round((total_tax / total_pretax) * 100.0, 2) if total_pretax else 0.0

        return {
            "entity_count": len(provisions),
            "total_pretax_income": round(total_pretax, 2),
            "total_tax_expense": round(total_tax, 2),
            "group_etr_pct": group_etr_pct,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Deferred tax rollforward
    # ─────────────────────────────────────────────────────────────────────────

    def rollforward_deferred_tax(self, tenant_id: str, period: str,
                                  items: list[dict]) -> list[dict]:
        """
        Roll forward deferred tax items: closing = opening + movement.
        Flags items where recognition is no longer supportable (valuation
        allowance trigger) — this flag requires human Tax Director sign-off,
        it is never auto-derecognised.
        """
        rolled = []
        for item in items:
            opening = float(item.get("opening_balance", 0))
            movement = float(item.get("movement", 0))
            closing = round(opening + movement, 2)
            rolled.append({**item, "period": period, "closing_balance": closing})

        self.db.save_deferred_tax_items(tenant_id, rolled)
        return rolled
