"""
tax_command/models/core.py

Core data models shared across all four Tax Command agents:
  - Indirect Tax / VAT Agent
  - Transfer Pricing Agent
  - Tax Provision Agent
  - Pillar Two / BEPS Agent

Architectural rule (same as Close Command): these models represent
DETERMINISTIC tax positions. ML and Gen AI never write directly into
these dataclasses — they produce advisory outputs (TPAdvisory,
ETRForecast, VATClassificationSuggestion) that a human or a rule engine
converts into a position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Shared enums
# ─────────────────────────────────────────────────────────────────────────────

class TaxJurisdiction(str, Enum):
    """ISO 3166-1 alpha-2 country codes — extend as entities are added."""
    GB = "GB"; DE = "DE"; FR = "FR"; US = "US"; SG = "SG"; IE = "IE"
    NL = "NL"; CH = "CH"; LU = "LU"; IN = "IN"; CN = "CN"; JP = "JP"
    OTHER = "OTHER"


class FilingStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    FILED = "FILED"
    AMENDED = "AMENDED"
    REJECTED = "REJECTED"


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Indirect Tax / VAT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VATTransaction:
    """A single transaction subject to VAT/GST classification."""
    txn_id: str
    entity_code: str
    jurisdiction: TaxJurisdiction
    transaction_date: date
    counterparty: str
    counterparty_vat_number: Optional[str]
    amount_net: float
    currency: str
    description: str
    account_code: str
    # Deterministic classification (set by rule engine, never by ML directly)
    vat_treatment: Optional[str] = None          # e.g. "STANDARD_20", "ZERO_RATED", "EXEMPT", "REVERSE_CHARGE"
    vat_rate_pct: Optional[float] = None
    vat_amount: Optional[float] = None
    place_of_supply: Optional[str] = None
    classification_source: str = "PENDING"        # RULE_ENGINE | ML_SUGGESTED | MANUAL
    classification_confidence: Optional[ConfidenceLevel] = None
    requires_review: bool = False


@dataclass
class VATReturn:
    """A periodic VAT/GST return for one entity/jurisdiction."""
    return_id: str
    entity_code: str
    jurisdiction: TaxJurisdiction
    period_start: date
    period_end: date
    output_vat: float = 0.0
    input_vat: float = 0.0
    net_vat_payable: float = 0.0
    transaction_count: int = 0
    flagged_count: int = 0
    status: FilingStatus = FilingStatus.DRAFT
    filed_at: Optional[datetime] = None
    filed_by: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Transfer Pricing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IntercompanyTransaction:
    """An intra-group transaction subject to transfer pricing analysis."""
    txn_id: str
    period: str
    seller_entity: str
    buyer_entity: str
    seller_jurisdiction: TaxJurisdiction
    buyer_jurisdiction: TaxJurisdiction
    transaction_type: str           # e.g. "TANGIBLE_GOODS", "SERVICES", "ROYALTY", "FINANCING", "MGMT_FEE"
    amount: float
    currency: str
    description: str
    tp_method: Optional[str] = None   # CUP | RPM | CPM | TNMM | PSM (deterministic, set by TP policy)
    benchmark_range_low: Optional[float] = None
    benchmark_range_high: Optional[float] = None
    actual_margin_pct: Optional[float] = None
    is_arms_length: Optional[bool] = None
    requires_documentation: bool = False


@dataclass
class TPDocumentationPack:
    """Master file + local file documentation status per entity."""
    entity_code: str
    period: str
    has_master_file: bool = False
    has_local_file: bool = False
    has_cbcr_inclusion: bool = False     # Country-by-Country Reporting
    benchmarking_study_date: Optional[date] = None
    documentation_status: FilingStatus = FilingStatus.DRAFT
    risk_rating: Optional[ConfidenceLevel] = None


@dataclass
class TPAdvisory:
    """
    LLM/ML advisory output for a TP analysis — NEVER a final position.
    Human (Tax Director) converts this into the deterministic TP method
    and benchmark range on IntercompanyTransaction.
    """
    txn_id: str
    suggested_method: str
    suggested_benchmark_low: float
    suggested_benchmark_high: float
    comparable_set_summary: str
    confidence: ConfidenceLevel
    narrative: str
    generated_at: datetime = field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tax Provision
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaxProvisionLine:
    """Current/deferred tax provision for one entity for one period."""
    entity_code: str
    period: str
    jurisdiction: TaxJurisdiction
    pretax_income: float
    statutory_rate_pct: float
    permanent_differences: float = 0.0
    temporary_differences: float = 0.0
    current_tax_expense: float = 0.0
    deferred_tax_expense: float = 0.0
    total_tax_expense: float = 0.0
    effective_tax_rate_pct: float = 0.0
    prior_year_true_up: float = 0.0
    status: FilingStatus = FilingStatus.DRAFT


@dataclass
class DeferredTaxItem:
    """Individual deferred tax asset/liability item (rolls forward each period)."""
    item_id: str
    entity_code: str
    description: str
    category: str               # e.g. "FIXED_ASSETS", "PROVISIONS", "TAX_LOSSES", "PENSIONS"
    opening_balance: float
    movement: float
    closing_balance: float
    is_asset: bool               # True = DTA, False = DTL
    recognition_supportable: bool = True   # valuation allowance trigger


# ─────────────────────────────────────────────────────────────────────────────
# 4. Pillar Two / BEPS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EntityETRCalculation:
    """Jurisdictional ETR calculation per OECD Pillar Two GloBE rules."""
    jurisdiction: TaxJurisdiction
    period: str
    entities_in_scope: list[str]
    globe_income: float
    covered_taxes: float
    jurisdictional_etr_pct: float
    minimum_rate_pct: float = 15.0
    top_up_tax_required: bool = False
    top_up_tax_amount: float = 0.0
    safe_harbour_applied: Optional[str] = None    # "CBCR_SAFE_HARBOUR" | "DE_MINIMIS" | None


@dataclass
class QDMTTFiling:
    """Qualified Domestic Minimum Top-up Tax filing for a jurisdiction."""
    filing_id: str
    jurisdiction: TaxJurisdiction
    period: str
    ultimate_parent_entity: str
    top_up_tax_amount: float
    allocated_entities: list[str]
    status: FilingStatus = FilingStatus.DRAFT
    due_date: Optional[date] = None
    filed_at: Optional[datetime] = None


@dataclass
class ETRForecast:
    """
    ML/LLM advisory output — forecasted ETR trajectory, NEVER the filed position.
    Used by the Pillar Two agent to flag entities approaching the 15% threshold
    before period-end, giving Tax time to plan substance-based carve-outs.
    """
    jurisdiction: TaxJurisdiction
    period: str
    forecasted_etr_pct: float
    confidence: ConfidenceLevel
    risk_flag: str               # "SAFE" | "WATCH" | "BREACH_LIKELY"
    narrative: str
    generated_at: datetime = field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Shared: escalation and audit objects (same pattern as Close Command)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaxEscalation:
    """Raised when an agent output requires human judgement beyond its mandate."""
    escalation_id: str
    agent_name: str
    entity_code: str
    period: str
    escalation_type: str   # e.g. "TP_METHOD_AMBIGUOUS", "ETR_BREACH_RISK", "VAT_JURISDICTION_UNCLEAR"
    description: str
    amount_usd: Optional[float] = None
    raised_at: datetime = field(default_factory=datetime.utcnow)
    resolved: bool = False
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
