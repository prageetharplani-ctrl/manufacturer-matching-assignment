import json
import unittest
from datetime import date

from tests.helpers import AS_OF, CFG, dataset, mfr, req
from veridia.config import ROOT
from veridia.data import load_dataset
from veridia.matching import canonical_json, run_match, why_not


def match(ds, r=None, as_of=AS_OF, held=None, limit=50):
    return run_match(r or req(), ds, CFG, as_of, held, limit)


def keys(res):
    return [m["key"] for m in res["matches"]]


class HardFilterTests(unittest.TestCase):
    def test_a_suitable_supplier_matches(self):
        self.assertEqual(keys(match(dataset(mfr("M1")))), ["M1"])

    def test_material_filter(self):
        self.assertEqual(keys(match(dataset(mfr("M1", materials=frozenset({"copper"}))))), [])

    def test_process_filter_and_cnc_group(self):
        ds = dataset(mfr("MILL", processes=frozenset({"CNC milling"})),
                     mfr("TURN", processes=frozenset({"CNC turning"})),
                     mfr("WELD", processes=frozenset({"welding"})))
        self.assertEqual(sorted(keys(match(ds))), ["MILL", "TURN"])
        self.assertEqual(keys(match(ds, req(process="welding"))), ["WELD"])
        self.assertEqual(keys(match(ds, req(process="grinding"))), [])

    def test_certification_must_be_held_and_all_of_them(self):
        ds = dataset(mfr("ONE"), mfr("BOTH", certifications=frozenset({"ISO 9001", "AS9100"})),
                     mfr("NONE", certifications=frozenset({"ISO 14001"})))
        self.assertEqual(sorted(keys(match(ds))), ["BOTH", "ONE"])
        self.assertEqual(keys(match(ds, req(certifications=["ISO 9001", "AS9100"]))), ["BOTH"])

    def test_no_certification_required_ignores_expiry(self):
        ds = dataset(mfr("OLD", cert_expiry=date(2020, 1, 1)), mfr("NODATE", cert_expiry=None))
        r = req(certifications=[])
        self.assertEqual(sorted(keys(match(ds, r))), ["NODATE", "OLD"])

    def test_lead_time_boundary_and_unknown(self):
        ds = dataset(mfr("EXACT", lead_time_weeks=6), mfr("LATE", lead_time_weeks=7), mfr("UNKNOWN", lead_time_weeks=None))
        self.assertEqual(keys(match(ds)), ["EXACT"])

    def test_order_range_boundaries(self):
        ds = dataset(mfr("LOW", min_order=1001), mfr("HIGH", max_order=999),
                     mfr("EDGE_LO", min_order=1000), mfr("EDGE_HI", max_order=1000))
        self.assertEqual(sorted(keys(match(ds))), ["EDGE_HI", "EDGE_LO"])

    def test_capacity_window_and_holds(self):
        small = mfr("SMALL", capacity_per_month=1000)   # 6 weeks -> ~1386 units
        self.assertEqual(keys(match(dataset(small))), ["SMALL"])
        self.assertEqual(keys(match(dataset(small), held={"SMALL": 500})), [])  # 886 left < 1000
        res = match(dataset(small), held={"SMALL": 300})
        self.assertEqual(keys(res), ["SMALL"])
        self.assertEqual(res["matches"][0]["warnings"][0]["code"], "CAPACITY_HELD_BY_OTHERS")
        too_small = mfr("TINY", capacity_per_month=500, min_order=100)
        self.assertEqual(keys(match(dataset(too_small))), [])


class TimingTests(unittest.TestCase):
    def test_expiry_day_is_still_valid_and_next_day_is_not(self):
        ds = dataset(mfr("M1", cert_expiry=date(2026, 12, 1)))
        self.assertEqual(keys(match(ds, as_of=date(2026, 12, 1))), ["M1"])
        self.assertEqual(keys(match(ds, as_of=date(2026, 12, 2))), [])

    def test_same_request_different_as_of_gives_different_but_stable_answers(self):
        ds = dataset(mfr("M1", cert_expiry=date(2026, 12, 1)))
        before = [canonical_json(match(ds, as_of=date(2026, 11, 1))) for _ in range(3)]
        after = [canonical_json(match(ds, as_of=date(2027, 1, 1))) for _ in range(3)]
        self.assertEqual(len(set(before)), 1)
        self.assertEqual(len(set(after)), 1)
        self.assertNotEqual(before[0], after[0])

    def test_missing_expiry_cannot_satisfy_a_certification_requirement(self):
        self.assertEqual(keys(match(dataset(mfr("M1", cert_expiry=None)))), [])

    def test_valid_today_but_expires_before_delivery_is_warned(self):
        # valid on as_of, but expires in 10 days while delivery takes 4 weeks
        ds = dataset(mfr("M1", cert_expiry=date(2026, 11, 7), lead_time_weeks=4))
        w = match(ds)["matches"][0]["warnings"]
        self.assertEqual([x["code"] for x in w], ["CERT_EXPIRES_BEFORE_DELIVERY"])

    def test_expiring_soon_is_warned_and_far_expiry_is_not(self):
        soon = dataset(mfr("M1", cert_expiry=date(2026, 12, 20), lead_time_weeks=2))
        self.assertEqual(match(soon)["matches"][0]["warnings"][0]["code"], "CERT_EXPIRING_SOON")
        self.assertEqual(match(dataset(mfr("M2")))["matches"][0]["warnings"], [])


