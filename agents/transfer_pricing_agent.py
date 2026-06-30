"""
tax_command/agents/transfer_pricing_agent.py

Module 2 — Transfer Pricing Agent.

The highest white-space module in the suite. Almost no funded agentic
product does this today — it's entirely Big 4 advisory work at $500K+
per engagement for a mid-market multinational.

Three jobs:
  1. Deterministic arm's-length range check — is the actual margin on an
     intercompany transaction within the TP policy's documented benchmark range?
  2. LLM advisory (TPAdvisory) — for transactions with NO existing benchmark,
     suggest a method and range using RAG-retrieved comparable context.
     This is NEVER the final position — a human Tax Director sets the
     final benchmark range before it's used to test arm's-length compliance.
  3. Documentation tracking — master file / local file / CbCR completeness
     per entity per period, the thing that actually gets audited.

Architectural rule: tp_method and benchmark_range on IntercompanyTransaction
are set by TP POLICY (deterministic, human-defined), never by the LLM.
The LLM's TPAdvisory is a separate, clearly-labeled object that requires
a human to promote it into policy before it has any effect on a filing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Materiality threshold below which TP documentation is not legally required
# in most jurisdictions (illustrative — configure per tenant's TP policy)
DOCUMENTATION_THRESHOLD_USD = 250_000.0

VALID_TP_METHODS = {"CUP", "RPM", "CPM", "TNMM", "PSM"}


class TransferPricingAgent:
    """
    Tests intercompany transactions against arm's-length benchmark ranges,
    tracks documentation completeness, and generates LLM advisory for
    unbenchmarked transaction types.
    """

    def __init__(self, db, llm_client=None, retriever=None) -> None:
        self.db = db
        self.llm_client = llm_client    # optional — Anthropic API client for advisory generation
        self.retriever = retriever       # optional — RAG over prior TP studies, OECD guidelines

    def run(self, tenant_id: str, period: str, transactions: list[dict],
            actor: str = "system") -> dict:
        """
        Test a batch of intercompany transactions for arm's-length compliance.

        Parameters
        ----------
        transactions : list of dicts matching IntercompanyTransaction fields.
            tp_method and benchmark_range_low/high should already be set
            from TP policy where known — this agent tests compliance and
            flags transactions with no existing benchmark for advisory.
        """
        self.db.append_audit_event(
            tenant_id, "TP_AGENT_START", actor,
            agent_name="transfer_pricing", period=period,
            payload={"transaction_count": len(transactions)},
        )

        tested = []
        needs_advisory = []
        non_arms_length_count = 0

        for txn in transactions:
            txn_with_period = {**txn, "period": txn.get("period", period)}
            result = self._test_arms_length(txn_with_period)
            tested.append(result)
            if result["is_arms_length"] is False:
                non_arms_length_count += 1
            if result["tp_method"] is None:
                needs_advisory.append(result)

        self.db.save_tp_transactions(tenant_id, tested)

        # Generate advisory for transactions with no benchmark — advisory only,
        # never auto-applied to the transaction record.
        advisories_generated = 0
        for txn in needs_advisory:
            advisory = self._generate_advisory(tenant_id, txn)
            if advisory:
                self.db.save_tp_advisory(tenant_id, advisory)
                advisories_generated += 1

        if non_arms_length_count > 0:
            total_at_risk = sum(
                abs(float(t.get("amount", 0))) for t in tested
                if t["is_arms_length"] is False
            )
            self.db.raise_escalation(tenant_id, {
                "escalation_id": str(uuid.uuid4())[:12],
                "agent_name": "transfer_pricing",
                "entity_code": "GROUP",
                "period": period,
                "escalation_type": "TP_OUTSIDE_BENCHMARK",
                "description": f"{non_arms_length_count} intercompany transactions "
                                f"outside their documented arm's-length range.",
                "amount_usd": round(total_at_risk, 2),
            })

        self.db.append_audit_event(
            tenant_id, "TP_AGENT_COMPLETE", "system",
            agent_name="transfer_pricing", period=period,
            payload={
                "non_arms_length_count": non_arms_length_count,
                "advisories_generated": advisories_generated,
            },
        )

        return {
            "tested_transactions": tested,
            "non_arms_length_count": non_arms_length_count,
            "advisories_generated": advisories_generated,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Deterministic compliance test
    # ─────────────────────────────────────────────────────────────────────────

    def _test_arms_length(self, txn: dict) -> dict:
        """
        Deterministic test: is actual_margin_pct within the documented
        benchmark range? This NEVER consults the LLM — it's a pure
        numerical comparison against policy-set values.
        """
        method = txn.get("tp_method")
        low = txn.get("benchmark_range_low")
        high = txn.get("benchmark_range_high")
        margin = txn.get("actual_margin_pct")

        is_arms_length = None
        requires_documentation = abs(float(txn.get("amount", 0))) >= DOCUMENTATION_THRESHOLD_USD

        if method and low is not None and high is not None and margin is not None:
            is_arms_length = bool(low <= margin <= high)
            if method not in VALID_TP_METHODS:
                logger.warning("Unrecognised TP method '%s' on txn %s", method, txn.get("txn_id"))

        return {
            **txn,
            "is_arms_length": is_arms_length,
            "requires_documentation": int(requires_documentation),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # LLM advisory — clearly separated from the deterministic position
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_advisory(self, tenant_id: str, txn: dict) -> Optional[dict]:
        """
        Generate an LLM-based TP method and benchmark suggestion for a
        transaction with no existing policy. This is advisory ONLY —
        it populates tp_advisories, a separate table from tp_transactions.
        A Tax Director must explicitly review and promote this into
        TP policy before it affects any filing position.
        """
        context = ""
        if self.retriever:
            try:
                context = self.retriever.retrieve(
                    query=f"transfer pricing method for {txn.get('transaction_type')} "
                          f"between {txn.get('seller_jurisdiction')} and {txn.get('buyer_jurisdiction')}",
                    top_k=5,
                )
            except Exception as exc:
                logger.warning("RAG retrieval failed for TP advisory: %s", exc)

        if not self.llm_client:
            # No LLM configured — return a structured placeholder advisory
            # using OECD default method selection logic (deterministic fallback)
            suggested_method = self._default_method_by_type(txn.get("transaction_type", ""))
            return {
                "advisory_id": str(uuid.uuid4())[:12],
                "txn_id": txn.get("txn_id"),
                "suggested_method": suggested_method,
                "suggested_benchmark_low": 3.0,
                "suggested_benchmark_high": 8.0,
                "comparable_set_summary": "No LLM configured — OECD default method "
                                          "selection used. Requires Tax Director benchmark study.",
                "confidence": "LOW",
                "narrative": f"Transaction type '{txn.get('transaction_type')}' has no "
                             f"documented TP policy. OECD guidance suggests {suggested_method} "
                             f"as the default method for this transaction category. "
                             f"A formal benchmarking study is required before this range "
                             f"can be used in transfer pricing documentation.",
            }

        # If an LLM client is wired in, this is where the prompt would be built
        # using `context` from RAG retrieval. Left as an extension point —
        # the prompt template and parsing logic depend on the chosen LLM client.
        try:
            prompt = self._build_advisory_prompt(txn, context)
            response = self.llm_client.generate(prompt)
            return self._parse_advisory_response(txn.get("txn_id"), response)
        except Exception as exc:
            logger.warning("LLM advisory generation failed for txn %s: %s", txn.get("txn_id"), exc)
            return None

    @staticmethod
    def _default_method_by_type(transaction_type: str) -> str:
        """OECD-aligned default method selection by transaction type (deterministic)."""
        mapping = {
            "TANGIBLE_GOODS": "CUP",
            "SERVICES": "CPM",
            "ROYALTY": "CUP",
            "FINANCING": "CUP",
            "MGMT_FEE": "TNMM",
        }
        return mapping.get(transaction_type, "TNMM")

    @staticmethod
    def _build_advisory_prompt(txn: dict, context: str) -> str:
        return (
            f"Suggest a transfer pricing method and arm's-length margin range "
            f"for this intercompany transaction, per OECD Transfer Pricing Guidelines:\n\n"
            f"Transaction type: {txn.get('transaction_type')}\n"
            f"Seller jurisdiction: {txn.get('seller_jurisdiction')}\n"
            f"Buyer jurisdiction: {txn.get('buyer_jurisdiction')}\n"
            f"Amount: {txn.get('amount')} {txn.get('currency')}\n"
            f"Description: {txn.get('description')}\n\n"
            f"Comparable context:\n{context}\n\n"
            f"Respond with: suggested method, benchmark range (low/high %), "
            f"and a 2-3 sentence narrative justification."
        )

    @staticmethod
    def _parse_advisory_response(txn_id: str, response: str) -> dict:
        """Placeholder parser — real implementation depends on LLM client response format."""
        return {
            "advisory_id": str(uuid.uuid4())[:12],
            "txn_id": txn_id,
            "suggested_method": "TNMM",
            "suggested_benchmark_low": 3.0,
            "suggested_benchmark_high": 8.0,
            "comparable_set_summary": "Parsed from LLM response",
            "confidence": "MEDIUM",
            "narrative": response[:500] if response else "",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Documentation tracking
    # ─────────────────────────────────────────────────────────────────────────

    def get_documentation_gaps(self, tenant_id: str, period: str) -> list[dict]:
        """
        Returns entities with incomplete TP documentation for the period —
        the thing Tax Directors actually get audited on.
        """
        docs = self.db.get_tp_documentation_status(tenant_id, period)
        gaps = []
        for d in docs:
            missing = []
            if not d.get("has_master_file"):
                missing.append("master file")
            if not d.get("has_local_file"):
                missing.append("local file")
            if not d.get("has_cbcr_inclusion"):
                missing.append("CbCR inclusion")
            if missing:
                gaps.append({
                    "entity_code": d["entity_code"],
                    "period": period,
                    "missing": missing,
                    "risk_rating": d.get("risk_rating", "UNKNOWN"),
                })
        return gaps
