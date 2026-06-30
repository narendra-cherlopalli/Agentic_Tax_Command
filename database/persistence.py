"""
tax_command/database/persistence.py

Multi-tenant database persistence for Tax Command.

Unlike Close Command (single-tenant, built for Helios), this is designed
as a commercial product from day one — every table carries a tenant_id
and every query is scoped to it. This is the single most important
architectural decision in a multi-tenant SaaS: there is no code path
that can accidentally return another tenant's tax data.

Uses SQLite for local development; the schema is written to be portable
to Postgres for production (no SQLite-specific syntax beyond AUTOINCREMENT).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DDL = """
-- ── Tenancy ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id       TEXT PRIMARY KEY,
    tenant_name     TEXT NOT NULL,
    plan_tier       TEXT NOT NULL DEFAULT 'STANDARD',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tenant_users (
    user_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    email           TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'ANALYST',  -- ANALYST | TAX_DIRECTOR | ADMIN
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- ── Master data: entities (shared reference for all 4 modules) ───────────
CREATE TABLE IF NOT EXISTS tax_entities (
    tenant_id           TEXT NOT NULL,
    entity_code         TEXT NOT NULL,
    entity_name         TEXT NOT NULL,
    jurisdiction        TEXT NOT NULL,
    is_ultimate_parent  INTEGER NOT NULL DEFAULT 0,
    parent_entity       TEXT,
    statutory_rate_pct  REAL NOT NULL DEFAULT 25.0,
    vat_number          TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by          TEXT NOT NULL DEFAULT 'system',
    PRIMARY KEY (tenant_id, entity_code)
);

-- ── Module 1: VAT / Indirect Tax ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vat_transactions (
    tenant_id               TEXT NOT NULL,
    txn_id                  TEXT NOT NULL,
    entity_code             TEXT NOT NULL,
    jurisdiction            TEXT NOT NULL,
    transaction_date        TEXT NOT NULL,
    counterparty             TEXT,
    counterparty_vat_number  TEXT,
    amount_net               REAL NOT NULL DEFAULT 0,
    currency                 TEXT NOT NULL DEFAULT 'USD',
    description               TEXT,
    account_code               TEXT,
    vat_treatment              TEXT,
    vat_rate_pct                REAL,
    vat_amount                  REAL,
    place_of_supply             TEXT,
    classification_source       TEXT NOT NULL DEFAULT 'PENDING',
    classification_confidence   TEXT,
    requires_review              INTEGER NOT NULL DEFAULT 0,
    created_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, txn_id)
);

CREATE TABLE IF NOT EXISTS vat_returns (
    tenant_id           TEXT NOT NULL,
    return_id            TEXT NOT NULL,
    entity_code           TEXT NOT NULL,
    jurisdiction           TEXT NOT NULL,
    period_start             TEXT NOT NULL,
    period_end                TEXT NOT NULL,
    output_vat                  REAL NOT NULL DEFAULT 0,
    input_vat                    REAL NOT NULL DEFAULT 0,
    net_vat_payable                REAL NOT NULL DEFAULT 0,
    transaction_count                INTEGER NOT NULL DEFAULT 0,
    flagged_count                      INTEGER NOT NULL DEFAULT 0,
    status                               TEXT NOT NULL DEFAULT 'DRAFT',
    filed_at                              TEXT,
    filed_by                              TEXT,
    PRIMARY KEY (tenant_id, return_id)
);

-- ── Module 2: Transfer Pricing ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tp_transactions (
    tenant_id                 TEXT NOT NULL,
    txn_id                     TEXT NOT NULL,
    period                       TEXT NOT NULL,
    seller_entity                  TEXT NOT NULL,
    buyer_entity                     TEXT NOT NULL,
    seller_jurisdiction                TEXT NOT NULL,
    buyer_jurisdiction                   TEXT NOT NULL,
    transaction_type                       TEXT NOT NULL,
    amount                                   REAL NOT NULL DEFAULT 0,
    currency                                   TEXT NOT NULL DEFAULT 'USD',
    description                                  TEXT,
    tp_method                                      TEXT,
    benchmark_range_low                              REAL,
    benchmark_range_high                               REAL,
    actual_margin_pct                                    REAL,
    is_arms_length                                         INTEGER,
    requires_documentation                                   INTEGER NOT NULL DEFAULT 0,
    created_at                                                 TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, txn_id)
);

