"""
tax_command/test_smoke.py

Run this after installation to verify Tax Command is wired correctly:

    python -m tax_command.test_smoke

Tests all 4 agents end-to-end against a real (temporary) SQLite database,
including the cross-module Pillar Two <- Tax Provision derivation, the
hash-chained audit trail, and tenant isolation. This is not a unit test
suite — it is a single integration smoke test you run once after setup
to confirm the package is correctly installed before connecting real data.
"""

from __future__ import annotations

import os
import sys
import tempfile


def main() -> None:
    from tax_command.database.persistence import TaxCommandDB
    from tax_command.agents.vat_agent import VATAgent
    from tax_command.agents.transfer_pricing_agent import TransferPricingAgent
    from tax_command.agents.tax_provision_agent import TaxProvisionAgent
    from tax_command.agents.pillar_two_agent import PillarTwoAgent
    from tax_command.orchestrator.graph import run_sequential

    db_path = os.path.join(tempfile.gettempdir(), "tax_command_smoke_test.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    print(f"Using temporary database: {db_path}\n")

    db = TaxCommandDB(db_path=db_path)
    db.create_tenant("smoke_test", "Smoke Test Tenant")

    agents = {
        "vat": VATAgent(db),
        "transfer_pricing": TransferPricingAgent(db),
        "tax_provision": TaxProvisionAgent(db),
        "pillar_two": PillarTwoAgent(db),
    }

    print("[1/5] Testing tenant isolation...")
    db.create_tenant("other_tenant", "Other Tenant")
    db.upsert_entity("smoke_test", {
        "entity_code": "TEST-UK", "entity_name": "Test UK Ltd", "jurisdiction": "GB",
        "is_ultimate_parent": 0, "parent_entity": None, "statutory_rate_pct": 25.0,
        "vat_number": "GB123456789", "is_active": 1,
    })
    assert len(db.get_entities("smoke_test")) == 1
    assert len(db.get_entities("other_tenant")) == 0
    print("    ✓ Tenant isolation verified\n")

    print("[2/5] Testing VAT agent...")
    vat_result = agents["vat"].run(
        "smoke_test", "TEST-UK",
        [{"txn_id": "T1", "jurisdiction": "GB", "transaction_date": "2024-12-01",
          "counterparty": "Test Co", "counterparty_vat_number": "GB999",
          "amount_net": 10000.0, "currency": "GBP", "description": "Test sale",
          "account_code": "40100"}],
        period_start="2024-12-01", period_end="2024-12-31", actor="smoke_test",
    )
    assert vat_result["vat_return"]["output_vat"] == 2000.0
    print(f"    ✓ VAT classified correctly: output_vat = {vat_result['vat_return']['output_vat']}\n")

    print("[3/5] Testing Transfer Pricing agent...")
    tp_result = agents["transfer_pricing"].run(
        "smoke_test", "2024-12",
        [{"txn_id": "TP1", "seller_entity": "HQ", "buyer_entity": "TEST-UK",
          "seller_jurisdiction": "DE", "buyer_jurisdiction": "GB",
          "transaction_type": "MGMT_FEE", "amount": 100000.0, "currency": "EUR",
          "description": "Test fee", "tp_method": "TNMM",
          "benchmark_range_low": 3.0, "benchmark_range_high": 7.0,
          "actual_margin_pct": 15.0}],  # OUTSIDE range — should flag
        actor="smoke_test",
    )
    assert tp_result["non_arms_length_count"] == 1
    print(f"    ✓ Out-of-range TP transaction correctly flagged\n")

    print("[4/5] Testing Tax Provision agent...")
    provision_result = agents["tax_provision"].run(
        "smoke_test", "2024-12",
        [{"entity_code": "TEST-UK", "period": "2024-12", "jurisdiction": "GB",
          "pretax_income": 1000000.0, "statutory_rate_pct": 25.0,
          "permanent_differences": 0.0, "temporary_differences": 0.0,
          "prior_year_true_up": 0.0}],
        actor="smoke_test",
    )
    assert provision_result["group_summary"]["total_tax_expense"] == 250000.0
    print(f"    ✓ Tax provision arithmetic correct: {provision_result['group_summary']}\n")

    print("[5/5] Testing Pillar Two agent + cross-module derivation...")
    state = {
        "tenant_id": "smoke_test", "period": "2024-12", "actor": "smoke_test",
        "provision_entity_inputs": [
            {"entity_code": "TEST-UK", "period": "2024-12", "jurisdiction": "GB",
             "pretax_income": 1000000.0, "statutory_rate_pct": 10.0,  # below 15% min
             "permanent_differences": 0.0, "temporary_differences": 0.0,
             "prior_year_true_up": 0.0},
        ],
        "pillar_two_jurisdiction_inputs": [
            {"jurisdiction": "GB", "entities_in_scope": ["TEST-UK"],
             "three_year_avg_revenue": 20000000.0, "three_year_avg_income": 2000000.0,
             "globe_income": 0, "covered_taxes": 0},
        ],
        "auto_derive_pillar_two_from_provision": True,
    }
    result = run_sequential(db, agents, state)
    p2_calc = result["pillar_two_result"]["etr_calculations"][0]
    assert p2_calc["covered_taxes"] == 100000.0, "Cross-module derivation failed"
    assert bool(p2_calc["top_up_tax_required"]) is True
    print(f"    ✓ Pillar Two correctly derived covered_taxes from Tax Provision output")
    print(f"    ✓ Top-up tax correctly triggered: {p2_calc['top_up_tax_amount']}\n")

    print("Checking audit trail hash chain integrity...")
    audit = db.get_audit_log("smoke_test")
    for i in range(len(audit) - 1):
        assert audit[i]["prior_hash"] == audit[i + 1]["event_hash"], "HASH CHAIN BROKEN"
    print(f"    ✓ {len(audit)} audit events, hash chain intact\n")

    escalations = db.get_open_escalations("smoke_test")
    print(f"Escalations raised: {len(escalations)}")
    for e in escalations:
        print(f"    - [{e['agent_name']}] {e['escalation_type']}")

    print("\n" + "=" * 60)
    print("ALL SMOKE TESTS PASSED — Tax Command is correctly installed.")
    print("=" * 60)

    os.remove(db_path)


if __name__ == "__main__":
    main()
