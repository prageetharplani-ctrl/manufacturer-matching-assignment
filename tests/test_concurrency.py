import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.helpers import AS_OF, BASE_REQ, CFG, dataset, mfr
from veridia.config import ROOT
from veridia.data import load_dataset
from veridia.holds import HoldRegistry, HoldRejected
from veridia.requirement import ValidationError
from veridia.service import NotFound, Service

REAL = load_dataset(ROOT / "data" / "veridia_csv.csv")
A = {"material": "aluminium", "process": "CNC machining", "quantity": 1000, "deadline_weeks": 6, "certifications": ["ISO 9001"]}
B = {"material": "stainless steel", "process": "sheet metal", "quantity": 1500, "deadline_weeks": 6, "certifications": ["ISO 9001"]}


def strip_ids(result):
    r = dict(result)
    r.pop("request_id", None)
    return json.dumps(r, sort_keys=True, default=str)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class HoldRegistryTests(unittest.TestCase):
    def test_many_threads_cannot_oversell_capacity(self):
        reg = HoldRegistry(60)
        wins, lock = [], threading.Lock()
        barrier = threading.Barrier(40)

        def worker(i):
            barrier.wait()
            try:
                reg.try_hold(f"buyer{i}", "S1", 100, 1000)
                with lock:
                    wins.append(i)
            except HoldRejected:
                pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(wins), 10)                      # 1000 / 100, never more
        self.assertEqual(sum(reg.snapshot().values()), 1000)

    def test_holds_expire(self):
        clock = FakeClock()
        reg = HoldRegistry(30, clock)
        reg.try_hold("a", "S1", 900, 1000)
        with self.assertRaises(HoldRejected):
            reg.try_hold("b", "S1", 900, 1000)
        clock.t += 31
        reg.try_hold("b", "S1", 900, 1000)                   # capacity is back

    def test_own_hold_is_replaced_not_stacked_and_release_works(self):
        reg = HoldRegistry(60)
        reg.try_hold("a", "S1", 600, 1000)
        reg.try_hold("a", "S1", 700, 1000)
        self.assertEqual(reg.snapshot(), {"S1": 700})
        self.assertEqual(reg.snapshot(exclude_owner="a"), {})
        self.assertEqual(reg.release_owner("a"), 1)
        self.assertEqual(reg.snapshot(), {})


