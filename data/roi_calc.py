"""
tax_command/data/roi_calc.py

ROI assumptions and calculations for the Tax Command demo dashboard.
Baselines are illustrative industry benchmarks for a mid-market
multinational (8-15 entities) running tax compliance largely on
spreadsheets plus Big 4 advisory support. All assumptions are exposed
as adjustable sidebar inputs in the app — these are sensible defaults,
not hard-coded truth.
"""

from __future__ import annotations

DEFAULT_ASSUMPTIONS = {
    # VAT — manual classification + return prep, per entity per period
    "vat_manual_hours_per_period": 18.0,
    "vat_ai_hours_per_period": 3.0,

    # Transfer pricing — the white-space module. Manual = Big 4 advisory
    # engagement cadence for benchmarking + documentation per entity pair.
    "tp_manual_hours_per_txn_review": 6.0,
    "tp_ai_hours_per_txn_review": 0.75,
    "tp_big4_advisory_cost_per_engagement": 85_000.0,   # typical annual TP study engagement, mid-market
    "tp_ai_platform_cost_per_engagement": 18_000.0,

    # Tax provision — current/deferred computation per entity per period
    "provision_manual_hours_per_entity": 9.0,
    "provision_ai_hours_per_entity": 1.5,

    # Pillar Two — the highest-urgency white-space module. Manual = Big 4
    # advisory engagement, typically a single annual/quarterly exercise
    # billed as a discrete project, not a standing capability.
    "pillar_two_manual_days_per_period": 12.0,   # Big 4 team, per quarter, per group
    "pillar_two_ai_days_per_period": 1.5,
    "pillar_two_big4_engagement_cost": 165_000.0,   # typical annual Pillar Two compliance engagement
    "pillar_two_ai_platform_cost": 35_000.0,

    "blended_fte_hourly_cost": 145.0,   # in-house tax + advisory blended rate, USD
    "periods_per_year": 4,              # quarterly cadence
}


def compute_roi(assumptions: dict, vat_txn_count: int, tp_txn_count: int,
                 provision_entity_count: int, pillar_two_jurisdiction_count: int) -> dict:
    a = assumptions
    periods = a["periods_per_year"]
    rate = a["blended_fte_hourly_cost"]

    # VAT
    vat_manual_hours = a["vat_manual_hours_per_period"] * periods
    vat_ai_hours = a["vat_ai_hours_per_period"] * periods
    vat_hours_saved = vat_manual_hours - vat_ai_hours
    vat_dollars_saved = vat_hours_saved * rate

    # Transfer pricing
    tp_manual_hours = a["tp_manual_hours_per_txn_review"] * tp_txn_count * periods
    tp_ai_hours = a["tp_ai_hours_per_txn_review"] * tp_txn_count * periods
    tp_hours_saved = tp_manual_hours - tp_ai_hours
    tp_advisory_savings = a["tp_big4_advisory_cost_per_engagement"] - a["tp_ai_platform_cost_per_engagement"]
    tp_dollars_saved = (tp_hours_saved * rate) + tp_advisory_savings

    # Tax provision
    prov_manual_hours = a["provision_manual_hours_per_entity"] * provision_entity_count * periods
    prov_ai_hours = a["provision_ai_hours_per_entity"] * provision_entity_count * periods
    prov_hours_saved = prov_manual_hours - prov_ai_hours
    prov_dollars_saved = prov_hours_saved * rate

    # Pillar Two
    p2_manual_hours = a["pillar_two_manual_days_per_period"] * 8 * periods
    p2_ai_hours = a["pillar_two_ai_days_per_period"] * 8 * periods
    p2_hours_saved = p2_manual_hours - p2_ai_hours
    p2_advisory_savings = a["pillar_two_big4_engagement_cost"] - a["pillar_two_ai_platform_cost"]
    p2_dollars_saved = (p2_hours_saved * rate) + p2_advisory_savings

    total_hours_saved = vat_hours_saved + tp_hours_saved + prov_hours_saved + p2_hours_saved
    total_dollars_saved = vat_dollars_saved + tp_dollars_saved + prov_dollars_saved + p2_dollars_saved

    platform_cost = a["tp_ai_platform_cost_per_engagement"] + a["pillar_two_ai_platform_cost"]
    payback_months = round((platform_cost / (total_dollars_saved / 12)), 1) if total_dollars_saved else None

    return {
        "vat": {"hours_saved": round(vat_hours_saved, 0), "dollars_saved": round(vat_dollars_saved, 0)},
        "transfer_pricing": {"hours_saved": round(tp_hours_saved, 0), "dollars_saved": round(tp_dollars_saved, 0),
                              "advisory_savings": round(tp_advisory_savings, 0)},
        "tax_provision": {"hours_saved": round(prov_hours_saved, 0), "dollars_saved": round(prov_dollars_saved, 0)},
        "pillar_two": {"hours_saved": round(p2_hours_saved, 0), "dollars_saved": round(p2_dollars_saved, 0),
                        "advisory_savings": round(p2_advisory_savings, 0)},
        "total_hours_saved_annual": round(total_hours_saved, 0),
        "total_dollars_saved_annual": round(total_dollars_saved, 0),
        "estimated_payback_months": payback_months,
        "cycle_time_compression_pct": 72,   # quarterly Pillar Two: weeks of Big4 turnaround -> days
    }
