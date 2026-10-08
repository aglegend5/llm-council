"""Tests for the predictive lead scoring engine."""

import unittest
from pathlib import Path

from backend import leads
from backend.content import build_content_brief


class EconomicsTest(unittest.TestCase):
    def test_tier_table_matches_thesis(self):
        # Gross take rate from zero baseline reproduces the published tier table
        for gpv, revenue, ebitda in [
            (150e6, 1_125_000, 843_750),
            (500e6, 3_500_000, 2_800_000),
            (1e9, 6_500_000, 5_525_000),
        ]:
            econ = leads.model_economics(gpv, "none")
            self.assertEqual(econ["incremental_payments_revenue"], revenue)
            self.assertEqual(econ["net_ebitda_added"], ebitda)
            self.assertEqual(econ["ev_uplift"], ebitda * 10)

    def test_iso_referral_counts_only_incremental_yield(self):
        econ = leads.model_economics(200e6, "iso_referral")
        self.assertEqual(econ["uplift_bps"], 60)
        self.assertEqual(econ["incremental_payments_revenue"], 1_200_000)

    def test_existing_payfac_has_no_upside(self):
        econ = leads.model_economics(500e6, "full_payfac")
        self.assertEqual(econ["uplift_bps"], 0)
        self.assertEqual(econ["net_ebitda_added"], 0)


class ParsingTest(unittest.TestCase):
    def test_number_parsing(self):
        self.assertEqual(leads._to_number("$14M"), 14e6)
        self.assertEqual(leads._to_number("1,200"), 1200)
        self.assertEqual(leads._to_number("250k"), 250e3)
        self.assertEqual(leads._to_number("$10M - $50M"), 30e6)
        self.assertIsNone(leads._to_number("n/a"))

    def test_csv_export_is_normalized(self):
        csv_text = (Path(__file__).parent.parent / "samples" / "sample_leads.csv").read_text()
        parsed = leads.parse_leads_csv(csv_text)
        self.assertEqual(len(parsed), 4)
        first = parsed[0]
        self.assertEqual(first["company_name"], "Summit Rental Systems")
        self.assertEqual(first["contact_name"], "Dana Reyes")
        self.assertEqual(first["ownership"], "founder")
        self.assertEqual(parsed[2]["ownership"], "lmm_pe")
        self.assertEqual(parsed[3]["ownership"], "vc")


class ScoringTest(unittest.TestCase):
    def setUp(self):
        csv_text = (Path(__file__).parent.parent / "samples" / "sample_leads.csv").read_text()
        self.ranked = leads.score_leads(leads.parse_leads_csv(csv_text))
        self.by_name = {s["company_name"]: s for s in self.ranked}

    def test_detection(self):
        summit = self.by_name["Summit Rental Systems"]
        self.assertEqual(summit["vertical"], "equipment_rental")
        self.assertEqual(summit["payment_model"], "iso_referral")
        self.assertEqual(summit["gpv_estimate"], 220_000_000)
        self.assertEqual(summit["gpv_confidence"], "medium")
        self.assertEqual(self.by_name["PartyGrid"]["vertical"], "event_rental")
        self.assertEqual(self.by_name["FieldPro Dispatch"]["vertical"], "field_service")

    def test_ranking_puts_in_band_rental_platform_first(self):
        self.assertEqual(self.ranked[0]["company_name"], "Summit Rental Systems")
        self.assertEqual(self.ranked[0]["tier"], "A")
        self.assertEqual(self.ranked[-1]["company_name"], "TinyTools")
        self.assertEqual(self.ranked[-1]["tier"], "D")

    def test_next_best_asset_follows_engagement(self):
        self.assertEqual(self.by_name["Summit Rental Systems"]["next_best_asset"], "cold_email")
        self.assertEqual(self.by_name["PartyGrid"]["next_best_asset"], "follow_up_sequence")
        self.assertEqual(self.by_name["FieldPro Dispatch"]["next_best_asset"], "yield_audit_one_pager")

    def test_persona_angle(self):
        self.assertIn("revenue line", self.by_name["PartyGrid"]["persona_angle"])
        self.assertIn("EBITDA", self.by_name["FieldPro Dispatch"]["persona_angle"])

    def test_brief_contains_numbers_and_guardrails(self):
        brief = build_content_brief(self.ranked[0], "cold_email")
        self.assertIn("Summit Rental Systems", brief)
        self.assertIn("$220.0M", brief)
        self.assertIn("never as guarantees", brief)
        self.assertIn("opt-out", brief)


if __name__ == "__main__":
    unittest.main()
