"""Predictive lead scoring for PayFac-as-a-Service prospects.

Takes a lead as exported from a lead-generation platform (Apollo, HubSpot,
ZoomInfo, Grata, etc.), estimates its Gross Processing Volume (GPV), models the
embedded-payments yield it is leaving on the table, and scores how well it fits
the Solvent Dynamics target profile. Everything here is deterministic so the
numbers that feed the council's content prompts are auditable.
"""

import csv
import io
import re
from typing import Any, Dict, List, Optional

# Target GPV band for the PayFacaaS thesis
GPV_MIN = 150_000_000
GPV_MAX = 1_000_000_000

# Enterprise value multiple applied to added EBITDA
EV_MULTIPLE = 10

# Per-vertical assumptions used when the lead export has no GPV figure.
#   per_customer_gpv: average annual processing per business customer on the platform
#   revenue_to_gpv:   GPV as a multiple of the software company's own revenue
#   avg_ticket:       typical invoice / transaction size
#   fit:              0-1 strategic fit with the PayFac thesis
VERTICALS: Dict[str, Dict[str, Any]] = {
    "equipment_rental": {
        "label": "Equipment & Heavy Machinery Rental",
        "per_customer_gpv": 1_000_000,
        "revenue_to_gpv": 15,
        "avg_ticket": 2_500,
        "fit": 1.0,
        "pain": "long-term rentals, recurring ACH/card billing, deposits and chargeback management",
    },
    "event_rental": {
        "label": "Event & Party Rental",
        "per_customer_gpv": 600_000,
        "revenue_to_gpv": 12,
        "avg_ticket": 1_200,
        "fit": 0.95,
        "pain": "heavy card usage, security-deposit holds and seasonal volume spikes",
    },
    "field_service": {
        "label": "Field Service & Commercial Asset Maintenance",
        "per_customer_gpv": 750_000,
        "revenue_to_gpv": 12,
        "avg_ticket": 900,
        "fit": 0.9,
        "pain": "on-site card-present payments combined with back-office invoicing",
    },
    "isp_telecom": {
        "label": "Regional ISP / Recurring-Billing Infrastructure",
        "per_customer_gpv": 2_000_000,
        "revenue_to_gpv": 10,
        "avg_ticket": 90,
        "fit": 0.75,
        "pain": "high-frequency auto-pay subscriptions and failed-payment recovery",
    },
    "property_management": {
        "label": "Property / Self-Storage Management",
        "per_customer_gpv": 1_500_000,
        "revenue_to_gpv": 20,
        "avg_ticket": 1_500,
        "fit": 0.8,
        "pain": "recurring rent collection, deposits and ACH-heavy receivables",
    },
    "other_vertical_saas": {
        "label": "Other Vertical SaaS",
        "per_customer_gpv": 500_000,
        "revenue_to_gpv": 10,
        "avg_ticket": 1_000,
        "fit": 0.5,
        "pain": "B2B invoicing and recurring billing",
    },
}

VERTICAL_KEYWORDS = [
    ("equipment_rental", ["equipment", "machinery", "heavy", "construction", "tool rental", "crane", "scaffold", "dumpster", "rental yard", "rental software"]),
    ("event_rental", ["event", "party", "tent", "wedding", "av rental", "staging"]),
    ("field_service", ["field service", "fieldserv", "hvac", "plumbing", "maintenance", "dispatch", "technician", "pest", "landscap", "moving", "hauler", "waste", "junk removal", "solar"]),
    ("isp_telecom", ["isp", "internet service", "broadband", "telecom", "wireless", "fiber"]),
    ("property_management", ["property", "storage", "real estate", "leasing", "hoa"]),
    # Generic fallback so "rental management software" etc. still lands in a rental vertical
    ("equipment_rental", ["rental"]),
]

# Firms that refer platforms rather than process payments themselves
# (accountants, M&A advisors, brokers, capital providers)
CHANNEL_PARTNER_KEYWORDS = [
    "cpa", "accounting", "accountant", "audit", "capital", "acquisition", "advisor",
    "advisory", "m&a", "broker", "investment", "wealth", "equity", "partners", "venture",
    "exit", "merger", "securities", "banker", "investor", "endowment", "corporate finance",
    "business sales", "closers",
]