CREATE TABLE IF NOT EXISTS tp_documentation (
    tenant_id                 TEXT NOT NULL,
    entity_code                 TEXT NOT NULL,
    period                         TEXT NOT NULL,
    has_master_file                  INTEGER NOT NULL DEFAULT 0,
    has_local_file                     INTEGER NOT NULL DEFAULT 0,
    has_cbcr_inclusion                   INTEGER NOT NULL DEFAULT 0,
    benchmarking_study_date                TEXT,
    documentation_status                     TEXT NOT NULL DEFAULT 'DRAFT',
    risk_rating                                TEXT,
    PRIMARY KEY (tenant_id, entity_code, period)
);

CREATE TABLE IF NOT EXISTS tp_advisories (
    tenant_id           TEXT NOT NULL,
    advisory_id           TEXT NOT NULL,
    txn_id                  TEXT NOT NULL,
    suggested_method          TEXT NOT NULL,
    suggested_benchmark_low      REAL NOT NULL,
    suggested_benchmark_high       REAL NOT NULL,
    comparable_set_summary           TEXT,
    confidence                         TEXT NOT NULL,
    narrative                            TEXT,
    generated_at                           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, advisory_id)
);

-- ── Module 3: Tax Provision ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tax_provisions (
    tenant_id                 TEXT NOT NULL,
    entity_code                 TEXT NOT NULL,
    period                         TEXT NOT NULL,
    jurisdiction                     TEXT NOT NULL,
    pretax_income                       REAL NOT NULL DEFAULT 0,
    statutory_rate_pct                    REAL NOT NULL DEFAULT 25.0,
    permanent_differences                   REAL NOT NULL DEFAULT 0,
    temporary_differences                     REAL NOT NULL DEFAULT 0,
    current_tax_expense                         REAL NOT NULL DEFAULT 0,
    deferred_tax_expense                          REAL NOT NULL DEFAULT 0,
    total_tax_expense                               REAL NOT NULL DEFAULT 0,
    effective_tax_rate_pct                            REAL NOT NULL DEFAULT 0,
    prior_year_true_up                                  REAL NOT NULL DEFAULT 0,
    status                                                TEXT NOT NULL DEFAULT 'DRAFT',
    PRIMARY KEY (tenant_id, entity_code, period)
);

CREATE TABLE IF NOT EXISTS deferred_tax_items (
    tenant_id                 TEXT NOT NULL,
    item_id                     TEXT NOT NULL,
    entity_code                   TEXT NOT NULL,
    period                           TEXT NOT NULL,
    description                        TEXT,
    category                             TEXT NOT NULL,
    opening_balance                        REAL NOT NULL DEFAULT 0,
    movement                                 REAL NOT NULL DEFAULT 0,
    closing_balance                            REAL NOT NULL DEFAULT 0,
    is_asset                                     INTEGER NOT NULL DEFAULT 1,
    recognition_supportable                        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, item_id)
);

-- ── Module 4: Pillar Two / BEPS ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pillar_two_etr (
    tenant_id                 TEXT NOT NULL,
    jurisdiction                 TEXT NOT NULL,
    period                          TEXT NOT NULL,
    entities_in_scope                  TEXT NOT NULL DEFAULT '[]',  -- JSON array
    globe_income                         REAL NOT NULL DEFAULT 0,
    covered_taxes                          REAL NOT NULL DEFAULT 0,
    jurisdictional_etr_pct                   REAL NOT NULL DEFAULT 0,
    minimum_rate_pct                           REAL NOT NULL DEFAULT 15.0,
    top_up_tax_required                          INTEGER NOT NULL DEFAULT 0,
    top_up_tax_amount                              REAL NOT NULL DEFAULT 0,
    safe_harbour_applied                             TEXT,
    PRIMARY KEY (tenant_id, jurisdiction, period)
);

CREATE TABLE IF NOT EXISTS qdmtt_filings (
    tenant_id             TEXT NOT NULL,
    filing_id               TEXT NOT NULL,
    jurisdiction               TEXT NOT NULL,
    period                        TEXT NOT NULL,
    ultimate_parent_entity          TEXT NOT NULL,
    top_up_tax_amount                  REAL NOT NULL DEFAULT 0,
    allocated_entities                    TEXT NOT NULL DEFAULT '[]',  -- JSON array
    status                                   TEXT NOT NULL DEFAULT 'DRAFT',
    due_date                                  TEXT,
    filed_at                                    TEXT,
    PRIMARY KEY (tenant_id, filing_id)
);