class TwoBuyerScenarioTests(unittest.TestCase):
    def test_concurrent_matching_is_pure_and_matches_sequential_results(self):
        svc = Service(REAL, CFG)
        expected = {n: strip_ids(svc.match(r, owner=n)) for n, r in (("A", A), ("B", B))}
        with ThreadPoolExecutor(16) as pool:
            futs = [(n, pool.submit(svc.match, r, owner=n)) for _ in range(20) for n, r in (("A", A), ("B", B))]
        for n, f in futs:
            self.assertEqual(strip_ids(f.result()), expected[n])

    def test_both_buyers_racing_for_the_same_supplier_exactly_one_wins(self):
        for _ in range(30):
            svc = Service(REAL, CFG)
            svc.submit("A", A)
            svc.submit("B", B)
            outcome, barrier = {}, threading.Barrier(2)

            def go(name):
                barrier.wait()
                try:
                    svc.hold(name, "VM1006")
                    outcome[name] = "held"
                except HoldRejected:
                    outcome[name] = "rejected"

            ts = [threading.Thread(target=go, args=(n,)) for n in ("A", "B")]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(sorted(outcome.values()), ["held", "rejected"], msg=outcome)
            # 1000 + 1500 > ~2079 window: both can never be held
            self.assertLessEqual(sum(svc.holds.snapshot().values()), 2079)

    def test_loser_sees_the_reduced_capacity_and_a_way_forward(self):
        svc = Service(REAL, CFG)
        svc.submit("A", A)
        svc.hold("A", "VM1006")
        res = svc.submit("B", B)["result"]
        self.assertEqual(res["total_matches"], 0)
        self.assertEqual(res["diagnosis"]["smallest_relaxation"]["change"], {"deadline_weeks": 8})
        # A, the holder, still sees VM1006 as fully theirs; a third buyer sees the warning.
        self.assertIn("VM1006", [m["key"] for m in svc.match(A, owner="A", limit=100)["matches"]])
        c = svc.match(A, owner="C", limit=100)
        vm = [m for m in c["matches"] if m["key"] == "VM1006"][0]
        self.assertEqual(vm["held_by_others_units"], 1000)
        self.assertEqual(vm["warnings"][0]["code"], "CAPACITY_HELD_BY_OTHERS")

    def test_matching_alone_never_reserves_capacity(self):
        svc = Service(REAL, CFG)
        svc.submit("A", A)
        svc.submit("B", B)
        self.assertEqual(svc.holds.snapshot(), {})
        self.assertEqual(svc.match(B, owner="B")["total_matches"], 1)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.svc = Service(dataset(mfr("FAST", lead_time_weeks=2), mfr("SLOW", lead_time_weeks=6)), CFG)

    def test_revision_recomputes_and_diffs_inputs_and_results(self):
        r1 = self.svc.submit("s1", BASE_REQ)
        self.assertEqual(r1["revision"], 1)
        self.assertIsNone(r1["diff"])
        r2 = self.svc.submit("s1", {**BASE_REQ, "deadline_weeks": 3})
        self.assertEqual(r2["revision"], 2)
        self.assertEqual(r2["diff"]["requirement_changes"], [{"field": "deadline_weeks", "from": 6, "to": 3}])
        self.assertEqual(r2["diff"]["result_changes"]["removed"], ["SLOW"])
        self.assertEqual(r2["diff"]["result_changes"]["matches_after"], 1)
        self.assertEqual([h["revision"] for h in self.svc.history("s1")], [1, 2])

    def test_invalid_revision_leaves_session_untouched(self):
        self.svc.submit("s1", BASE_REQ)
        with self.assertRaises(ValidationError):
            self.svc.submit("s1", {**BASE_REQ, "quantity": -1})
        self.assertEqual(len(self.svc.history("s1")), 1)

    def test_sessions_are_isolated_and_ids_validated(self):
        self.svc.submit("s1", BASE_REQ)
        self.svc.submit("s2", {**BASE_REQ, "deadline_weeks": 3})
        self.assertEqual(self.svc.history("s1")[0]["total_matches"], 2)
        self.assertEqual(self.svc.history("s2")[0]["total_matches"], 1)
        for bad in ("", "a b", "../x", "x" * 65, "s;1"):
            with self.assertRaises(ValidationError):
                self.svc.submit(bad, BASE_REQ)

    def test_revising_releases_the_old_hold(self):
        self.svc.submit("s1", BASE_REQ)
        self.svc.hold("s1", "FAST")
        self.assertEqual(self.svc.holds.snapshot(), {"FAST": 1000})
        out = self.svc.submit("s1", {**BASE_REQ, "quantity": 500})
        self.assertEqual(out["diff"]["holds_released"], 1)
        self.assertEqual(self.svc.holds.snapshot(), {})

    def test_concurrent_revisions_in_one_session_stay_consistent(self):
        def rev(i):
            return self.svc.submit("s1", {**BASE_REQ, "quantity": 100 + i})["revision"]
        with ThreadPoolExecutor(8) as pool:
            revs = sorted(f.result() for f in [pool.submit(rev, i) for i in range(20)])
        self.assertEqual(revs, list(range(1, 21)))

    def test_hold_guards(self):
        with self.assertRaises(NotFound):
            self.svc.hold("nobody", "FAST")
        self.svc.submit("s1", {**BASE_REQ, "deadline_weeks": 3})
        with self.assertRaises(ValidationError):
            self.svc.hold("s1", "SLOW")      # does not meet the lead time
        with self.assertRaises(NotFound):
            self.svc.hold("s1", "GHOST")


class AuditLogTests(unittest.TestCase):
    def test_every_request_is_traceable(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.jsonl")
            svc = Service(dataset(mfr("M1")), CFG, audit_path=path)
            res = svc.match(BASE_REQ)
            with open(path, encoding="utf-8") as fh:
                line = json.loads(fh.read().splitlines()[0])
            self.assertEqual(line["request_id"], res["request_id"])
            self.assertEqual(line["as_of"], AS_OF.isoformat())
            self.assertEqual(line["dataset_sha256"], "testsha")
            self.assertEqual(line["result_keys"], ["M1"])
            self.assertEqual(line["fingerprint"], res["request_fingerprint"])
            self.assertEqual(line["requirement"]["material"], "aluminium")
            for h in list(svc._log.handlers):
                h.close()


if __name__ == "__main__":
    unittest.main()
