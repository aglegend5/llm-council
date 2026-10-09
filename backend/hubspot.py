"""Pull leads from HubSpot CRM into the PayFac Lead Engine.

Uses a HubSpot private app access token (HUBSPOT_ACCESS_TOKEN in .env) with the
crm.objects.companies.read and crm.objects.contacts.read scopes. Each company
becomes a lead; its first associated contact supplies the name, title and
engagement stage.
"""

from typing import Any, Dict, List, Optional

import httpx

from .config import HUBSPOT_ACCESS_TOKEN

HUBSPOT_API = "https://api.hubapi.com"

COMPANY_PROPERTIES = [
    "name", "domain", "website", "industry", "description", "numberofemployees",
    "annualrevenue", "lifecyclestage", "hs_lead_status", "type", "web_technologies",
    "hs_ideal_customer_profile",
]

# Optional custom company properties. Create them in HubSpot (Settings > Properties)
# to feed hard data into the model instead of estimates.
CUSTOM_COMPANY_PROPERTIES = {
    "gpv": "payfac_gpv",
    "num_customers": "payfac_num_customers",
    "current_processor": "payfac_current_processor",
    "ownership": "payfac_ownership",
    "signals": "payfac_signals",
    "vertical": "payfac_vertical",
}

CONTACT_PROPERTIES = ["firstname", "lastname", "jobtitle", "email", "hs_lead_status", "lifecyclestage"]

# HubSpot lead status → engine engagement stage
LEAD_STATUS_TO_STAGE = {
    "NEW": "new",
    "OPEN": "new",
    "ATTEMPTED_TO_CONTACT": "contacted",
    "IN_PROGRESS": "contacted",
    "BAD_TIMING": "contacted",
    "CONNECTED": "replied",
    "OPEN_DEAL": "meeting_booked",
}

PORTAL_RECORD_URL = "https://app.hubspot.com/contacts/{portal_id}/record/0-2/{company_id}"


class HubSpotError(Exception):
    pass


def company_to_lead(
    company: Dict[str, Any],
    contact: Optional[Dict[str, Any]] = None,
    portal_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a HubSpot company (and optional primary contact) to an engine lead dict."""
    props = company.get("properties") or {}
    contact_props = (contact or {}).get("properties") or {}

    lead: Dict[str, Any] = {
        "source": "hubspot",
        "hubspot_id": str(company.get("id")),
        "company_name": props.get("name") or props.get("domain"),
        "website": props.get("website") or props.get("domain"),
        "industry": props.get("industry"),
        "keywords": props.get("description"),
        "employees": props.get("numberofemployees"),
        "annual_revenue": props.get("annualrevenue"),
        "technologies": props.get("web_technologies"),
    }
    for field, prop in CUSTOM_COMPANY_PROPERTIES.items():
        if props.get(prop):
            lead[field] = props[prop]

    if contact_props:
        name = f"{contact_props.get('firstname') or ''} {contact_props.get('lastname') or ''}".strip()
        lead["contact_name"] = name or None
        lead["contact_title"] = contact_props.get("jobtitle")
        lead["contact_email"] = contact_props.get("email")

    status = contact_props.get("hs_lead_status") or props.get("hs_lead_status")
    if status:
        lead["engagement_stage"] = LEAD_STATUS_TO_STAGE.get(status.upper(), "new")

    if portal_id:
        lead["hubspot_url"] = PORTAL_RECORD_URL.format(portal_id=portal_id, company_id=lead["hubspot_id"])

    return {k: v for k, v in lead.items() if v not in (None, "")}


async def _request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> Dict[str, Any]:
    response = await client.request(method, f"{HUBSPOT_API}{path}", **kwargs)
    if response.status_code == 401:
        raise HubSpotError("HubSpot rejected the access token (401). Check HUBSPOT_ACCESS_TOKEN.")
    if response.status_code == 403:
        raise HubSpotError("HubSpot token is missing a scope (403). It needs crm.objects.companies.read and crm.objects.contacts.read.")
    if response.status_code >= 400:
        raise HubSpotError(f"HubSpot API error {response.status_code}: {response.text[:200]}")
    return response.json()


async def fetch_leads(
    limit: int = 100,
    lifecycle_stage: Optional[str] = None,
    token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch up to `limit` companies (newest first) with their primary contact, as leads."""
    token = token or HUBSPOT_ACCESS_TOKEN
    if not token:
        raise HubSpotError("HUBSPOT_ACCESS_TOKEN is not set. Add a HubSpot private app token to .env.")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    properties = COMPANY_PROPERTIES + list(CUSTOM_COMPANY_PROPERTIES.values())

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        account = await _request(client, "GET", "/account-info/v3/details")
        portal_id = str(account.get("portalId", ""))

        companies: List[Dict[str, Any]] = []
        after = None
        while len(companies) < limit:
            body: Dict[str, Any] = {
                "properties": properties,
                "limit": min(100, limit - len(companies)),
                "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
            }
            if lifecycle_stage:
                body["filterGroups"] = [{"filters": [
                    {"propertyName": "lifecyclestage", "operator": "EQ", "value": lifecycle_stage}
                ]}]
            if after:
                body["after"] = after
            page = await _request(client, "POST", "/crm/v3/objects/companies/search", json=body)
            companies.extend(page.get("results", []))
            after = (page.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

        contacts_by_company = await _fetch_primary_contacts(client, [c["id"] for c in companies])

    return [company_to_lead(c, contacts_by_company.get(str(c["id"])), portal_id) for c in companies]


async def _fetch_primary_contacts(client: httpx.AsyncClient, company_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Map company id → its first associated contact record."""
    first_contact: Dict[str, str] = {}
    for i in range(0, len(company_ids), 100):
        batch = company_ids[i:i + 100]
        data = await _request(
            client, "POST", "/crm/v4/associations/companies/contacts/batch/read",
            json={"inputs": [{"id": str(cid)} for cid in batch]},
        )
        for row in data.get("results", []):
            to = row.get("to") or []
            if to:
                first_contact[str(row["from"]["id"])] = str(to[0]["toObjectId"])

    contact_ids = list(set(first_contact.values()))
    contacts: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(contact_ids), 100):
        data = await _request(
            client, "POST", "/crm/v3/objects/contacts/batch/read",
            json={"properties": CONTACT_PROPERTIES, "inputs": [{"id": cid} for cid in contact_ids[i:i + 100]]},
        )
        for contact in data.get("results", []):
            contacts[str(contact["id"])] = contact

    return {company_id: contacts[cid] for company_id, cid in first_contact.items() if cid in contacts}
