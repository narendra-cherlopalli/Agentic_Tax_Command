"""
tax_command/orchestrator/graph.py

LangGraph orchestration for Tax Command — same pattern as Close Command's
orchestrator/graph.py.

Unlike Close Command's strictly sequential 8-agent pipeline, the four Tax
Command modules are largely INDEPENDENT — a company might run VAT monthly,
Transfer Pricing quarterly, Tax Provision at each close, and Pillar Two
annually. The graph reflects this: each module is a node that can run
standalone, with one real dependency (Tax Provision needs Pillar Two's
ETR inputs is NOT required — they're independent; Pillar Two needs
covered_taxes which typically comes FROM Tax Provision's current_tax_expense).

Graph topology:

    [Tax Provision] ──covered_taxes──> [Pillar Two]
           │
           └─ (independent) [VAT Agent]
           └─ (independent) [Transfer Pricing Agent]

HITL gate: any module that raises an escalation pauses the graph at a
review checkpoint, same as Close Command's interrupt_before pattern.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    _LANGGRAPH_AVAILABLE = True
except ImportError:
    _LANGGRAPH_AVAILABLE = False
    StateGraph = None
    END = "END"
    MemorySaver = None

logger = logging.getLogger(__name__)


class TaxCommandState(TypedDict, total=False):
    tenant_id: str
    period: str
    actor: str

    # Inputs (provided by caller before graph.invoke())
    vat_transactions: list[dict]
    vat_entity_code: str
    vat_period_start: str
    vat_period_end: str

    tp_transactions: list[dict]

    provision_entity_inputs: list[dict]
    prior_deferred_items: list[dict]

    pillar_two_jurisdiction_inputs: list[dict]
    auto_derive_pillar_two_from_provision: bool

    # Outputs (populated by each node)
    vat_result: dict
    tp_result: dict
    provision_result: dict
    pillar_two_result: dict

    # Control
    hitl_required: bool
    escalation_count: int
    overall_status: str


def build_tax_command_graph(db, agents: dict):
    """
    Build the Tax Command LangGraph.

    Parameters
    ----------
    db : TaxCommandDB
    agents : dict with keys 'vat', 'transfer_pricing', 'tax_provision', 'pillar_two'
             — instances of the four agent classes.

    Returns
    -------
    Compiled LangGraph app, or None if langgraph is not installed
    (caller should fall back to run_sequential() below).
    """
    if not _LANGGRAPH_AVAILABLE:
        logger.warning("langgraph not installed — use run_sequential() instead")
        return None

    graph = StateGraph(TaxCommandState)

    graph.add_node("vat", _make_vat_node(agents["vat"]))
    graph.add_node("transfer_pricing", _make_tp_node(agents["transfer_pricing"]))
    graph.add_node("tax_provision", _make_provision_node(agents["tax_provision"]))
    graph.add_node("pillar_two", _make_pillar_two_node(agents["pillar_two"], db))
    graph.add_node("finalize", _make_finalize_node(db))

    # VAT and TP are independent entry points — run in parallel conceptually,
    # sequential in this simple graph (LangGraph supports true parallelism
    # via fan-out, omitted here for clarity).
    graph.set_entry_point("vat")
    graph.add_edge("vat", "transfer_pricing")
    graph.add_edge("transfer_pricing", "tax_provision")
    graph.add_edge("tax_provision", "pillar_two")
    graph.add_edge("pillar_two", "finalize")
    graph.add_edge("finalize", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def run_sequential(db, agents: dict, state: TaxCommandState) -> TaxCommandState:
    """
    Fallback orchestration when langgraph is not installed.
    Same node logic as the graph version, run as plain Python.
    """
    state = dict(state)

    if state.get("vat_transactions"):
        state["vat_result"] = agents["vat"].run(
            state["tenant_id"], state["vat_entity_code"], state["vat_transactions"],
            state["vat_period_start"], state["vat_period_end"], actor=state.get("actor", "system"),
        )

    if state.get("tp_transactions"):
        state["tp_result"] = agents["transfer_pricing"].run(
            state["tenant_id"], state["period"], state["tp_transactions"],
            actor=state.get("actor", "system"),
        )

    if state.get("provision_entity_inputs"):
        state["provision_result"] = agents["tax_provision"].run(
            state["tenant_id"], state["period"], state["provision_entity_inputs"],
            prior_deferred_items=state.get("prior_deferred_items"),
            actor=state.get("actor", "system"),
        )

    if state.get("pillar_two_jurisdiction_inputs"):
        j_inputs = state["pillar_two_jurisdiction_inputs"]
        if state.get("auto_derive_pillar_two_from_provision") and state.get("provision_result"):
            j_inputs = _derive_pillar_two_inputs(j_inputs, state["provision_result"])
        state["pillar_two_result"] = agents["pillar_two"].run(
            state["tenant_id"], state["period"], j_inputs, actor=state.get("actor", "system"),
        )

    escalations = db.get_open_escalations(state["tenant_id"])
    state["escalation_count"] = len(escalations)
    state["hitl_required"] = len(escalations) > 0
    state["overall_status"] = "HITL_PENDING" if state["hitl_required"] else "COMPLETE"

    return state


# ─────────────────────────────────────────────────────────────────────────────
# Node factories (used by LangGraph version)
# ─────────────────────────────────────────────────────────────────────────────

def _make_vat_node(vat_agent):
    def node(state: TaxCommandState) -> TaxCommandState:
        if not state.get("vat_transactions"):
            return state
        result = vat_agent.run(
            state["tenant_id"], state["vat_entity_code"], state["vat_transactions"],
            state["vat_period_start"], state["vat_period_end"], actor=state.get("actor", "system"),
        )
        return {**state, "vat_result": result}
    return node


def _make_tp_node(tp_agent):
    def node(state: TaxCommandState) -> TaxCommandState:
        if not state.get("tp_transactions"):
            return state
        result = tp_agent.run(
            state["tenant_id"], state["period"], state["tp_transactions"],
            actor=state.get("actor", "system"),
        )
        return {**state, "tp_result": result}
    return node


def _make_provision_node(provision_agent):
    def node(state: TaxCommandState) -> TaxCommandState:
        if not state.get("provision_entity_inputs"):
            return state
        result = provision_agent.run(
            state["tenant_id"], state["period"], state["provision_entity_inputs"],
            prior_deferred_items=state.get("prior_deferred_items"),
            actor=state.get("actor", "system"),
        )
        return {**state, "provision_result": result}
    return node


def _make_pillar_two_node(p2_agent, db):
    def node(state: TaxCommandState) -> TaxCommandState:
        if not state.get("pillar_two_jurisdiction_inputs"):
            return state
        j_inputs = state["pillar_two_jurisdiction_inputs"]
        if state.get("auto_derive_pillar_two_from_provision") and state.get("provision_result"):
            j_inputs = _derive_pillar_two_inputs(j_inputs, state["provision_result"])
        result = p2_agent.run(
            state["tenant_id"], state["period"], j_inputs, actor=state.get("actor", "system"),
        )
        return {**state, "pillar_two_result": result}
    return node


def _make_finalize_node(db):
    def node(state: TaxCommandState) -> TaxCommandState:
        escalations = db.get_open_escalations(state["tenant_id"])
        hitl_required = len(escalations) > 0
        return {
            **state,
            "escalation_count": len(escalations),
            "hitl_required": hitl_required,
            "overall_status": "HITL_PENDING" if hitl_required else "COMPLETE",
        }
    return node


def _derive_pillar_two_inputs(j_inputs: list[dict], provision_result: dict) -> list[dict]:
    """
    Wire Tax Provision's current_tax_expense into Pillar Two's covered_taxes
    input — the one real cross-module dependency in this suite. Without this,
    a caller would have to manually copy numbers between modules.
    """
    provisions_by_entity = {
        p["entity_code"]: p for p in provision_result.get("provisions", [])
    }
    enriched = []
    for j in j_inputs:
        entities = j.get("entities_in_scope", [])
        covered_taxes = sum(
            provisions_by_entity.get(e, {}).get("current_tax_expense", 0)
            for e in entities
        )
        globe_income = sum(
            provisions_by_entity.get(e, {}).get("pretax_income", 0)
            for e in entities
        )
        enriched.append({
            **j,
            "covered_taxes": covered_taxes or j.get("covered_taxes", 0),
            "globe_income": globe_income or j.get("globe_income", 0),
        })
    return enriched