class ScoringTests(unittest.TestCase):
    def test_faster_and_more_reliable_ranks_higher(self):
        ds = dataset(mfr("SLOW", lead_time_weeks=6), mfr("FAST", lead_time_weeks=2),
                     mfr("FLAKY", lead_time_weeks=2, on_time_pct=80.0))
        self.assertEqual(keys(match(ds)), ["FAST", "FLAKY", "SLOW"])

    def test_ties_break_by_key(self):
        ds = dataset(mfr("B2"), mfr("A1"), mfr("C3"))
        self.assertEqual(keys(match(ds)), ["A1", "B2", "C3"])

    def test_missing_on_time_data_is_not_penalised_and_weights_renormalise(self):
        res = match(dataset(mfr("NODATA", on_time_pct=None)))
        bd = res["matches"][0]["score_breakdown"]
        self.assertEqual(bd["delivery"]["effective_weight"], 0.0)
        self.assertAlmostEqual(sum(c["effective_weight"] for c in bd.values()), 1.0, places=3)
        self.assertAlmostEqual(sum(c["points"] for c in bd.values()), res["matches"][0]["score"], places=1)

    def test_every_result_explains_each_criterion(self):
        m = match(dataset(mfr("M1")))["matches"][0]
        self.assertEqual([r["criterion"] for r in m["reasons"]],
                         ["material", "process", "certification", "certificate_validity",
                          "lead_time", "order_range", "capacity"])


class DiagnosisAndWhyNotTests(unittest.TestCase):
    def setUp(self):
        self.ds = dataset(mfr("LATE", lead_time_weeks=9), mfr("LATER", lead_time_weeks=12),
                          mfr("WRONGMAT", materials=frozenset({"copper"})),
                          mfr("EXPIRED", cert_expiry=date(2026, 1, 1)))

    def test_zero_matches_returns_funnel_and_relaxations_not_an_empty_list(self):
        res = match(self.ds)
        self.assertEqual(res["total_matches"], 0)
        self.assertEqual([s["remaining"] for s in res["funnel"]], [4, 3, 3, 3, 2, 0, 0, 0])
        d = res["diagnosis"]
        self.assertEqual(d["most_restrictive_requirement"], "lead_time")
        self.assertEqual(d["smallest_relaxation"]["relax"], "deadline")
        self.assertEqual(d["smallest_relaxation"]["change"], {"deadline_weeks": 9})

    def test_relaxation_is_actually_valid_when_applied(self):
        res = match(self.ds)
        w = res["diagnosis"]["smallest_relaxation"]["change"]["deadline_weeks"]
        self.assertEqual(keys(match(self.ds, req(deadline_weeks=w))), ["LATE"])

    def test_quantity_relaxation_when_order_too_large(self):
        ds = dataset(mfr("M1", max_order=800))
        res = match(ds)
        q = [s for s in res["diagnosis"]["relaxations"] if s["relax"] == "quantity"][0]
        self.assertEqual(q["change"], {"quantity": 800})

    def test_nothing_matches_even_when_relaxed(self):
        res = match(dataset(mfr("M1", materials=frozenset({"copper"}), processes=frozenset({"welding"}))))
        self.assertEqual({s["relax"] for s in res["diagnosis"]["relaxations"]} & {"deadline", "quantity"}, set())

    def test_why_not_names_the_failing_criterion(self):
        out = why_not("LATE", req(), self.ds, CFG, AS_OF)
        self.assertEqual(out["manufacturers"][0]["failed_criteria"], ["lead_time"])
        out = why_not("EXPIRED", req(), self.ds, CFG, AS_OF)
        self.assertEqual(out["manufacturers"][0]["failed_criteria"], ["certificate_validity"])

    def test_why_not_unknown_id_suggests_near_matches_and_handles_duplicates(self):
        out = why_not("LAT", req(), self.ds, CFG, AS_OF)
        self.assertFalse(out["found"])
        self.assertIn("LATE", out["did_you_mean"])
        ds = dataset(mfr("X1#1"), mfr("X1#2"))
        self.assertEqual(len(why_not("X1", req(), ds, CFG, AS_OF)["manufacturers"]), 2)


class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(ROOT / "data" / "veridia_csv.csv")

    def test_known_funnel_for_the_german_style_buyer(self):
        res = run_match(req(), self.ds, CFG, AS_OF, limit=1000)
        self.assertEqual([s["remaining"] for s in res["funnel"]], [1000, 241, 129, 50, 50, 34, 34, 34])

    def test_results_are_byte_identical_across_runs(self):
        runs = {canonical_json(run_match(req(), self.ds, CFG, AS_OF)) for _ in range(5)}
        self.assertEqual(len(runs), 1)

    def test_no_expired_or_unknown_lead_supplier_is_ever_returned(self):
        by = self.ds.by_key()
        res = run_match(req(deadline_weeks=104), self.ds, CFG, AS_OF, limit=1000)
        for m in res["matches"]:
            s = by[m["key"]]
            self.assertGreaterEqual(s.cert_expiry, AS_OF)
            self.assertIsNotNone(s.lead_time_weeks)

    def test_buyer_b_has_exactly_one_candidate(self):
        b = req(material="stainless steel", process="sheet metal", quantity=1500)
        self.assertEqual(keys(run_match(b, self.ds, CFG, AS_OF)), ["VM1006"])

    def test_json_serialisable(self):
        json.dumps(run_match(req(), self.ds, CFG, AS_OF))


if __name__ == "__main__":
    unittest.main()
