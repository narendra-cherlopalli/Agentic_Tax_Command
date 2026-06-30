"""
tax_command/governance/ownership.py

Named ownership for Tax Command's 4 agents — same pattern as Close
Command. Pipeline startup is blocked until every agent has a named
human owner. This is a hard governance requirement, not a suggestion:
AI-generated tax positions without a named accountable owner are not
defensible to a tax authority or auditor.
"""

from __future__ import annotations

# Per-tenant ownership — in production this is stored in the database
# and configured via a settings UI, not hardcoded. This module provides
# the shape and the validation function; replace AGENT_OWNERSHIP with a
# DB-backed lookup keyed on tenant_id before going to production.

AGENT_OWNERSHIP = {
    "vat": {
        "owner": "",            # e.g. "indirect.tax.lead@yourcompany.com"
        "role": "Indirect Tax Manager",
    },
    "transfer_pricing": {
        "owner": "",
        "role": "Transfer Pricing Director",
    },
    "tax_provision": {
        "owner": "",
        "role": "Group Tax Manager",
    },
    "pillar_two": {
        "owner": "",
        "role": "Head of Tax",
    },
}


def validate_ownership() -> list[str]:
    """
    Return a list of issue strings for any agent with no named owner.
    Empty list = all agents have an accountable human. Pipeline startup
    should call this and refuse to run if the list is non-empty.
    """
    issues = []
    for agent_name, info in AGENT_OWNERSHIP.items():
        if not info.get("owner", "").strip():
            issues.append(
                f"{agent_name}: no named owner set "
                f"(expected role: {info.get('role', 'unspecified')})"
            )
    return issues


def get_owner(agent_name: str) -> str:
    """Return the named owner for an agent, or empty string if unset."""
    return AGENT_OWNERSHIP.get(agent_name, {}).get("owner", "")