CREATE TABLE IF NOT EXISTS etr_forecasts (
    tenant_id           TEXT NOT NULL,
    forecast_id           TEXT NOT NULL,
    jurisdiction             TEXT NOT NULL,
    period                      TEXT NOT NULL,
    forecasted_etr_pct            REAL NOT NULL,
    confidence                       TEXT NOT NULL,
    risk_flag                          TEXT NOT NULL,
    narrative                            TEXT,
    generated_at                           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, forecast_id)
);

-- ── Shared: escalations and audit trail ────────────────────────────────────
CREATE TABLE IF NOT EXISTS tax_escalations (
    tenant_id           TEXT NOT NULL,
    escalation_id          TEXT NOT NULL,
    agent_name                TEXT NOT NULL,
    entity_code                  TEXT NOT NULL,
    period                          TEXT NOT NULL,
    escalation_type                   TEXT NOT NULL,
    description                          TEXT,
    amount_usd                              REAL,
    raised_at                                 TEXT NOT NULL DEFAULT (datetime('now')),
    resolved                                    INTEGER NOT NULL DEFAULT 0,
    resolved_by                                   TEXT,
    resolution_notes                                TEXT,
    PRIMARY KEY (tenant_id, escalation_id)
);

CREATE TABLE IF NOT EXISTS tax_audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id              TEXT NOT NULL,
    event_type                TEXT NOT NULL,
    agent_name                   TEXT,
    entity_code                     TEXT,
    period                             TEXT,
    actor                                 TEXT NOT NULL,
    payload                                  TEXT NOT NULL DEFAULT '{}',
    prior_hash                                 TEXT,
    event_hash                                   TEXT,
    created_at                                     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_vat_tenant       ON vat_transactions(tenant_id, entity_code);
