"""
tax_command/data/sample_data.py

Realistic sample data for the Tax Command demo, modelled on a mid-market
multinational group with the kind of footprint that triggers Pillar Two
GloBE obligations: 8 entities across 7 jurisdictions, consolidated
revenue comfortably above the EUR 750M threshold.

Deterministic — random.seed(42) — so the demo behaves the same way
every run unless the user changes the seed.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

SEED = 42

# ─────────────────────────────────────────────────────────────────────────────
# Group structure — modelled on a real multinational shape
# ─────────────────────────────────────────────────────────────────────────────

ENTITIES = [
    {"entity_code": "MERIDIAN_UK",   "entity_name": "Meridian Industrial Holdings plc", "jurisdiction": "GB", "is_ultimate_parent": 1, "parent_entity": None,         "statutory_rate_pct": 25.0, "vat_number": "GB123456789", "is_active": 1},
    {"entity_code": "MERIDIAN_DE",   "entity_name": "Meridian Manufacturing GmbH",       "jurisdiction": "DE", "is_ultimate_parent": 0, "parent_entity": "MERIDIAN_UK", "statutory_rate_pct": 29.9, "vat_number": "DE987654321", "is_active": 1},
    {"entity_code": "MERIDIAN_IE",   "entity_name": "Meridian IP & Licensing Ltd",       "jurisdiction": "IE", "is_ultimate_parent": 0, "parent_entity": "MERIDIAN_UK", "statutory_rate_pct": 12.5, "vat_number": "IE3456789A", "is_active": 1},
    {"entity_code": "MERIDIAN_SG",   "entity_name": "Meridian Asia Pacific Pte Ltd",     "jurisdiction": "SG", "is_ultimate_parent": 0, "parent_entity": "MERIDIAN_UK", "statutory_rate_pct": 17.0, "vat_number": "SG200912345", "is_active": 1},
    {"entity_code": "MERIDIAN_LU",   "entity_name": "Meridian Treasury Sarl",            "jurisdiction": "LU", "is_ultimate_parent": 0, "parent_entity": "MERIDIAN_UK", "statutory_rate_pct": 24.9, "vat_number": "LU24681012", "is_active": 1},
    {"entity_code": "MERIDIAN_US",   "entity_name": "Meridian North America Inc",        "jurisdiction": "US", "is_ultimate_parent": 0, "parent_entity": "MERIDIAN_UK", "statutory_rate_pct": 21.0, "vat_number": None, "is_active": 1},
    {"entity_code": "MERIDIAN_NL",   "entity_name": "Meridian Distribution BV",          "jurisdiction": "NL", "is_ultimate_parent": 0, "parent_entity": "MERIDIAN_UK", "statutory_rate_pct": 25.8, "vat_number": "NL135792468", "is_active": 1},
    {"entity_code": "MERIDIAN_IN",   "entity_name": "Meridian India Engineering Pvt Ltd","jurisdiction": "IN", "is_ultimate_parent": 0, "parent_entity": "MERIDIAN_UK", "statutory_rate_pct": 25.2, "vat_number": None, "is_active": 1},
]

ENTITY_CODES = [e["entity_code"] for e in ENTITIES]
PERIOD = "2026-Q1"
PERIOD_START = "2026-01-01"
PERIOD_END = "2026-03-31"

COUNTERPARTIES = [
    "Atlas Steel Supply Co", "Northbridge Logistics Group", "Solace Components Ltd",
    "Hartwell Industrial Partners", "Quantel Precision Engineering", "Vela Freight Systems",
    "Carraway & Finch Distribution", "Ironbark Materials AG", "Tidewater Components SA",
    "Brightline Electronics BV", "Summit Fasteners Inc", "Greyfriars Packaging Ltd",
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. VAT transactions — Meridian DE (mature category)
# ─────────────────────────────────────────────────────────────────────────────

def generate_vat_transactions(n: int = 220, seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    start = date(2026, 1, 1)
    for i in range(n):
        txn_date = start + timedelta(days=rng.randint(0, 89))
        amount = round(rng.choice([1, -1]) * rng.uniform(450, 185_000), 2)
        counterparty = rng.choice(COUNTERPARTIES)
        is_eu_vat = rng.random() < 0.55
        cp_vat = f"{rng.choice(['DE','FR','NL','IE','GB','SG'])}{rng.randint(100000000,999999999)}" if is_eu_vat else None

        # seed deliberate edge cases: ~12% ambiguous, the kind that fall to manual review
        ambiguous = rng.random() < 0.12
        desc_pool = ["Component supply", "Engineering services", "Freight and logistics",
                     "Export shipment — Rotterdam", "Insurance premium allocation",
                     "Intercompany management fee passthrough", "Financial services fee",
                     "Spare parts delivery", "Software licence renewal"]
        description = rng.choice(desc_pool)
        if ambiguous:
            description = "Mixed-supply contract — partial digital/partial physical delivery"

        rows.append({
            "txn_id": f"VAT-DE-{i+1:04d}",
            "jurisdiction": "DE",
            "transaction_date": txn_date.isoformat(),
            "counterparty": counterparty,
            "counterparty_vat_number": cp_vat,
            "amount_net": amount,
            "currency": "EUR",
            "description": description,
            "account_code": rng.choice(["6100-COGS", "7200-FREIGHT", "8400-PROFESSIONAL_FEES", "6650-EXPORT"]),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 2. Intercompany transactions — Transfer Pricing (white space)
# ─────────────────────────────────────────────────────────────────────────────

TP_TRANSACTION_TEMPLATES = [
    # (seller, buyer, type, method, low, high, description)
    ("MERIDIAN_DE", "MERIDIAN_UK", "TANGIBLE_GOODS", "TNMM", 4.0, 7.0, "Precision component supply — UK assembly"),
    ("MERIDIAN_IE", "MERIDIAN_DE", "ROYALTY",         "CUP",  3.5, 5.5, "IP licence — patented alloy process"),
    ("MERIDIAN_IE", "MERIDIAN_US", "ROYALTY",         "CUP",  3.5, 5.5, "IP licence — patented alloy process"),
    ("MERIDIAN_SG", "MERIDIAN_NL", "SERVICES",        "CPM",  5.0, 9.0, "Regional procurement coordination services"),
    ("MERIDIAN_UK", "MERIDIAN_DE", "MGMT_FEE",        "TNMM", 4.0, 6.0, "Group management and HQ recharge"),
    ("MERIDIAN_LU", "MERIDIAN_UK", "FINANCING",       "CUP",  1.8, 3.2, "Intercompany loan — working capital facility"),
    ("MERIDIAN_LU", "MERIDIAN_US", "FINANCING",       "CUP",  1.8, 3.2, "Intercompany loan — capex facility"),
    ("MERIDIAN_IN", "MERIDIAN_DE", "SERVICES",        "TNMM", 8.0, 12.0, "Engineering design and CAD support"),
    ("MERIDIAN_NL", "MERIDIAN_SG", "TANGIBLE_GOODS",  "RPM",  6.0, 10.0, "Finished goods distribution — APAC resale"),
    # Deliberately unbenchmarked — the white-space trigger for advisory
    ("MERIDIAN_SG", "MERIDIAN_IN", "SERVICES",        None,   None, None, "New data analytics shared-services arrangement"),
    ("MERIDIAN_US", "MERIDIAN_DE", "MGMT_FEE",        None,   None, None, "New North America platform support fee"),
]


def generate_tp_transactions(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for i, (seller, buyer, ttype, method, low, high, desc) in enumerate(TP_TRANSACTION_TEMPLATES):
        amount = round(rng.uniform(180_000, 9_500_000), 2)
        # actual margin: mostly within range, but plant 2-3 deliberate breaches
        if method:
            breach = rng.random() < 0.27
            if breach:
                margin = round(rng.choice([low - rng.uniform(1.0, 2.5), high + rng.uniform(1.0, 3.0)]), 2)
            else:
                margin = round(rng.uniform(low, high), 2)
        else:
            margin = None

        seller_entity = next(e for e in ENTITIES if e["entity_code"] == seller)
        buyer_entity = next(e for e in ENTITIES if e["entity_code"] == buyer)

        rows.append({
            "txn_id": f"TP-{PERIOD}-{i+1:03d}",
            "period": PERIOD,
            "seller_entity": seller,
            "buyer_entity": buyer,
            "seller_jurisdiction": seller_entity["jurisdiction"],
            "buyer_jurisdiction": buyer_entity["jurisdiction"],
            "transaction_type": ttype,
            "amount": amount,
            "currency": "USD",
            "description": desc,
            "tp_method": method,
            "benchmark_range_low": low,
            "benchmark_range_high": high,
            "actual_margin_pct": margin,
        })
    return rows


def generate_tp_documentation_status(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    statuses = []
    for e in ENTITIES:
        if e["entity_code"] == "MERIDIAN_UK":
            continue  # ultimate parent — master file owner, not subject to local-file gap
        has_master = rng.random() < 0.85
        has_local = rng.random() < 0.55   # local files are where Tax Directors actually fall behind
        has_cbcr = rng.random() < 0.80
        statuses.append({
            "entity_code": e["entity_code"],
            "has_master_file": int(has_master),
            "has_local_file": int(has_local),
            "has_cbcr_inclusion": int(has_cbcr),
            "risk_rating": rng.choice(["LOW", "MEDIUM", "HIGH"]) if not (has_master and has_local and has_cbcr) else "LOW",
        })
    return statuses


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tax provision — entity-level pretax income and differences
# ─────────────────────────────────────────────────────────────────────────────

def generate_provision_inputs(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for e in ENTITIES:
        pretax = round(rng.uniform(1_800_000, 42_000_000), 2)
        permanent = round(rng.uniform(-450_000, 950_000), 2)
        temporary = round(rng.uniform(-1_200_000, 1_800_000), 2)
        prior_true_up = round(rng.uniform(-80_000, 80_000), 2)
        rows.append({
            "entity_code": e["entity_code"],
            "period": PERIOD,
            "jurisdiction": e["jurisdiction"],
            "pretax_income": pretax,
            "statutory_rate_pct": e["statutory_rate_pct"],
            "permanent_differences": permanent,
            "temporary_differences": temporary,
            "prior_year_true_up": prior_true_up,
        })
    return rows


def generate_prior_deferred_items(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed + 1)
    rows = []
    for e in ENTITIES:
        rows.append({
            "entity_code": e["entity_code"],
            "closing_balance": round(rng.uniform(300_000, 2_200_000), 2),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 4. Pillar Two / GloBE — jurisdictional inputs (the white-space module)
# ─────────────────────────────────────────────────────────────────────────────
# Modelled so that two jurisdictions deliberately breach the 15% GloBE
# minimum — IE (low statutory rate + heavy IP income) and SG (regional
# incentive regime) — to demonstrate the top-up tax and QDMTT flow.

def generate_pillar_two_inputs(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    jurisdiction_entities: dict[str, list[str]] = {}
    for e in ENTITIES:
        jurisdiction_entities.setdefault(e["jurisdiction"], []).append(e["entity_code"])

    # (jurisdiction, globe_income, effective covered-tax rate target)
    profile = {
        "GB": 38_000_000 * rng.uniform(0.92, 1.08),
        "DE": 29_500_000 * rng.uniform(0.92, 1.08),
        "IE": 21_000_000 * rng.uniform(0.92, 1.08),   # low-tax IP hub — will breach
        "SG": 17_500_000 * rng.uniform(0.92, 1.08),   # incentive regime — will breach
        "LU": 4_200_000 * rng.uniform(0.92, 1.08),    # treasury entity, small base — de minimis candidate
        "US": 33_000_000 * rng.uniform(0.92, 1.08),
        "NL": 19_000_000 * rng.uniform(0.92, 1.08),
        "IN": 12_500_000 * rng.uniform(0.92, 1.08),
    }
    target_etr = {
        "GB": 22.5, "DE": 27.0, "IE": 6.8, "SG": 8.1,
        "LU": 16.0, "US": 19.5, "NL": 21.0, "IN": 23.0,
    }

    rows = []
    for jurisdiction, globe_income in profile.items():
        globe_income = round(globe_income, 2)
        covered_taxes = round(globe_income * (target_etr[jurisdiction] / 100.0), 2)
        rows.append({
            "jurisdiction": jurisdiction,
            "entities_in_scope": jurisdiction_entities.get(jurisdiction, []),
            "globe_income": globe_income,
            "covered_taxes": covered_taxes,
            "three_year_avg_revenue": globe_income * rng.uniform(2.8, 3.4),
            "three_year_avg_income": globe_income * rng.uniform(0.85, 1.05),
        })
    return rows


def generate_historical_etrs(jurisdiction: str, current_etr_pct: float, seed: int = SEED) -> list[dict]:
    """Four trailing quarters trending toward the current period, for ETR forecasting."""
    rng = random.Random(seed + hash(jurisdiction) % 1000)
    quarters = ["2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1"]
    # build a trend that lands near current_etr_pct in the final quarter
    start_etr = current_etr_pct + rng.uniform(1.5, 3.5)
    step = (current_etr_pct - start_etr) / 3
    history = []
    for i, q in enumerate(quarters):
        etr = round(start_etr + step * i + rng.uniform(-0.4, 0.4), 2)
        history.append({"jurisdiction": jurisdiction, "period": q, "jurisdictional_etr_pct": etr})
    return history