# Payment processors / PayFacs / payroll: competitors or infrastructure, never pitch targets
PAYMENTS_PROVIDER_KEYWORDS = [
    "finix", "fortispay", "nmi.com", "xplorpay", "fullsteam", "ingenico", "paychex", "gusto",
    "stripe.com", "adyen", "payrix", "worldpay", "fiserv", "tsys", "heartland", "paysafe",
]

# Net bps the platform currently keeps, by payment model
CURRENT_YIELD_BPS = {
    "none": 0,
    "iso_referral": 15,
    "stripe_connect_standard": 20,
    "payfac_lite": 40,
    "full_payfac": 70,
}

PROCESSOR_HINTS = [
    ("full_payfac", ["payfac", "own merchant", "registered payfac"]),
    ("payfac_lite", ["finix", "payrix", "infinicept", "stripe connect custom", "adyen for platforms"]),
    ("stripe_connect_standard", ["stripe", "square", "paypal", "braintree"]),
    ("iso_referral", ["tsys", "clover", "fiserv", "first data", "worldpay", "heartland", "global payments", "elavon", "authorize.net", "iso", "referral"]),
]

OWNERSHIP_SCORES = {
    "founder": 1.0,
    "bootstrapped": 1.0,
    "lmm_pe": 0.9,
    "pe": 0.8,
    "vc": 0.55,
    "public": 0.2,
    "unknown": 0.6,
}

# Buying signals a lead platform can surface, with their weight (points out of 10)
SIGNAL_WEIGHTS = {
    "hiring_payments": 4,
    "recent_funding": 3,
    "new_cfo": 3,
    "acquisition_activity": 3,
    "succession_planning": 3,
    "processor_contract_renewal": 4,
    "pricing_page_payments": 2,
    "website_visit": 2,
    "content_engagement": 2,
}

# Engagement stage → the next piece of content most likely to move the lead forward
NEXT_BEST_CONTENT = {
    "new": "cold_email",
    "contacted": "linkedin_message",
    "opened": "follow_up_sequence",
    "clicked": "yield_audit_one_pager",
    "replied": "yield_audit_one_pager",
    "meeting_booked": "board_memo",
    "pitched": "board_memo",
}

ASSET_TYPES = {
    "cold_email": "a first-touch cold email (subject line + under 140 words)",
    "linkedin_message": "a LinkedIn connection note (under 300 characters) and a follow-up DM (under 120 words)",
    "follow_up_sequence": "a 3-touch follow-up email sequence spaced over 10 business days, each under 110 words with its own subject line",
    "yield_audit_one_pager": "a one-page 'Hidden Payment Yield Audit' summary in markdown (headline, current state, opportunity math, how it works, deal structure, next step)",
    "board_memo": "a concise board-level investment memo (under 500 words) presenting the Capital + PayFac Engine thesis",
    "partner_referral_intro": "a short email (under 150 words) to an accounting or M&A advisory firm inviting them to refer vertical SaaS clients for a free payment-yield audit, proposing a call to discuss a referral arrangement",
}