CREATE INDEX IF NOT EXISTS idx_tp_tenant         ON tp_transactions(tenant_id, period);
CREATE INDEX IF NOT EXISTS idx_provision_tenant  ON tax_provisions(tenant_id, period);
CREATE INDEX IF NOT EXISTS idx_etr_tenant         ON pillar_two_etr(tenant_id, period);
CREATE INDEX IF NOT EXISTS idx_audit_tenant       ON tax_audit_log(tenant_id, created_at);
"""


class TaxCommandDB:
    """
    Multi-tenant persistence layer for Tax Command.

    Every public method requires a tenant_id as its first argument
    (after self). This is enforced at the method signature level —
    there is no method that queries across tenants. If you need
    cross-tenant analytics (e.g. for internal product metrics),
    write a separate admin-only module, never extend this class.
    """

    def __init__(self, db_path: str = "tax_command.db") -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(DDL)
        self.conn.commit()

    @contextmanager
    def _tenant_scope(self, tenant_id: str):
        """Guard clause — every method calls this first. Raises if tenant_id is blank."""
        if not tenant_id or not tenant_id.strip():
            raise ValueError(
                "tenant_id is required for every TaxCommandDB operation — "
                "refusing to query without tenant scope."
            )
        yield tenant_id.strip()

    # ─────────────────────────────────────────────────────────────────────────
    # TENANCY
    # ─────────────────────────────────────────────────────────────────────────

    def create_tenant(self, tenant_id: str, tenant_name: str, plan_tier: str = "STANDARD") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO tenants (tenant_id, tenant_name, plan_tier) VALUES (?, ?, ?)",
            (tenant_id, tenant_name, plan_tier),
        )
        self.conn.commit()

    def get_tenant(self, tenant_id: str) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    # ─────────────────────────────────────────────────────────────────────────
    # MASTER DATA: ENTITIES
    # ─────────────────────────────────────────────────────────────────────────

    def upsert_entity(self, tenant_id: str, entity: dict, updated_by: str = "user") -> None:
        with self._tenant_scope(tenant_id) as tid:
            self.conn.execute("""
                INSERT INTO tax_entities
                    (tenant_id, entity_code, entity_name, jurisdiction,
                     is_ultimate_parent, parent_entity, statutory_rate_pct,
                     vat_number, is_active, updated_at, updated_by)
                VALUES (:tenant_id, :entity_code, :entity_name, :jurisdiction,
                        :is_ultimate_parent, :parent_entity, :statutory_rate_pct,
                        :vat_number, :is_active, :updated_at, :updated_by)
                ON CONFLICT(tenant_id, entity_code) DO UPDATE SET
                    entity_name         = excluded.entity_name,
                    jurisdiction        = excluded.jurisdiction,
                    is_ultimate_parent  = excluded.is_ultimate_parent,
                    parent_entity       = excluded.parent_entity,
                    statutory_rate_pct  = excluded.statutory_rate_pct,
                    vat_number          = excluded.vat_number,
                    is_active           = excluded.is_active,
                    updated_at          = excluded.updated_at,
                    updated_by          = excluded.updated_by
            """, {**entity, "tenant_id": tid,
                  "updated_at": datetime.utcnow().isoformat(), "updated_by": updated_by})
            self.conn.commit()

    def get_entities(self, tenant_id: str, active_only: bool = True) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            where = "AND is_active = 1" if active_only else ""
            cur = self.conn.execute(
                f"SELECT * FROM tax_entities WHERE tenant_id = ? {where} ORDER BY entity_code",
                (tid,)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ─────────────────────────────────────────────────────────────────────────
    # MODULE 1: VAT
    # ─────────────────────────────────────────────────────────────────────────

    def save_vat_transactions(self, tenant_id: str, transactions: list[dict]) -> None:
        with self._tenant_scope(tenant_id) as tid:
            rows = [{**t, "tenant_id": tid} for t in transactions]
            self.conn.executemany("""
                INSERT OR REPLACE INTO vat_transactions
                    (tenant_id, txn_id, entity_code, jurisdiction, transaction_date,
                     counterparty, counterparty_vat_number, amount_net, currency,
                     description, account_code, vat_treatment, vat_rate_pct,
                     vat_amount, place_of_supply, classification_source,
                     classification_confidence, requires_review)
                VALUES
                    (:tenant_id, :txn_id, :entity_code, :jurisdiction, :transaction_date,
                     :counterparty, :counterparty_vat_number, :amount_net, :currency,
                     :description, :account_code, :vat_treatment, :vat_rate_pct,
                     :vat_amount, :place_of_supply, :classification_source,
                     :classification_confidence, :requires_review)
            """, rows)
            self.conn.commit()

    def get_vat_transactions(
        self, tenant_id: str, entity_code: Optional[str] = None,
        requires_review_only: bool = False,
    ) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            query = "SELECT * FROM vat_transactions WHERE tenant_id = ?"
            params = [tid]
            if entity_code:
                query += " AND entity_code = ?"
                params.append(entity_code)
            if requires_review_only:
                query += " AND requires_review = 1"
            cur = self.conn.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def save_vat_return(self, tenant_id: str, vat_return: dict) -> None:
        with self._tenant_scope(tenant_id) as tid:
            self.conn.execute("""
                INSERT OR REPLACE INTO vat_returns
                    (tenant_id, return_id, entity_code, jurisdiction,
                     period_start, period_end, output_vat, input_vat,
                     net_vat_payable, transaction_count, flagged_count, status)
                VALUES
                    (:tenant_id, :return_id, :entity_code, :jurisdiction,
                     :period_start, :period_end, :output_vat, :input_vat,
                     :net_vat_payable, :transaction_count, :flagged_count, :status)
            """, {**vat_return, "tenant_id": tid})
            self.conn.commit()

    # ─────────────────────────────────────────────────────────────────────────
    # MODULE 2: TRANSFER PRICING
    # ─────────────────────────────────────────────────────────────────────────

    def save_tp_transactions(self, tenant_id: str, transactions: list[dict]) -> None:
        with self._tenant_scope(tenant_id) as tid:
            rows = [{**t, "tenant_id": tid} for t in transactions]
            self.conn.executemany("""
                INSERT OR REPLACE INTO tp_transactions
                    (tenant_id, txn_id, period, seller_entity, buyer_entity,
                     seller_jurisdiction, buyer_jurisdiction, transaction_type,
                     amount, currency, description, tp_method,
                     benchmark_range_low, benchmark_range_high,
                     actual_margin_pct, is_arms_length, requires_documentation)
                VALUES
                    (:tenant_id, :txn_id, :period, :seller_entity, :buyer_entity,
                     :seller_jurisdiction, :buyer_jurisdiction, :transaction_type,
                     :amount, :currency, :description, :tp_method,
                     :benchmark_range_low, :benchmark_range_high,
                     :actual_margin_pct, :is_arms_length, :requires_documentation)
            """, rows)
            self.conn.commit()

    def get_tp_transactions(self, tenant_id: str, period: Optional[str] = None) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            query = "SELECT * FROM tp_transactions WHERE tenant_id = ?"
            params = [tid]
            if period:
                query += " AND period = ?"
                params.append(period)
            cur = self.conn.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def save_tp_advisory(self, tenant_id: str, advisory: dict) -> None:
        with self._tenant_scope(tenant_id) as tid:
            self.conn.execute("""
                INSERT OR REPLACE INTO tp_advisories
                    (tenant_id, advisory_id, txn_id, suggested_method,
                     suggested_benchmark_low, suggested_benchmark_high,
                     comparable_set_summary, confidence, narrative)
                VALUES
                    (:tenant_id, :advisory_id, :txn_id, :suggested_method,
                     :suggested_benchmark_low, :suggested_benchmark_high,
                     :comparable_set_summary, :confidence, :narrative)
            """, {**advisory, "tenant_id": tid})
            self.conn.commit()

    def get_tp_documentation_status(self, tenant_id: str, period: str) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            cur = self.conn.execute(
                "SELECT * FROM tp_documentation WHERE tenant_id = ? AND period = ?",
                (tid, period)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ─────────────────────────────────────────────────────────────────────────
    # MODULE 3: TAX PROVISION
    # ─────────────────────────────────────────────────────────────────────────

    def save_tax_provision(self, tenant_id: str, provision: dict) -> None:
        with self._tenant_scope(tenant_id) as tid:
            self.conn.execute("""
                INSERT OR REPLACE INTO tax_provisions
                    (tenant_id, entity_code, period, jurisdiction,
                     pretax_income, statutory_rate_pct, permanent_differences,
                     temporary_differences, current_tax_expense,
                     deferred_tax_expense, total_tax_expense,
                     effective_tax_rate_pct, prior_year_true_up, status)
                VALUES
                    (:tenant_id, :entity_code, :period, :jurisdiction,
                     :pretax_income, :statutory_rate_pct, :permanent_differences,
                     :temporary_differences, :current_tax_expense,
                     :deferred_tax_expense, :total_tax_expense,
                     :effective_tax_rate_pct, :prior_year_true_up, :status)
            """, {**provision, "tenant_id": tid})
            self.conn.commit()

    def get_tax_provisions(self, tenant_id: str, period: Optional[str] = None) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            query = "SELECT * FROM tax_provisions WHERE tenant_id = ?"
            params = [tid]
            if period:
                query += " AND period = ?"
                params.append(period)
            cur = self.conn.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def save_deferred_tax_items(self, tenant_id: str, items: list[dict]) -> None:
        with self._tenant_scope(tenant_id) as tid:
            rows = [{**i, "tenant_id": tid} for i in items]
            self.conn.executemany("""
                INSERT OR REPLACE INTO deferred_tax_items
                    (tenant_id, item_id, entity_code, period, description,
                     category, opening_balance, movement, closing_balance,
                     is_asset, recognition_supportable)
                VALUES
                    (:tenant_id, :item_id, :entity_code, :period, :description,
                     :category, :opening_balance, :movement, :closing_balance,
                     :is_asset, :recognition_supportable)
            """, rows)
            self.conn.commit()

    # ─────────────────────────────────────────────────────────────────────────
    # MODULE 4: PILLAR TWO / BEPS
    # ─────────────────────────────────────────────────────────────────────────

    def save_etr_calculation(self, tenant_id: str, etr: dict) -> None:
        with self._tenant_scope(tenant_id) as tid:
            payload = {**etr, "tenant_id": tid}
            payload["entities_in_scope"] = json.dumps(etr.get("entities_in_scope", []))
            self.conn.execute("""
                INSERT OR REPLACE INTO pillar_two_etr
                    (tenant_id, jurisdiction, period, entities_in_scope,
                     globe_income, covered_taxes, jurisdictional_etr_pct,
                     minimum_rate_pct, top_up_tax_required, top_up_tax_amount,
                     safe_harbour_applied)
                VALUES
                    (:tenant_id, :jurisdiction, :period, :entities_in_scope,
                     :globe_income, :covered_taxes, :jurisdictional_etr_pct,
                     :minimum_rate_pct, :top_up_tax_required, :top_up_tax_amount,
                     :safe_harbour_applied)
            """, payload)
            self.conn.commit()

    def get_etr_calculations(self, tenant_id: str, period: Optional[str] = None) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            query = "SELECT * FROM pillar_two_etr WHERE tenant_id = ?"
            params = [tid]
            if period:
                query += " AND period = ?"
                params.append(period)
            cur = self.conn.execute(query, params)
            cols = [d[0] for d in cur.description]
            results = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                d["entities_in_scope"] = json.loads(d.get("entities_in_scope") or "[]")
                results.append(d)
            return results

    def save_qdmtt_filing(self, tenant_id: str, filing: dict) -> None:
        with self._tenant_scope(tenant_id) as tid:
            payload = {**filing, "tenant_id": tid}
            payload["allocated_entities"] = json.dumps(filing.get("allocated_entities", []))
            self.conn.execute("""
                INSERT OR REPLACE INTO qdmtt_filings
                    (tenant_id, filing_id, jurisdiction, period,
                     ultimate_parent_entity, top_up_tax_amount,
                     allocated_entities, status, due_date, filed_at)
                VALUES
                    (:tenant_id, :filing_id, :jurisdiction, :period,
                     :ultimate_parent_entity, :top_up_tax_amount,
                     :allocated_entities, :status, :due_date, :filed_at)
            """, payload)
            self.conn.commit()

    def save_etr_forecast(self, tenant_id: str, forecast: dict) -> None:
        with self._tenant_scope(tenant_id) as tid:
            self.conn.execute("""
                INSERT OR REPLACE INTO etr_forecasts
                    (tenant_id, forecast_id, jurisdiction, period,
                     forecasted_etr_pct, confidence, risk_flag, narrative)
                VALUES
                    (:tenant_id, :forecast_id, :jurisdiction, :period,
                     :forecasted_etr_pct, :confidence, :risk_flag, :narrative)
            """, {**forecast, "tenant_id": tid})
            self.conn.commit()

    # ─────────────────────────────────────────────────────────────────────────
    # SHARED: ESCALATIONS + AUDIT
    # ─────────────────────────────────────────────────────────────────────────

    def raise_escalation(self, tenant_id: str, escalation: dict) -> None:
        with self._tenant_scope(tenant_id) as tid:
            self.conn.execute("""
                INSERT INTO tax_escalations
                    (tenant_id, escalation_id, agent_name, entity_code, period,
                     escalation_type, description, amount_usd)
                VALUES
                    (:tenant_id, :escalation_id, :agent_name, :entity_code, :period,
                     :escalation_type, :description, :amount_usd)
            """, {**escalation, "tenant_id": tid})
            self.conn.commit()

    def get_open_escalations(self, tenant_id: str) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            cur = self.conn.execute(
                "SELECT * FROM tax_escalations WHERE tenant_id = ? AND resolved = 0 "
                "ORDER BY raised_at DESC",
                (tid,)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def append_audit_event(
        self, tenant_id: str, event_type: str, actor: str,
        agent_name: Optional[str] = None, entity_code: Optional[str] = None,
        period: Optional[str] = None, payload: Optional[dict] = None,
    ) -> None:
        """Hash-chained audit log — same pattern as Close Command's MCP audit server."""
        import hashlib
        with self._tenant_scope(tenant_id) as tid:
            cur = self.conn.execute(
                "SELECT event_hash FROM tax_audit_log WHERE tenant_id = ? "
                "ORDER BY id DESC LIMIT 1", (tid,)
            )
            row = cur.fetchone()
            prior_hash = row[0] if row else "GENESIS"

            payload_json = json.dumps(payload or {}, default=str, sort_keys=True)
            hash_input = f"{tid}|{event_type}|{actor}|{payload_json}|{prior_hash}"
            event_hash = hashlib.sha256(hash_input.encode()).hexdigest()

            self.conn.execute("""
                INSERT INTO tax_audit_log
                    (tenant_id, event_type, agent_name, entity_code, period,
                     actor, payload, prior_hash, event_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tid, event_type, agent_name, entity_code, period,
                  actor, payload_json, prior_hash, event_hash))
            self.conn.commit()

    def get_audit_log(self, tenant_id: str, limit: int = 200) -> list[dict]:
        with self._tenant_scope(tenant_id) as tid:
            cur = self.conn.execute(
                "SELECT * FROM tax_audit_log WHERE tenant_id = ? "
                "ORDER BY id DESC LIMIT ?", (tid, limit)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
