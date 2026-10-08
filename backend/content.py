"""Council-generated outreach content for scored PayFacaaS leads.

Each scored lead becomes a content brief. The brief runs through the normal
3-stage council: every model drafts the asset, the drafts are peer-ranked
anonymously, and the chairman ships the strongest final version.
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .council import run_full_council
from .leads import ASSET_TYPES, VERTICALS, score_lead

CONTENT_DIR = "data/content"

COMPANY_PROFILE = """Solvent Dynamics LLC is a FinTech-informed growth equity operator. It pairs growth capital with a proprietary, white-label Payment Facilitator (PayFac) engine that is integrated natively into a software platform's existing billing workflow at Solvent Dynamics' expense.
- What gets integrated: sub-merchant onboarding and underwriting, PCI-DSS compliance, risk engine, deposit routing, recurring billing / auto-pay, instant payouts.
- What it replaces: low-yield ISO / referral processing arrangements (e.g., TSYS, Clover Connect, basic Stripe Connect).
- Typical outcome modeled: 60-90 bps of net payment yield captured with no price increase to the platform's end customers.
- Deal structure: growth capital ($2M-$10M) plus the PayFac engine, in exchange for a performance-vested equity stake (typically 20%-30%) and a board seat. Revenue share on processing is tied to the equity agreement.
- Contact: Antwon Gordon, Solvent Dynamics LLC, solventdynamic@gmail.com, solventdynamics.com"""

GUARDRAILS = """Rules every draft must follow:
- Use only the figures in the LEAD ANALYSIS. Present them as estimates ("we estimate", "roughly", "based on public data") and never as guarantees.
- Do not invent customer names, case studies, testimonials, logos, or past deal results.
- Lead with the prospect's world (their vertical's payment pain), not with Solvent Dynamics.
- Be specific and plain-spoken. No hype words ("revolutionary", "game-changing"), no fake urgency.
- One clear, low-friction call to action (e.g., a 20-minute call to walk through a free payment-yield audit).
- Emails must include a simple opt-out line and the sender's real contact details.
- Output only the finished asset in markdown, ready to send."""


def _money(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:,.0f}"


def lead_analysis_text(scored: Dict[str, Any]) -> str:
    """Render the scored lead as the analysis block the council writes from."""
    lead = scored["lead"]
    econ = scored["economics"]
    lines = [
        f"Company: {scored['company_name']}",
        f"Website: {lead.get('website', 'n/a')}",
        f"Vertical: {scored['vertical_label']}",
        f"Contact: {lead.get('contact_name', 'n/a')} ({lead.get('contact_title', 'title unknown')})",
        f"Ownership: {scored['ownership']}",
        f"Current payment model: {scored['payment_model']} (~{econ['current_yield_bps']} bps net yield)",
        f"Estimated GPV: {_money(scored['gpv_estimate'])} (method: {scored['gpv_method']}; confidence: {scored['gpv_confidence']})",
        f"Modeled PayFac net take rate: {econ['target_yield_bps']} bps",
        f"Estimated incremental payments revenue: {_money(econ['incremental_payments_revenue'])}/yr",
        f"Estimated net EBITDA added to the platform: {_money(econ['net_ebitda_added'])}/yr",
        f"Illustrative enterprise value uplift at 10x EBITDA: {_money(econ['ev_uplift'])}",
        f"Buying signals: {', '.join(scored['signals']) or 'none recorded'}",
        f"Message angle for this contact: {scored['persona_angle']}",
        f"Lead score: {scored['score']}/100 (tier {scored['tier']})",
    ]
    if lead.get("notes"):
        lines.append(f"Notes from lead platform: {lead['notes']}")
    return "\n".join(lines)


def build_content_brief(scored: Dict[str, Any], asset_type: str, extra_instructions: str = "") -> str:
    """Build the full prompt the council members draft from."""
    asset_description = ASSET_TYPES[asset_type]
    pain = VERTICALS[scored["vertical"]]["pain"]
    brief = f"""You are a B2B FinTech copywriter producing outreach content for Solvent Dynamics.

ABOUT SOLVENT DYNAMICS:
{COMPANY_PROFILE}

LEAD ANALYSIS (from our predictive scoring model):
{lead_analysis_text(scored)}

The prospect's vertical typically struggles with: {pain}.

TASK: Write {asset_description} addressed to this contact. The goal is to earn their attention by showing, credibly and specifically, how embedded payments could become a revenue line for their platform, and that Solvent Dynamics is a partner that carries the build, compliance and risk burden for them.

{GUARDRAILS}"""
    if extra_instructions.strip():
        brief += f"\n\nAdditional instructions from the sender:\n{extra_instructions.strip()}"
    return brief


async def generate_content(
    lead: Dict[str, Any],
    asset_type: Optional[str] = None,
    extra_instructions: str = "",
) -> Dict[str, Any]:
    """Score a lead, run the council on its content brief, and save the result."""
    scored = score_lead(lead)
    asset_type = asset_type or scored["next_best_asset"]
    if asset_type not in ASSET_TYPES:
        raise ValueError(f"Unknown asset_type '{asset_type}'. Choose one of: {', '.join(ASSET_TYPES)}")

    brief = build_content_brief(scored, asset_type, extra_instructions)
    stage1, stage2, stage3, metadata = await run_full_council(brief)

    record = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.utcnow().isoformat(),
        "company_name": scored["company_name"],
        "asset_type": asset_type,
        "scored_lead": scored,
        "brief": brief,
        "stage1": stage1,
        "stage2": stage2,
        "stage3": stage3,
        "metadata": metadata,
    }
    save_content(record)
    return record


def save_content(record: Dict[str, Any]) -> None:
    Path(CONTENT_DIR).mkdir(parents=True, exist_ok=True)
    with open(os.path.join(CONTENT_DIR, f"{record['id']}.json"), "w") as f:
        json.dump(record, f, indent=2)


def list_content() -> List[Dict[str, Any]]:
    """List saved content (summary only), newest first."""
    if not os.path.isdir(CONTENT_DIR):
        return []
    items = []
    for name in os.listdir(CONTENT_DIR):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(CONTENT_DIR, name)) as f:
            record = json.load(f)
        items.append({
            "id": record["id"],
            "created_at": record["created_at"],
            "company_name": record["company_name"],
            "asset_type": record["asset_type"],
            "score": record["scored_lead"]["score"],
            "tier": record["scored_lead"]["tier"],
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def get_content(content_id: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(CONTENT_DIR, f"{content_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)
