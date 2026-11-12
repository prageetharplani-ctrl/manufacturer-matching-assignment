import os
import tempfile
import unittest
from datetime import date

from tests.helpers import AS_OF, GOOD_ROW, csv_text
from veridia.config import ROOT
from veridia.data import DataError, load_dataset, load_text

REAL = ROOT / "data" / "veridia_csv.csv"


def codes(ds, code):
    return [i for i in ds.issues if i.code == code]


class DirtyDataTests(unittest.TestCase):
    def test_spelling_and_case_variants_are_normalised_and_reported(self):
        ds = load_text(csv_text({"manufacturer_id": "A", "materials": " Aluminum ; STAINLESS ; al",
                                 "processes": "sheet-metal;cnc machining"}))
        m = ds.manufacturers[0]
        self.assertEqual(m.materials, {"aluminium", "stainless steel"})
        self.assertEqual(m.processes, {"sheet metal", "CNC milling", "CNC turning"})
        self.assertTrue(codes(ds, "TOKEN_NORMALISED"))

    def test_unknown_token_is_dropped_but_row_kept(self):
        ds = load_text(csv_text({"materials": "aluminium;unobtainium"}))
        self.assertEqual(len(ds.manufacturers), 1)
        self.assertEqual(ds.manufacturers[0].materials, {"aluminium"})
        self.assertEqual(len(codes(ds, "UNKNOWN_TOKEN")), 1)

    def test_same_id_on_different_companies_gets_unique_keys(self):
        ds = load_text(csv_text({"manufacturer_id": "X1", "name": "One"}, {"manufacturer_id": "X1", "name": "Two"}))
        self.assertEqual([m.key for m in ds.manufacturers], ["X1#1", "X1#2"])
        self.assertEqual(len(codes(ds, "DUPLICATE_ID")), 2)
        self.assertTrue(all("DUPLICATE_ID" in m.flags for m in ds.manufacturers))

    def test_exact_duplicate_row_is_excluded_and_reported(self):
        ds = load_text(csv_text({}, {}))
        self.assertEqual(len(ds.manufacturers), 1)
        self.assertEqual(len(codes(ds, "DUPLICATE_ROW")), 1)

    def test_blank_lead_time_is_kept_but_unknown(self):
        ds = load_text(csv_text({"lead_time_weeks": "", "lead_time_days": ""}))
        self.assertIsNone(ds.manufacturers[0].lead_time_weeks)
        self.assertEqual(len(codes(ds, "LEAD_TIME_MISSING")), 1)

    def test_lead_time_derived_from_days_when_weeks_blank(self):
        ds = load_text(csv_text({"lead_time_weeks": "", "lead_time_days": "22"}))
        self.assertEqual(ds.manufacturers[0].lead_time_weeks, 4)  # ceil(22/7)
        self.assertEqual(len(codes(ds, "LEAD_TIME_FROM_DAYS")), 1)

    def test_negative_lead_time_is_invalid_not_trusted(self):
        ds = load_text(csv_text({"lead_time_weeks": "-3", "lead_time_days": ""}))
        self.assertIsNone(ds.manufacturers[0].lead_time_weeks)
        self.assertEqual(len(codes(ds, "LEAD_TIME_INVALID")), 1)

    def test_impossible_on_time_percentage_is_ignored_not_clipped(self):
        ds = load_text(csv_text({"on_time_delivery_pct": "142.6"}))
        self.assertIsNone(ds.manufacturers[0].on_time_pct)
        self.assertIn("OTD_OUT_OF_RANGE", ds.manufacturers[0].flags)

    def test_malformed_expiry_date_is_flagged_never_guessed(self):
        ds = load_text(csv_text({"certification_expiry": "31/12/2027"}))
        self.assertIsNone(ds.manufacturers[0].cert_expiry)
        self.assertEqual(len(codes(ds, "CERT_EXPIRY_INVALID")), 1)

    def test_shared_expiry_across_two_certs_is_flagged(self):
        ds = load_text(csv_text({"certifications": "ISO 9001;AS9100"}))
        self.assertIn("CERT_EXPIRY_SHARED", ds.manufacturers[0].flags)

    def test_unusable_rows_are_excluded_with_a_reason(self):
        ds = load_text(csv_text(
            {"manufacturer_id": ""},
            {"manufacturer_id": "B", "capacity_units_per_month": "0"},
            {"manufacturer_id": "C", "capacity_units_per_month": "lots"},
            {"manufacturer_id": "D", "min_order_qty": "900", "max_order_qty": "100"},
            {"manufacturer_id": "OK"}))
        self.assertEqual([m.key for m in ds.manufacturers], ["OK"])
        self.assertEqual(ds.quality_report()["rows_excluded"], 4)
        self.assertEqual({i.code for i in ds.issues}, {"MISSING_ID", "INVALID_CAPACITY", "INVALID_ORDER_RANGE"})

    def test_min_order_above_capacity_is_flagged_only(self):
        ds = load_text(csv_text({"min_order_qty": "5000", "max_order_qty": "9000", "capacity_units_per_month": "1500"}))
        self.assertEqual(len(ds.manufacturers), 1)
        self.assertIn("MIN_ORDER_EXCEEDS_CAPACITY", ds.manufacturers[0].flags)

    def test_bom_and_windows_line_endings(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
            f.write(csv_text({}, bom=True, crlf=True).encode("utf-8"))
        try:
            ds = load_dataset(f.name)  # BOM must not corrupt the first header name
        finally:
            os.unlink(f.name)
        self.assertEqual(len(ds.manufacturers), 1)
        self.assertEqual(ds.manufacturers[0].key, "VM9001")

    def test_missing_columns_and_bad_encoding_raise_clear_errors(self):
        with self.assertRaises(DataError):
            load_text("manufacturer_id,name\nA,B\n")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
            f.write(b"\xff\xfe\x00bad")
        try:
            with self.assertRaises(DataError):
                load_dataset(f.name)
        finally:
            os.unlink(f.name)
        with self.assertRaises(DataError):
            load_dataset("/no/such/file.csv")

    def test_quality_report_counts_match_issues(self):
        ds = load_text(csv_text({"lead_time_weeks": "", "lead_time_days": ""}, {"manufacturer_id": "Z"}))
        rep = ds.quality_report()
        self.assertEqual(rep["rows_read"], 2)
        self.assertEqual({c["code"]: c["count"] for c in rep["issue_counts"]}, {"LEAD_TIME_MISSING": 1})


class RealDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(REAL)

    def test_known_findings_from_profiling(self):
        counts = {c["code"]: c["count"] for c in self.ds.quality_report()["issue_counts"]}
        self.assertEqual(len(self.ds.manufacturers), 1000)
        self.assertEqual(len({m.key for m in self.ds.manufacturers}), 1000)
        self.assertEqual(counts["DUPLICATE_ID"], 20)
        self.assertEqual(counts["LEAD_TIME_MISSING"], 10)
        self.assertEqual(counts["OTD_OUT_OF_RANGE"], 10)
        self.assertEqual(counts["CERT_EXPIRY_SHARED"], 346)
        self.assertEqual(counts["MIN_ORDER_EXCEEDS_CAPACITY"], 13)

    def test_three_certificates_are_expired_on_the_reference_date(self):
        self.assertEqual(sum(1 for m in self.ds.manufacturers if m.cert_expiry < AS_OF), 3)

    def test_dataset_hash_is_stable(self):
        self.assertEqual(self.ds.sha256, load_dataset(REAL).sha256)


if __name__ == "__main__":
    unittest.main()
