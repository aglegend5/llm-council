"""Tests for the HubSpot lead import."""

import asyncio
import json
import unittest
from unittest import mock

import httpx

from backend import hubspot, leads


def fake_hubspot(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/account-info/v3/details":
        return httpx.Response(200, json={"portalId": 246832354})
    if path == "/crm/v3/objects/companies/search":
        return httpx.Response(200, json={"results": [
            {"id": "1", "properties": {"name": "HaulerHero", "domain": "haulerhero.com",
                                        "numberofemployees": "60", "payfac_num_customers": "300"}},
            {"id": "2", "properties": {"name": "Walz Group", "domain": "walzgroupcpa.com"}},
            {"id": "3", "properties": {"name": "BGM", "domain": "bgm360.com"}},
        ]})
    if path == "/crm/v4/associations/companies/contacts/batch/read":
        return httpx.Response(200, json={"results": [
            {"from": {"id": "1"}, "to": [{"toObjectId": 11}]},
            {"from": {"id": "2"}, "to": [{"toObjectId": 22}]},
        ]})
    if path == "/crm/v3/objects/contacts/batch/read":
        ids = [i["id"] for i in json.loads(request.content)["inputs"]]
        people = {
            "11": {"firstname": "Pat", "lastname": "Doe", "jobtitle": "CEO", "hs_lead_status": "CONNECTED"},
            "22": {"firstname": "Lee", "lastname": "Walz", "jobtitle": "Partner"},
        }
        return httpx.Response(200, json={"results": [{"id": i, "properties": people[i]} for i in ids]})
    return httpx.Response(404)


class HubSpotImportTest(unittest.TestCase):
    def test_fetch_and_score(self):
        transport = httpx.MockTransport(fake_hubspot)
        real_client = httpx.AsyncClient
        with mock.patch.object(hubspot.httpx, "AsyncClient",
                               lambda **kw: real_client(transport=transport, **kw)):
            pulled = asyncio.run(hubspot.fetch_leads(limit=10, token="test"))

        by_name = {l["company_name"]: l for l in pulled}
        hero = by_name["HaulerHero"]
        self.assertEqual(hero["contact_name"], "Pat Doe")
        self.assertEqual(hero["engagement_stage"], "replied")
        self.assertEqual(hero["num_customers"], "300")
        self.assertEqual(hero["hubspot_url"], "https://app.hubspot.com/contacts/246832354/record/0-2/1")

        ranked = leads.score_leads(pulled)
        self.assertEqual([s["company_name"] for s in ranked], ["HaulerHero", "BGM", "Walz Group"])
        self.assertEqual(ranked[0]["vertical"], "field_service")
        self.assertEqual(ranked[0]["gpv_estimate"], 225_000_000)
        self.assertTrue(ranked[1]["needs_enrichment"])
        self.assertEqual(ranked[2]["tier"], "P")
        self.assertEqual(ranked[2]["next_best_asset"], "partner_referral_intro")

    def test_missing_token(self):
        with mock.patch.object(hubspot, "HUBSPOT_ACCESS_TOKEN", None):
            with self.assertRaises(hubspot.HubSpotError):
                asyncio.run(hubspot.fetch_leads())

    def test_bad_token(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(401))
        real_client = httpx.AsyncClient
        with mock.patch.object(hubspot.httpx, "AsyncClient",
                               lambda **kw: real_client(transport=transport, **kw)):
            with self.assertRaisesRegex(hubspot.HubSpotError, "401"):
                asyncio.run(hubspot.fetch_leads(token="bad"))


class LeadKindTest(unittest.TestCase):
    def test_channel_partners(self):
        for name, domain in [("Ansvar Capital", "ansvarcapital.com"), ("Walz Group", "walzgroupcpa.com"),
                             ("Business Acquisitions", "businessacquisition.com")]:
            self.assertEqual(leads.detect_lead_kind({"company_name": name, "website": domain}), "channel_partner")

    def test_more_channel_partners(self):
        for name, domain in [("Trailhead Partners", "trailhead-partners.com"),
                             ("Susquehanna Growth Equity", "sgep.com"),
                             ("Exit Strategies Group", "exitstrategiesgroup.com")]:
            self.assertEqual(leads.detect_lead_kind({"company_name": name, "website": domain}), "channel_partner")

    def test_payments_providers_are_excluded(self):
        for name, domain in [("Finix", "finix.com"), ("Fortis", "fortispay.com"), ("NMI", "nmi.com")]:
            scored = leads.score_lead({"company_name": name, "website": domain})
            self.assertEqual(scored["lead_kind"], "payments_provider")
            self.assertEqual(scored["tier"], "X")
            self.assertIsNone(scored["next_best_asset"])

    def test_generic_rental_falls_back_to_rental_vertical(self):
        lead = {"company_name": "TapGoods", "keywords": "rental management software for the rental industry"}
        self.assertEqual(leads.detect_vertical(lead), "equipment_rental")

    def test_platforms(self):
        for name, domain in [("SmartMoving", "smartmoving.com"), ("FieldServio", "fieldservio.com")]:
            self.assertEqual(leads.detect_lead_kind({"company_name": name, "website": domain}), "platform")

    def test_explicit_override(self):
        self.assertEqual(leads.detect_lead_kind({"company_name": "Acme Capital", "vertical": "equipment_rental"}), "platform")