def _to_number(value: Any) -> Optional[float]:
    """Parse numbers like '$12.5M', '1,200', '250k', '1.2B' into floats."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", "").replace("$", "")
    if not text:
        return None
    # Ranges like "10-50" or "$10M - $50M": take the midpoint
    range_match = re.match(r"^([\d.]+\s*[kmb]?)\s*(?:-|to)\s*([\d.]+\s*[kmb]?)", text)
    if range_match:
        low, high = _to_number(range_match.group(1)), _to_number(range_match.group(2))
        if low is not None and high is not None:
            return (low + high) / 2
    match = re.match(r"^([\d.]+)\s*([kmb])?", text)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}.get(match.group(2) or "", 1)
    return number * multiplier


def detect_vertical(lead: Dict[str, Any]) -> str:
    """Infer the vertical key from an explicit field or from industry/keywords text."""
    explicit = (lead.get("vertical") or "").strip().lower()
    if explicit in VERTICALS:
        return explicit
    haystack = " ".join(
        str(lead.get(k) or "") for k in ("vertical", "industry", "keywords", "description", "company_name")
    ).lower()
    for key, words in VERTICAL_KEYWORDS:
        if any(word in haystack for word in words):
            return key
    return "other_vertical_saas"


LEAD_KINDS = ("platform", "channel_partner", "payments_provider")


def detect_lead_kind(lead: Dict[str, Any]) -> str:
    """'platform' (software company with GPV), 'channel_partner' (refers platforms) or 'payments_provider'."""
    explicit = (lead.get("lead_kind") or "").strip().lower()
    if explicit in LEAD_KINDS:
        return explicit
    if lead.get("vertical") or lead.get("gpv") or lead.get("num_customers"):
        return "platform"
    haystack = " ".join(
        str(lead.get(k) or "") for k in ("company_name", "website", "industry")
    ).lower()
    # Substring match: lead-platform domains run words together (e.g. walzgroupcpa.com)
    if any(word in haystack for word in PAYMENTS_PROVIDER_KEYWORDS):
        return "payments_provider"
    if any(word in haystack for word in CHANNEL_PARTNER_KEYWORDS):
        return "channel_partner"
    return "platform"


def detect_payment_model(lead: Dict[str, Any]) -> str:
    """Infer the current payment model from an explicit field or the processor/tech stack."""
    explicit = (lead.get("payment_model") or "").strip().lower()
    if explicit in CURRENT_YIELD_BPS:
        return explicit
    haystack = " ".join(
        str(lead.get(k) or "") for k in ("payment_model", "current_processor", "technologies")
    ).lower()
    for key, words in PROCESSOR_HINTS:
        if any(word in haystack for word in words):
            return key
    # Most LMM vertical SaaS platforms without a known stack run referral deals
    return "iso_referral"


def target_take_rate_bps(gpv: float) -> int:
    """Net PayFac take rate after interchange and network fees, by GPV tier."""
    if gpv < 300_000_000:
        return 75
    if gpv < 750_000_000:
        return 70
    return 65


def partner_share(gpv: float) -> float:
    """Solvent Dynamics' PayFacaaS revenue share, by GPV tier."""
    if gpv < 300_000_000:
        return 0.25
    if gpv < 750_000_000:
        return 0.20
    return 0.15


def estimate_gpv(lead: Dict[str, Any], vertical: str) -> Dict[str, Any]:
    """Estimate GPV, preferring hard data over proxies. Returns value + method + confidence."""
    assumptions = VERTICALS[vertical]

    gpv = _to_number(lead.get("gpv"))
    if gpv:
        return {"gpv": gpv, "method": "reported GPV", "confidence": "high"}

    customers = _to_number(lead.get("num_customers"))
    if customers:
        per_customer = _to_number(lead.get("avg_processing_per_customer")) or assumptions["per_customer_gpv"]
        return {
            "gpv": customers * per_customer,
            "method": f"{int(customers):,} customers x ${per_customer:,.0f} avg annual processing",
            "confidence": "medium",
        }

    revenue = _to_number(lead.get("annual_revenue"))
    if revenue:
        multiple = assumptions["revenue_to_gpv"]
        return {
            "gpv": revenue * multiple,
            "method": f"${revenue:,.0f} software revenue x {multiple}x {assumptions['label']} GPV multiple",
            "confidence": "low",
        }

    employees = _to_number(lead.get("employees"))
    if employees:
        # ~$150k revenue per employee is typical for LMM vertical SaaS
        revenue = employees * 150_000
        multiple = assumptions["revenue_to_gpv"]
        return {
            "gpv": revenue * multiple,
            "method": f"{int(employees)} employees x $150k revenue/employee x {multiple}x GPV multiple",
            "confidence": "very low",
        }

    return {"gpv": 0.0, "method": "insufficient data", "confidence": "none"}


def model_economics(gpv: float, payment_model: str) -> Dict[str, Any]:
    """Model the incremental embedded-payments economics of moving to PayFacaaS."""
    target_bps = target_take_rate_bps(gpv) if gpv else 0
    current_bps = CURRENT_YIELD_BPS[payment_model]
    uplift_bps = max(target_bps - current_bps, 0)
    current_revenue = gpv * current_bps / 10_000
    target_revenue = gpv * target_bps / 10_000
    incremental_revenue = gpv * uplift_bps / 10_000
    share = partner_share(gpv) if gpv else 0
    partner_revenue = incremental_revenue * share
    net_ebitda = incremental_revenue - partner_revenue
    return {
        "current_yield_bps": current_bps,
        "target_yield_bps": target_bps,
        "uplift_bps": uplift_bps,
        "current_payments_revenue": round(current_revenue),
        "target_payments_revenue": round(target_revenue),
        "incremental_payments_revenue": round(incremental_revenue),
        "partner_share_pct": share,
        "partner_revenue": round(partner_revenue),
        "net_ebitda_added": round(net_ebitda),
        "ev_uplift": round(net_ebitda * EV_MULTIPLE),
    }


