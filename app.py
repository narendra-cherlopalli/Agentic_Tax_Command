"""
tax_command/app.py

Tax Command — agentic AI for the multinational tax function.

Demonstrates all four product areas from the white-space analysis:
  1. Indirect Tax / VAT          — mature category, AI as automation layer
  2. Transfer Pricing            — white space: arm's-length testing + LLM advisory
  3. Tax Provision               — emerging category, current/deferred automation
  4. Pillar Two / BEPS           — white space: ETR monitoring + top-up tax + QDMTT

This is a live wiring of the real agent classes in agents/ — not a
mock. Run() calls execute the actual deterministic tax logic and
persist to a real SQLite-backed TaxCommandDB. The "AI" in this demo is
the agentic orchestration + advisory/forecast layer (TPAdvisory,
ETRForecast) layered on top of deterministic tax mechanics — exactly
the governance pattern the underlying engine enforces.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tax_command.agents.pillar_two_agent import PillarTwoAgent, GLOBE_MINIMUM_RATE_PCT
from tax_command.agents.tax_provision_agent import TaxProvisionAgent
from tax_command.agents.transfer_pricing_agent import TransferPricingAgent
from tax_command.agents.vat_agent import VATAgent
from tax_command.data import sample_data as sd
from tax_command.data.roi_calc import DEFAULT_ASSUMPTIONS, compute_roi
from tax_command.database.persistence import TaxCommandDB

TENANT_ID = "meridian-demo"
TENANT_NAME = "Meridian Industrial Holdings plc"

st.set_page_config(layout="wide", page_title="Tax Command — Agentic AI for Multinational Tax")


# ─────────────────────────────────────────────────────────────────────────────
# Engine bootstrap — real DB, real agents, cached for the session
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def get_engine(seed: int):
    db_path = os.path.join(tempfile.gettempdir(), f"tax_command_demo_{seed}.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = TaxCommandDB(db_path=db_path)
    db.create_tenant(TENANT_ID, TENANT_NAME, plan_tier="ENTERPRISE")
    for e in sd.ENTITIES:
        db.upsert_entity(TENANT_ID, e, updated_by="system_seed")

    vat_agent = VATAgent(db)
    tp_agent = TransferPricingAgent(db)
    provision_agent = TaxProvisionAgent(db)
    pillar_two_agent = PillarTwoAgent(db)

    return {
        "db": db,
        "vat_agent": vat_agent,
        "tp_agent": tp_agent,
        "provision_agent": provision_agent,
        "pillar_two_agent": pillar_two_agent,
    }


@st.cache_data(show_spinner=False)
def run_pipeline(seed: int):
    """Executes all four agents end-to-end against generated sample data."""
    engine = get_engine(seed)
    db = engine["db"]

    # 1. VAT
    vat_txns = sd.generate_vat_transactions(seed=seed)
    vat_result = engine["vat_agent"].run(
        TENANT_ID, "MERIDIAN_DE", vat_txns, sd.PERIOD_START, sd.PERIOD_END
    )

    # 2. Transfer pricing
    tp_txns = sd.generate_tp_transactions(seed=seed)
    tp_result = engine["tp_agent"].run(TENANT_ID, sd.PERIOD, tp_txns)
    tp_doc_status = sd.generate_tp_documentation_status(seed=seed)
    for d in tp_doc_status:
        db.conn.execute("""
            INSERT OR REPLACE INTO tp_documentation
                (tenant_id, entity_code, period, has_master_file, has_local_file,
                 has_cbcr_inclusion, risk_rating)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (TENANT_ID, d["entity_code"], sd.PERIOD, d["has_master_file"],
              d["has_local_file"], d["has_cbcr_inclusion"], d["risk_rating"]))
    db.conn.commit()
    doc_gaps = engine["tp_agent"].get_documentation_gaps(TENANT_ID, sd.PERIOD)

    # 3. Tax provision
    provision_inputs = sd.generate_provision_inputs(seed=seed)
    prior_deferred = sd.generate_prior_deferred_items(seed=seed)
    provision_result = engine["provision_agent"].run(
        TENANT_ID, sd.PERIOD, provision_inputs, prior_deferred_items=prior_deferred
    )

    # 4. Pillar Two
    p2_inputs = sd.generate_pillar_two_inputs(seed=seed)
    p2_result = engine["pillar_two_agent"].run(TENANT_ID, sd.PERIOD, p2_inputs)

    forecasts = []
    for etr in p2_result["etr_calculations"]:
        hist = sd.generate_historical_etrs(etr["jurisdiction"], etr["jurisdictional_etr_pct"], seed=seed)
        forecast = engine["pillar_two_agent"].forecast_etr(
            TENANT_ID, etr["jurisdiction"], hist, "2026-Q2"
        )
        forecasts.append(forecast)

    qdmtt_filings = []
    for j in p2_result["breach_jurisdictions"]:
        filing = engine["pillar_two_agent"].generate_qdmtt_filing(
            TENANT_ID, j, sd.PERIOD, ultimate_parent="MERIDIAN_UK", due_date="2026-12-31"
        )
        if filing["generated"]:
            qdmtt_filings.append(filing["filing"])

    escalations = db.get_open_escalations(TENANT_ID)
    audit_log = db.get_audit_log(TENANT_ID, limit=100)

    return {
        "vat": vat_result,
        "transfer_pricing": tp_result,
        "tp_doc_gaps": doc_gaps,
        "tp_doc_status": tp_doc_status,
        "provision": provision_result,
        "pillar_two": p2_result,
        "etr_forecasts": forecasts,
        "qdmtt_filings": qdmtt_filings,
        "escalations": escalations,
        "audit_log": audit_log,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — global controls + ROI assumptions
# ─────────────────────────────────────────────────────────────────────────────

st.sidebar.title("Tax Command")
st.sidebar.caption("Agentic AI for the multinational tax function")
seed = st.sidebar.number_input("Sample data seed", min_value=1, max_value=9999, value=42, step=1)

engine = get_engine(seed)
results = run_pipeline(seed)

st.sidebar.markdown("---")
st.sidebar.subheader("Group snapshot")
st.sidebar.metric("Entities", len(sd.ENTITIES))
st.sidebar.metric("Jurisdictions", len({e["jurisdiction"] for e in sd.ENTITIES}))
st.sidebar.metric("Period", sd.PERIOD)

st.sidebar.markdown("---")
st.sidebar.subheader("ROI assumptions")
st.sidebar.caption("Adjust to match your own cost base")
assumptions = dict(DEFAULT_ASSUMPTIONS)
assumptions["blended_fte_hourly_cost"] = st.sidebar.slider(
    "Blended hourly cost ($)", 60.0, 350.0, DEFAULT_ASSUMPTIONS["blended_fte_hourly_cost"], 5.0
)
assumptions["pillar_two_big4_engagement_cost"] = st.sidebar.slider(
    "Big 4 Pillar Two engagement ($/yr)", 40_000.0, 400_000.0,
    DEFAULT_ASSUMPTIONS["pillar_two_big4_engagement_cost"], 5_000.0
)
assumptions["tp_big4_advisory_cost_per_engagement"] = st.sidebar.slider(
    "Big 4 TP advisory engagement ($/yr)", 20_000.0, 300_000.0,
    DEFAULT_ASSUMPTIONS["tp_big4_advisory_cost_per_engagement"], 5_000.0
)

roi = compute_roi(
    assumptions,
    vat_txn_count=results["vat"]["vat_return"]["transaction_count"],
    tp_txn_count=len(results["transfer_pricing"]["tested_transactions"]),
    provision_entity_count=results["provision"]["group_summary"]["entity_count"],
    pillar_two_jurisdiction_count=len(results["pillar_two"]["etr_calculations"]),
)


# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────

st.title("Tax Command")
st.caption(
    "Four-agent tax compliance suite for multinational groups — VAT, transfer pricing, "
    "tax provision, and Pillar Two / BEPS, running on the same agentic pipeline. "
    f"Live demo group: **{TENANT_NAME}**, {len(sd.ENTITIES)} entities, "
    f"{len({e['jurisdiction'] for e in sd.ENTITIES})} jurisdictions."
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Open escalations", len(results["escalations"]))
c2.metric("Pillar Two breach jurisdictions", len(results["pillar_two"]["breach_jurisdictions"]))
c3.metric("TP transactions outside arm's-length range", results["transfer_pricing"]["non_arms_length_count"])
c4.metric("Group ETR (tax provision)", f"{results['provision']['group_summary']['group_etr_pct']:.1f}%")

st.markdown("---")

tabs = st.tabs([
    "🌍 Pillar Two / BEPS",
    "🔁 Transfer Pricing",
    "🧾 Tax Provision",
    "📋 VAT / Indirect Tax",
    "💰 ROI Impact",
    "🗂️ Audit Trail",
])

# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Pillar Two / BEPS (the headline white-space module)
# ─────────────────────────────────────────────────────────────────────────────

with tabs[0]:
    st.subheader("Pillar Two / BEPS — jurisdictional ETR monitoring")
    st.markdown(
        "Every multinational group with consolidated revenue ≥ €750M has needed OECD GloBE "
        "compliance since fiscal years starting 2024. Today this is **almost entirely Big 4 "
        "advisory work** — spreadsheets, consultants, $500K+ engagements, run as a quarterly "
        "fire-drill. This agent computes jurisdictional ETR and top-up tax deterministically "
        "from covered taxes and GloBE income, then forecasts the ETR trajectory **before** "
        "period-end so Tax has months — not days — to act."
    )

    p2 = results["pillar_two"]
    etr_df = pd.DataFrame(p2["etr_calculations"])
    etr_df["entities_in_scope"] = etr_df["entities_in_scope"].apply(lambda x: ", ".join(x))

    colA, colB = st.columns([2, 1])
    with colA:
        fig = go.Figure()
        colors = ["#d62728" if v < GLOBE_MINIMUM_RATE_PCT else "#2c6e49" for v in etr_df["jurisdictional_etr_pct"]]
        fig.add_trace(go.Bar(
            x=etr_df["jurisdiction"], y=etr_df["jurisdictional_etr_pct"],
            marker_color=colors, name="Jurisdictional ETR",
            text=[f"{v:.1f}%" for v in etr_df["jurisdictional_etr_pct"]], textposition="outside",
        ))
        fig.add_hline(y=GLOBE_MINIMUM_RATE_PCT, line_dash="dash", line_color="#444",
                       annotation_text="15% GloBE minimum", annotation_position="top left")
        fig.update_layout(title="Jurisdictional ETR vs 15% GloBE minimum — " + sd.PERIOD,
                           yaxis_title="ETR %", height=420, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with colB:
        st.metric("Total top-up tax exposure", f"${p2['total_top_up_tax_usd']:,.0f}")
        st.metric("Breach jurisdictions", len(p2["breach_jurisdictions"]))
        if p2["breach_jurisdictions"]:
            st.error(f"Top-up tax required: {', '.join(p2['breach_jurisdictions'])}")
        else:
            st.success("No jurisdictions currently breach the GloBE minimum.")

    st.markdown("##### Jurisdictional ETR detail")
    display_df = etr_df[[
        "jurisdiction", "entities_in_scope", "globe_income", "covered_taxes",
        "jurisdictional_etr_pct", "top_up_tax_required", "top_up_tax_amount", "safe_harbour_applied"
    ]].rename(columns={
        "globe_income": "GloBE income ($)", "covered_taxes": "Covered taxes ($)",
        "jurisdictional_etr_pct": "ETR (%)", "top_up_tax_required": "Top-up required",
        "top_up_tax_amount": "Top-up tax ($)", "safe_harbour_applied": "Safe harbour",
        "entities_in_scope": "Entities in scope", "jurisdiction": "Jurisdiction",
    })
    display_df["Top-up required"] = display_df["Top-up required"].map({1: "Yes", 0: "No"})
    st.dataframe(
        display_df.style.format({
            "GloBE income ($)": "{:,.0f}", "Covered taxes ($)": "{:,.0f}",
            "ETR (%)": "{:.2f}", "Top-up tax ($)": "{:,.0f}",
        }),
        use_container_width=True, hide_index=True,
    )

    st.markdown("##### ETR early-warning forecast — next period (2026-Q2)")
    st.caption(
        "This is the single highest-leverage capability in the suite: a forward forecast of "
        "next period's ETR trajectory, flagging jurisdictions trending toward breach **before** "
        "the position crystallises — turning a quarterly surprise into months of planning runway."
    )
    fc_df = pd.DataFrame(results["etr_forecasts"])
    risk_color = {"SAFE": "🟢", "WATCH": "🟡", "BREACH_LIKELY": "🔴"}
    for _, row in fc_df.iterrows():
        with st.expander(f"{risk_color.get(row['risk_flag'], '')} {row['jurisdiction']} — "
                          f"forecast {row['forecasted_etr_pct']:.1f}% ({row['risk_flag']})"):
            st.write(row["narrative"])
            st.caption(f"Confidence: {row['confidence']}")

    st.markdown("##### QDMTT filings generated (HITL approval required before FILED)")
    if results["qdmtt_filings"]:
        qf_df = pd.DataFrame(results["qdmtt_filings"])
        qf_df["allocated_entities"] = qf_df["allocated_entities"].apply(lambda x: ", ".join(x))
        st.dataframe(
            qf_df[["filing_id", "jurisdiction", "ultimate_parent_entity", "top_up_tax_amount",
                   "allocated_entities", "status", "due_date"]],
            use_container_width=True, hide_index=True,
        )
        st.info("Status DRAFT — a Tax Director must explicitly approve before this moves to FILED. "
                "The agent never auto-files.")
    else:
        st.write("No QDMTT filings required this period.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Transfer Pricing (second white-space module)
# ─────────────────────────────────────────────────────────────────────────────

with tabs[1]:
    st.subheader("Transfer Pricing — arm's-length testing + LLM advisory")
    st.markdown(
        "The highest white-space module in the suite. Almost no funded agentic product tests "
        "intercompany pricing today — it's entirely Big 4 advisory work at **$500K+ per "
        "engagement** for a mid-market multinational. The deterministic test below runs against "
        "policy-set benchmark ranges; the LLM advisory layer only activates for transaction types "
        "with **no existing benchmark**, and is never auto-applied to a filing position."
    )

    tp = results["transfer_pricing"]
    tp_df = pd.DataFrame(tp["tested_transactions"])

    c1, c2, c3 = st.columns(3)
    c1.metric("Transactions tested", len(tp_df))
    c2.metric("Outside arm's-length range", tp["non_arms_length_count"])
    c3.metric("Advisories generated (unbenchmarked)", tp["advisories_generated"])

    def _flag(row):
        if row["is_arms_length"] is None:
            return "🟡 Needs advisory"
        return "🟢 Within range" if row["is_arms_length"] else "🔴 Outside range"

    tp_df["status"] = tp_df.apply(_flag, axis=1)
    display_tp = tp_df[[
        "txn_id", "seller_entity", "buyer_entity", "transaction_type", "amount",
        "tp_method", "benchmark_range_low", "benchmark_range_high", "actual_margin_pct", "status"
    ]].rename(columns={
        "txn_id": "Txn", "seller_entity": "Seller", "buyer_entity": "Buyer",
        "transaction_type": "Type", "amount": "Amount ($)", "tp_method": "Method",
        "benchmark_range_low": "Range low (%)", "benchmark_range_high": "Range high (%)",
        "actual_margin_pct": "Actual margin (%)", "status": "Status",
    })
    st.dataframe(
        display_tp.style.format({"Amount ($)": "{:,.0f}", "Range low (%)": "{:.1f}",
                                  "Range high (%)": "{:.1f}", "Actual margin (%)": "{:.1f}"}),
        use_container_width=True, hide_index=True,
    )

    needs_advisory = [t for t in tp["tested_transactions"] if t["tp_method"] is None]
    if needs_advisory:
        st.markdown("##### LLM advisory for unbenchmarked transactions")
        st.caption("Advisory only — a Tax Director must explicitly promote this into TP policy "
                    "before it affects any filing position.")
        for txn in needs_advisory:
            advisory = engine["tp_agent"]._generate_advisory(TENANT_ID, txn)
            if advisory:
                with st.expander(f"{txn['txn_id']} — {txn['description']}"):
                    a1, a2 = st.columns(2)
                    a1.metric("Suggested method", advisory["suggested_method"])
                    a2.metric("Suggested range", f"{advisory['suggested_benchmark_low']:.1f}% – "
                                                  f"{advisory['suggested_benchmark_high']:.1f}%")
                    st.write(advisory["narrative"])
                    st.caption(f"Confidence: {advisory['confidence']}")

    st.markdown("##### Documentation completeness — what Tax Directors actually get audited on")
    gaps = results["tp_doc_gaps"]
    if gaps:
        gap_df = pd.DataFrame(gaps)
        gap_df["missing"] = gap_df["missing"].apply(lambda x: ", ".join(x))
        st.dataframe(gap_df.rename(columns={
            "entity_code": "Entity", "missing": "Missing documentation", "risk_rating": "Risk rating"
        }), use_container_width=True, hide_index=True)
    else:
        st.success("All entities have complete TP documentation for this period.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — Tax provision
# ─────────────────────────────────────────────────────────────────────────────

with tabs[2]:
    st.subheader("Tax Provision — current/deferred tax automation")
    st.markdown(
        "Pulls accounting data, applies tax rules, computes current and deferred tax per entity "
        "per period — wired into the same agentic pipeline as the close process, so the provision "
        "is computed from the actual consolidated numbers, not a separate manual data pull."
    )
    prov = results["provision"]
    gs = prov["group_summary"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Group pretax income", f"${gs['total_pretax_income']:,.0f}")
    c2.metric("Group total tax expense", f"${gs['total_tax_expense']:,.0f}")
    c3.metric("Group ETR", f"{gs['group_etr_pct']:.1f}%")
    c4.metric("Flagged DT movements", len(prov["flagged_entities"]))

    prov_df = pd.DataFrame(prov["provisions"])
    fig2 = px.bar(prov_df, x="entity_code", y=["current_tax_expense", "deferred_tax_expense"],
                   title="Current vs deferred tax expense by entity", barmode="stack",
                   labels={"value": "Tax expense ($)", "entity_code": "Entity", "variable": "Component"})
    st.plotly_chart(fig2, use_container_width=True)

    st.dataframe(
        prov_df[["entity_code", "jurisdiction", "pretax_income", "statutory_rate_pct",
                 "current_tax_expense", "deferred_tax_expense", "total_tax_expense",
                 "effective_tax_rate_pct"]].rename(columns={
            "entity_code": "Entity", "jurisdiction": "Jurisdiction", "pretax_income": "Pretax income ($)",
            "statutory_rate_pct": "Statutory rate (%)", "current_tax_expense": "Current tax ($)",
            "deferred_tax_expense": "Deferred tax ($)", "total_tax_expense": "Total tax ($)",
            "effective_tax_rate_pct": "ETR (%)",
        }).style.format({
            "Pretax income ($)": "{:,.0f}", "Current tax ($)": "{:,.0f}",
            "Deferred tax ($)": "{:,.0f}", "Total tax ($)": "{:,.0f}",
        }),
        use_container_width=True, hide_index=True,
    )
    if prov["flagged_entities"]:
        st.warning(f"Unusual deferred tax movement flagged for review: {', '.join(prov['flagged_entities'])}")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 — VAT
# ─────────────────────────────────────────────────────────────────────────────

with tabs[3]:
    st.subheader("Indirect Tax / VAT — the mature category")
    st.markdown(
        "ML classifies transactions, applies jurisdiction rules, and produces the periodic "
        "return. This is the most mature category in the market (Vertex, Avalara, TaxJar already "
        "do this well) — Tax Command's differentiation is running it on the same agentic pipeline "
        "as the white-space modules, not novel VAT logic."
    )
    vat = results["vat"]
    vr = vat["vat_return"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transactions classified", vr["transaction_count"])
    c2.metric("Net VAT payable", f"€{vr['net_vat_payable']:,.0f}")
    c3.metric("Flagged for manual review", vr["flagged_count"])
    c4.metric("Auto-classification rate",
              f"{(1 - vr['flagged_count'] / max(vr['transaction_count'], 1)) * 100:.0f}%")

    vat_df = pd.DataFrame(vat["classified_transactions"])
    fig3 = px.pie(vat_df, names="vat_treatment", title="VAT treatment distribution")
    st.plotly_chart(fig3, use_container_width=True)

    st.markdown("##### Transactions requiring manual review")
    review_df = vat_df[vat_df["requires_review"] == 1][
        ["txn_id", "counterparty", "amount_net", "description", "classification_source",
         "classification_confidence"]
    ]
    if len(review_df):
        st.dataframe(review_df, use_container_width=True, hide_index=True)
    else:
        st.success("No transactions require manual review this period.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 5 — ROI
# ─────────────────────────────────────────────────────────────────────────────

with tabs[4]:
    st.subheader("ROI impact — Tax Command vs manual / Big 4 advisory baseline")
    st.markdown(
        "Pillar Two and transfer pricing are the two modules with **near-zero product "
        "competition** today — they're advisory engagements, not software. That's also where "
        "the largest dollar savings sit, because the baseline isn't an internal FTE's time, "
        "it's a discrete six-figure Big 4 engagement run on a fixed cadence regardless of "
        "actual complexity."
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Total annual savings", f"${roi['total_dollars_saved_annual']:,.0f}")
    c2.metric("Total hours saved / year", f"{roi['total_hours_saved_annual']:,.0f}")
    c3.metric("Estimated payback", f"{roi['estimated_payback_months']} months" if roi['estimated_payback_months'] else "—")

    roi_rows = [
        {"Module": "Pillar Two / BEPS", "Hours saved": roi["pillar_two"]["hours_saved"],
         "Dollars saved": roi["pillar_two"]["dollars_saved"],
         "Advisory fee displacement": roi["pillar_two"]["advisory_savings"]},
        {"Module": "Transfer Pricing", "Hours saved": roi["transfer_pricing"]["hours_saved"],
         "Dollars saved": roi["transfer_pricing"]["dollars_saved"],
         "Advisory fee displacement": roi["transfer_pricing"]["advisory_savings"]},
        {"Module": "Tax Provision", "Hours saved": roi["tax_provision"]["hours_saved"],
         "Dollars saved": roi["tax_provision"]["dollars_saved"], "Advisory fee displacement": 0},
        {"Module": "VAT / Indirect Tax", "Hours saved": roi["vat"]["hours_saved"],
         "Dollars saved": roi["vat"]["dollars_saved"], "Advisory fee displacement": 0},
    ]
    roi_df = pd.DataFrame(roi_rows)

    colL, colR = st.columns([3, 2])
    with colL:
        fig4 = px.bar(roi_df, x="Module", y="Dollars saved", color="Module",
                       title="Annual dollar savings by module",
                       text=roi_df["Dollars saved"].apply(lambda v: f"${v:,.0f}"))
        fig4.update_traces(textposition="outside")
        fig4.update_layout(showlegend=False, height=420)
        st.plotly_chart(fig4, use_container_width=True)
    with colR:
        st.dataframe(
            roi_df.style.format({"Hours saved": "{:,.0f}", "Dollars saved": "{:,.0f}",
                                  "Advisory fee displacement": "{:,.0f}"}),
            use_container_width=True, hide_index=True,
        )
        st.metric("Cycle time compression (Pillar Two)", f"{roi['cycle_time_compression_pct']}%",
                   help="Weeks of Big 4 turnaround compressed to days of agentic monitoring.")

    csv = roi_df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download ROI summary (CSV)", csv, "tax_command_roi_summary.csv", "text/csv")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 6 — Audit trail
# ─────────────────────────────────────────────────────────────────────────────

with tabs[5]:
    st.subheader("Audit trail & open escalations")
    st.markdown(
        "Every agent run is logged. Escalations are raised whenever an agent's output requires "
        "human judgement beyond its mandate — top-up tax required, TP transactions outside "
        "benchmark, unusual deferred tax movements, or VAT treatments the rule engine couldn't "
        "classify with confidence. Nothing here auto-resolves."
    )

    esc_df = pd.DataFrame(results["escalations"])
    if len(esc_df):
        st.dataframe(
            esc_df[["escalation_id", "agent_name", "entity_code", "escalation_type",
                    "description", "amount_usd", "resolved"]].rename(columns={
                "escalation_id": "ID", "agent_name": "Agent", "entity_code": "Entity",
                "escalation_type": "Type", "description": "Description", "amount_usd": "Amount ($)",
                "resolved": "Resolved",
            }),
            use_container_width=True, hide_index=True,
        )
    else:
        st.success("No open escalations.")

    st.markdown("##### Agent audit log")
    audit_df = pd.DataFrame(results["audit_log"])
    if len(audit_df):
        cols = [c for c in ["created_at", "event_type", "actor", "agent_name", "period"] if c in audit_df.columns]
        st.dataframe(audit_df[cols].rename(columns={
            "created_at": "Timestamp", "event_type": "Event", "actor": "Actor",
            "agent_name": "Agent", "period": "Period",
        }), use_container_width=True, hide_index=True, height=320)

st.markdown("---")
st.caption(
    "Tax Command POC — agents execute real deterministic tax logic from `agents/`. "
    "Sample data is illustrative and deterministic (seed-based). Not a substitute for "
    "professional tax advice."
)
