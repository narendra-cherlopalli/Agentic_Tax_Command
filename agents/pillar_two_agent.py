"""
tax_command/agents/pillar_two_agent.py

Module 4 — Pillar Two / BEPS Agent.

The most time-critical white-space module. Every multinational group
with consolidated revenue ≥ €750M has needed Pillar Two GloBE compliance
since fiscal years starting in 2024. This is currently almost entirely
Big 4 advisory work — there is no funded agentic product doing this
end-to-end.

Three jobs:
  1. Jurisdictional ETR calculation — per OECD GloBE rules, computed
     deterministically from covered taxes and GloBE income.
  2. Top-up tax calculation — if jurisdictional ETR < 15%, compute the
     top-up tax required, deterministically.
  3. ETR forecasting (ML/advisory) — forecast next period's ETR trajectory
     BEFORE period-end, so Tax has time to act on substance-based carve-outs
     or other planning before the position crystallises. This is the
     single highest-value piece of automation in the entire Tax Command
     suite — turning a quarterly surprise into an early warning.

Architectural rule: jurisdictional_etr_pct and top_up_tax_amount are
PURE ARITHMETIC from covered_taxes and globe_income — Pillar Two is a
formula-driven regime by design, there is no judgment call in the core
calculation. ML is used ONLY for the forward-looking ETRForecast, which
is explicitly a different object (etr_forecasts table) from the actual
filed ETR calculation (pillar_two_etr table).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

GLOBE_MINIMUM_RATE_PCT = 15.0

# De minimis exclusion thresholds per OECD GloBE rules (3-year average)
DE_MINIMIS_REVENUE_THRESHOLD = 10_000_000.0
DE_MINIMIS_INCOME_THRESHOLD = 1_000_000.0


class PillarTwoAgent:
    """
    Computes jurisdictional ETR under OECD Pillar Two GloBE rules,
    determines top-up tax liability, and forecasts ETR trajectory
    to give early warning of approaching breaches.
    """

    def __init__(self, db, etr_forecaster=None) -> None:
        self.db = db
        self.etr_forecaster = etr_forecaster   # optional — ML time-series forecaster

    def run(self, tenant_id: str, period: str,
            jurisdiction_inputs: list[dict], actor: str = "system") -> dict:
        """
        Compute ETR and top-up tax per jurisdiction.

        Parameters
        ----------
        jurisdiction_inputs : list of dicts with keys:
            jurisdiction, entities_in_scope (list[str]), globe_income,
            covered_taxes, three_year_avg_revenue (for de minimis test),
            three_year_avg_income (for de minimis test)
        """
        self.db.append_audit_event(
            tenant_id, "PILLAR_TWO_AGENT_START", actor,
            agent_name="pillar_two", period=period,
            payload={"jurisdiction_count": len(jurisdiction_inputs)},
        )

        results = []
        breach_jurisdictions = []
        total_top_up_tax = 0.0

        for j_input in jurisdiction_inputs:
            result = self._compute_jurisdictional_etr(period, j_input)
            results.append(result)
            self.db.save_etr_calculation(tenant_id, result)

            if result["top_up_tax_required"]:
                breach_jurisdictions.append(result["jurisdiction"])
                total_top_up_tax += result["top_up_tax_amount"]

        if breach_jurisdictions:
            self.db.raise_escalation(tenant_id, {
                "escalation_id": str(uuid.uuid4())[:12],
                "agent_name": "pillar_two",
                "entity_code": "GROUP",
                "period": period,
                "escalation_type": "TOP_UP_TAX_REQUIRED",
                "description": f"Top-up tax required in: {', '.join(breach_jurisdictions)}",
                "amount_usd": round(total_top_up_tax, 2),
            })

        self.db.append_audit_event(
            tenant_id, "PILLAR_TWO_AGENT_COMPLETE", "system",
            agent_name="pillar_two", period=period,
            payload={"breach_jurisdictions": breach_jurisdictions,
                      "total_top_up_tax": round(total_top_up_tax, 2)},
        )

        return {
            "etr_calculations": results,
            "breach_jurisdictions": breach_jurisdictions,
            "total_top_up_tax_usd": round(total_top_up_tax, 2),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Deterministic GloBE ETR calculation — pure OECD formula
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_jurisdictional_etr(self, period: str, j_input: dict) -> dict:
        """
        OECD GloBE jurisdictional ETR formula:

            ETR = covered_taxes / globe_income

            if ETR < 15% AND not de minimis excluded:
                top_up_rate = 15% - ETR
                top_up_tax  = top_up_rate × (globe_income - SBIE)

        SBIE (Substance-Based Income Exclusion) is omitted from this
        simplified formula — in production this requires payroll and
        tangible asset carve-out data per entity, fed in as an additional
        input. Flagged as a TODO for the production build.
        """
        jurisdiction = j_input["jurisdiction"]
        globe_income = float(j_input.get("globe_income", 0))
        covered_taxes = float(j_input.get("covered_taxes", 0))
        avg_revenue = float(j_input.get("three_year_avg_revenue", globe_income))
        avg_income = float(j_input.get("three_year_avg_income", globe_income))

        # De minimis safe harbour test (OECD GloBE Article 5.5.3)
        safe_harbour = None
        if avg_revenue < DE_MINIMIS_REVENUE_THRESHOLD and avg_income < DE_MINIMIS_INCOME_THRESHOLD:
            safe_harbour = "DE_MINIMIS"

        etr_pct = round((covered_taxes / globe_income) * 100.0, 4) if globe_income else 0.0

        top_up_required = False
        top_up_amount = 0.0

        if safe_harbour is None and globe_income > 0 and etr_pct < GLOBE_MINIMUM_RATE_PCT:
            top_up_rate_pct = GLOBE_MINIMUM_RATE_PCT - etr_pct
            # NOTE: production version subtracts SBIE from globe_income here.
            top_up_amount = round(globe_income * (top_up_rate_pct / 100.0), 2)
            top_up_required = True

        return {
            "jurisdiction": jurisdiction,
            "period": period,
            "entities_in_scope": j_input.get("entities_in_scope", []),
            "globe_income": globe_income,
            "covered_taxes": covered_taxes,
            "jurisdictional_etr_pct": etr_pct,
            "minimum_rate_pct": GLOBE_MINIMUM_RATE_PCT,
            "top_up_tax_required": int(top_up_required),
            "top_up_tax_amount": top_up_amount,
            "safe_harbour_applied": safe_harbour,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # ML forecasting — the highest-leverage capability in the whole suite
    # ─────────────────────────────────────────────────────────────────────────

    def forecast_etr(self, tenant_id: str, jurisdiction: str,
                      historical_etrs: list[dict], forecast_period: str) -> Optional[dict]:
        """
        Forecast next period's jurisdictional ETR using historical trend.

        This is the single most valuable capability in Tax Command — it
        converts Pillar Two from a quarterly surprise into an early
        warning system. If a jurisdiction is trending toward breaching
        15%, Tax has months (not days) to evaluate substance-based
        carve-out options, restructure intercompany pricing, or accept
        and plan for the top-up tax.

        Architectural rule: this writes to etr_forecasts, NEVER to
        pillar_two_etr. The forecast does not become a filed position
        until the actual period closes and run() computes the real ETR.
        """
        if self.etr_forecaster and self.etr_forecaster.is_available():
            try:
                ml_result = self.etr_forecaster.forecast(historical_etrs)
                forecasted_etr = ml_result["forecasted_etr_pct"]
                confidence = ml_result.get("confidence", "MEDIUM")
            except Exception as exc:
                logger.warning("ML ETR forecast failed, using trend fallback: %s", exc)
                forecasted_etr, confidence = self._trend_fallback(historical_etrs)
        else:
            forecasted_etr, confidence = self._trend_fallback(historical_etrs)

        if forecasted_etr < GLOBE_MINIMUM_RATE_PCT - 1.0:
            risk_flag = "BREACH_LIKELY"
        elif forecasted_etr < GLOBE_MINIMUM_RATE_PCT + 1.5:
            risk_flag = "WATCH"
        else:
            risk_flag = "SAFE"

        narrative = self._build_forecast_narrative(jurisdiction, forecasted_etr, risk_flag, historical_etrs)

        forecast = {
            "forecast_id": str(uuid.uuid4())[:12],
            "jurisdiction": jurisdiction,
            "period": forecast_period,
            "forecasted_etr_pct": round(forecasted_etr, 2),
            "confidence": confidence,
            "risk_flag": risk_flag,
            "narrative": narrative,
        }
        self.db.save_etr_forecast(tenant_id, forecast)

        if risk_flag in ("WATCH", "BREACH_LIKELY"):
            self.db.raise_escalation(tenant_id, {
                "escalation_id": str(uuid.uuid4())[:12],
                "agent_name": "pillar_two",
                "entity_code": "GROUP",
                "period": forecast_period,
                "escalation_type": "ETR_BREACH_RISK",
                "description": f"{jurisdiction} forecasted ETR {forecasted_etr:.1f}% — {risk_flag}",
                "amount_usd": None,
            })

        return forecast

    @staticmethod
    def _trend_fallback(historical_etrs: list[dict]) -> tuple:
        """Simple linear trend fallback when no ML forecaster is wired in."""
        if len(historical_etrs) < 2:
            last = historical_etrs[-1]["jurisdictional_etr_pct"] if historical_etrs else 20.0
            return last, "LOW"

        values = [h["jurisdictional_etr_pct"] for h in historical_etrs[-4:]]
        deltas = [values[i + 1] - values[i] for i in range(len(values) - 1)]
        avg_delta = sum(deltas) / len(deltas)
        forecast = values[-1] + avg_delta
        confidence = "MEDIUM" if len(values) >= 3 else "LOW"
        return forecast, confidence

    @staticmethod
    def _build_forecast_narrative(jurisdiction: str, etr: float, risk_flag: str,
                                   historical: list[dict]) -> str:
        trend = "stable"
        if len(historical) >= 2:
            prior = historical[-1]["jurisdictional_etr_pct"]
            if etr < prior - 0.5:
                trend = "declining"
            elif etr > prior + 0.5:
                trend = "improving"

        if risk_flag == "BREACH_LIKELY":
            return (
                f"{jurisdiction} ETR forecasted at {etr:.1f}%, {trend} trend, "
                f"likely to breach the 15% GloBE minimum. Recommend reviewing "
                f"substance-based income exclusion eligibility and intercompany "
                f"pricing in this jurisdiction before period-end."
            )
        elif risk_flag == "WATCH":
            return (
                f"{jurisdiction} ETR forecasted at {etr:.1f}%, {trend} trend, "
                f"approaching the 15% threshold. Monitor closely — no action "
                f"required yet, but model the top-up tax exposure as a precaution."
            )
        return f"{jurisdiction} ETR forecasted at {etr:.1f}%, {trend} trend, comfortably above the GloBE minimum."

    # ─────────────────────────────────────────────────────────────────────────
    # QDMTT filing generation
    # ─────────────────────────────────────────────────────────────────────────

    def generate_qdmtt_filing(self, tenant_id: str, jurisdiction: str, period: str,
                               ultimate_parent: str, due_date: str) -> dict:
        """
        Generate a draft QDMTT filing from the computed ETR for a jurisdiction.
        Requires HITL approval before status moves to FILED.
        """
        etrs = self.db.get_etr_calculations(tenant_id, period=period)
        match = next((e for e in etrs if e["jurisdiction"] == jurisdiction), None)

        if not match or not match["top_up_tax_required"]:
            return {"generated": False, "reason": "No top-up tax required for this jurisdiction/period."}

        filing = {
            "filing_id": str(uuid.uuid4())[:12],
            "jurisdiction": jurisdiction,
            "period": period,
            "ultimate_parent_entity": ultimate_parent,
            "top_up_tax_amount": match["top_up_tax_amount"],
            "allocated_entities": match["entities_in_scope"],
            "status": "DRAFT",
            "due_date": due_date,
            "filed_at": None,
        }
        self.db.save_qdmtt_filing(tenant_id, filing)
        return {"generated": True, "filing": filing}