def _gpv_fit(gpv: float) -> float:
    """1.0 inside the target band, tapering off outside it."""
    if gpv <= 0:
        return 0.0
    if GPV_MIN <= gpv <= GPV_MAX:
        return 1.0
    if gpv < GPV_MIN:
        return max(gpv / GPV_MIN, 0.0) ** 1.5
    return max(1 - (gpv - GPV_MAX) / (4 * GPV_MAX), 0.2)


def _signals(lead: Dict[str, Any]) -> List[str]:
    raw = lead.get("signals") or []
    if isinstance(raw, str):
        raw = re.split(r"[;,|]", raw)
    return [s.strip().lower().replace(" ", "_") for s in raw if s and s.strip()]


def score_lead(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Score one lead and return its predicted economics, fit score, tier and content plan."""
    kind = detect_lead_kind(lead)
    if kind != "platform":
        return _score_non_platform(lead, kind)

    vertical = detect_vertical(lead)
    payment_model = detect_payment_model(lead)
    gpv_estimate = estimate_gpv(lead, vertical)
    gpv = gpv_estimate["gpv"]
    economics = model_economics(gpv, payment_model)

    ownership = (lead.get("ownership") or "unknown").strip().lower()
    ownership_fit = OWNERSHIP_SCORES.get(ownership, OWNERSHIP_SCORES["unknown"])
    signals = _signals(lead)
    signal_points = min(sum(SIGNAL_WEIGHTS.get(s, 1) for s in signals), 10)

    # Upside fit: how much of the 75 bps headroom is still uncaptured
    upside_fit = economics["uplift_bps"] / 75 if economics["target_yield_bps"] else 0

    breakdown = {
        "gpv_band": round(35 * _gpv_fit(gpv), 1),
        "vertical": round(20 * VERTICALS[vertical]["fit"], 1),
        "payment_upside": round(20 * min(upside_fit, 1), 1),
        "ownership": round(15 * ownership_fit, 1),
        "signals": float(signal_points),
    }
    score = round(sum(breakdown.values()))

    if score >= 75:
        tier = "A"
    elif score >= 60:
        tier = "B"
    elif score >= 45:
        tier = "C"
    else:
        tier = "D"

    stage = (lead.get("engagement_stage") or "new").strip().lower()
    next_asset = NEXT_BEST_CONTENT.get(stage, "cold_email")

    return {
        "lead": lead,
        "lead_kind": "platform",
        "needs_enrichment": gpv_estimate["confidence"] == "none",
        "company_name": lead.get("company_name") or "Unknown company",
        "vertical": vertical,
        "vertical_label": VERTICALS[vertical]["label"],
        "payment_model": payment_model,
        "ownership": ownership,
        "signals": signals,
        "gpv_estimate": round(gpv),
        "gpv_method": gpv_estimate["method"],
        "gpv_confidence": gpv_estimate["confidence"],
        "in_target_band": GPV_MIN <= gpv <= GPV_MAX,
        "economics": economics,
        "score": score,
        "score_breakdown": breakdown,
        "tier": tier,
        "persona_angle": persona_angle(lead, ownership),
        "engagement_stage": stage,
        "next_best_asset": next_asset,
    }


def _score_non_platform(lead: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Referral partners get partner content; payments providers are excluded from outreach."""
    stage = (lead.get("engagement_stage") or "new").strip().lower()
    if kind == "payments_provider":
        label, tier, asset = "Payments Provider (competitor / infrastructure)", "X", None
        angle = "not an outreach target"
    else:
        label, tier, asset = "Channel / Referral Partner", "P", "partner_referral_intro"
        angle = "a referral relationship that brings their vertical SaaS clients a free payment-yield audit and a new revenue line"
    return {
        "lead": lead,
        "lead_kind": kind,
        "needs_enrichment": False,
        "company_name": lead.get("company_name") or "Unknown company",
        "vertical": kind,
        "vertical_label": label,
        "payment_model": "n/a",
        "ownership": (lead.get("ownership") or "unknown").strip().lower(),
        "signals": _signals(lead),
        "gpv_estimate": 0,
        "gpv_method": "not a platform: no GPV of its own",
        "gpv_confidence": "n/a",
        "in_target_band": False,
        "economics": model_economics(0, "none"),
        "score": 0,
        "score_breakdown": {},
        "tier": tier,
        "persona_angle": angle,
        "engagement_stage": stage,
        "next_best_asset": asset,
    }


def persona_angle(lead: Dict[str, Any], ownership: str) -> str:
    """Pick the message angle most likely to resonate with this contact."""
    title = (lead.get("contact_title") or "").lower()
    if any(t in title for t in ("cfo", "finance", "controller")):
        return "new high-margin revenue line and cleaner payment reconciliation, with no capex"
    if any(t in title for t in ("cto", "engineering", "product", "vp product")):
        return "embedded payments shipped natively in the product without building PayFac infrastructure, compliance or a risk team in-house"
    if ownership in ("pe", "lmm_pe") or any(t in title for t in ("partner", "operating", "board", "chair")):
        return "EBITDA and enterprise-value expansion across the portfolio company ahead of exit"
    return "growth capital plus a new payments revenue line that raises enterprise value without raising software prices"


def score_leads(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Score many leads and return them ranked best-first."""
    scored = [score_lead(lead) for lead in leads]
    # Platforms with data first, then platforms needing enrichment, then partners, then providers
    scored.sort(key=lambda s: (
        s["lead_kind"] == "platform",
        s["lead_kind"] == "channel_partner",
        not s["needs_enrichment"],
        s["score"],
        s["economics"]["net_ebitda_added"],
    ), reverse=True)
    return scored


# Common column names from Apollo, HubSpot, ZoomInfo and Grata exports
CSV_COLUMN_MAP = {
    "company_name": ["company_name", "company", "company name", "account name", "organization name", "name"],
    "website": ["website", "company website", "domain", "website url"],
    "industry": ["industry", "industries", "sub-industry", "primary industry"],
    "keywords": ["keywords", "description", "short description", "company description", "seo description"],
    "vertical": ["vertical"],
    "employees": ["employees", "# employees", "number of employees", "employee count", "headcount"],
    "annual_revenue": ["annual_revenue", "annual revenue", "revenue", "estimated revenue", "revenue (in usd)"],
    "num_customers": ["num_customers", "customers", "customer count", "# customers", "locations"],
    "avg_processing_per_customer": ["avg_processing_per_customer"],
    "gpv": ["gpv", "gross processing volume", "processing volume", "payment volume"],
    "current_processor": ["current_processor", "processor", "payment processor"],
    "technologies": ["technologies", "tech stack", "technology"],
    "payment_model": ["payment_model"],
    "ownership": ["ownership", "ownership type", "funding status", "company type"],
    "contact_name": ["contact_name", "full name", "contact", "contact full name"],
    "first_name": ["first name", "first_name"],
    "last_name": ["last name", "last_name"],
    "contact_title": ["contact_title", "title", "job title"],
    "contact_email": ["contact_email", "email", "work email"],
    "engagement_stage": ["engagement_stage", "stage", "lead status", "lifecycle stage"],
    "signals": ["signals", "intent", "intent topics", "buying signals"],
}


def parse_leads_csv(csv_text: str) -> List[Dict[str, Any]]:
    """Normalize a lead-platform CSV export into lead dicts."""
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        return []
    header_lookup = {h.strip().lower(): h for h in reader.fieldnames if h}

    column_for: Dict[str, str] = {}
    for field, aliases in CSV_COLUMN_MAP.items():
        for alias in aliases:
            if alias in header_lookup:
                column_for[field] = header_lookup[alias]
                break

    leads = []
    for row in reader:
        lead = {field: (row.get(col) or "").strip() for field, col in column_for.items()}
        if not lead.get("contact_name") and (lead.get("first_name") or lead.get("last_name")):
            lead["contact_name"] = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
        lead.pop("first_name", None)
        lead.pop("last_name", None)
        if lead.get("ownership"):
            lead["ownership"] = _normalize_ownership(lead["ownership"])
        lead = {k: v for k, v in lead.items() if v}
        if lead.get("company_name"):
            leads.append(lead)
    return leads


def _normalize_ownership(value: str) -> str:
    v = value.lower()
    if "public" in v or "ipo" in v:
        return "public"
    if "lower" in v or "lmm" in v:
        return "lmm_pe"
    if "private equity" in v or v == "pe" or "pe-backed" in v or "buyout" in v:
        return "pe"
    if "venture" in v or "series" in v or "seed" in v or v == "vc":
        return "vc"
    if "bootstrap" in v:
        return "bootstrapped"
    if "founder" in v or "private" in v or "family" in v:
        return "founder"
    return v if v in OWNERSHIP_SCORES else "unknown"
