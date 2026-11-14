"""ledger_since (Arden 1571129057112752299, after the false "never started" for 92acf9f9 on 2026-11-14): the run line
wrote no started/finished facts before it was deployed, so an enqueued-only firing from before state/ledger-since is
`pre-ledger`, never `never-started`, and rows A and C take no action on it. The boundary is written once at deploy:
the earliest `started` fact, or the time of seeding for a ledger with none."""
import json
import os
import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "testguard"))
import no_real_claude  # noqa: E402,F401  first: no test can start a real claude
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_heartbeat_decisions import Base, sid, T0  # noqa: E402
import test_heartbeat_stalled as stalled  # noqa: E402  (a module, so none of its test classes run twice here)
at = stalled.at
import claims  # noqa: E402
import coordination  # noqa: E402
import heartbeat  # noqa: E402


class PreLedger(Base):
    claims_on = False

    def since(self, minutes_ago):
        (self.state / "ledger-since").write_text((self.now - timedelta(minutes=minutes_ago)).isoformat() + "\n")

    def keys(self):
        return sorted(l.split(" key=")[1] for l in self.lines)

    def test_an_enqueued_only_firing_before_the_boundary_is_not_reported_and_one_after_is(self):
        old, _ = self.fire(how="never-started", minutes_ago=600, work=True)
        new, _ = self.fire(how="never-started", minutes_ago=30, work=True)
        self.since(300)
        self.beat()
        self.assertEqual(self.keys(), [f"wake:{new}:never-started"])

    def test_a_firing_enqueued_exactly_at_the_boundary_is_judged(self):
        # Arden's point 1 on #38: the boundary is inclusive ("at or after", heartbeat.md), so `at == since` is covered by
        # the ledger and an enqueued-only firing there is never-started. `<` -> `<=` survived before this test.
        f, _ = self.fire(how="never-started", minutes_ago=300, work=True)
        self.since(300)
        self.beat()
        self.assertEqual(self.keys(), [f"wake:{f}:never-started"])
        facts, _ = coordination.snapshot(self.state)
        since = coordination.ledger_since(self.state)
        self.assertEqual(heartbeat.firing_states(facts, self.now, [], lambda *a: False, {}, {}, since=since)[f],
                         "never-started")

    def test_without_the_file_both_are_reported_as_before(self):
        old, _ = self.fire(how="never-started", minutes_ago=600, work=True)
        new, _ = self.fire(how="never-started", minutes_ago=30, work=True)
        self.beat()
        self.assertEqual(self.keys(), sorted([f"wake:{old}:never-started", f"wake:{new}:never-started"]))

    def test_a_lost_heartbeat_json_does_not_re_report_a_pre_ledger_firing(self):
        # The 92acf9f9 hazard: its reported key is the only thing keeping it quiet today.
        self.fire(how="never-started", minutes_ago=600, work=True)
        self.since(300)
        self.beat()
        (self.state / "heartbeat.json").unlink()
        self.beat(10)
        self.assertEqual(self.lines, [])

    def test_claims_off_the_62_case_stays_quiet(self):
        # Rollback step 2 (TASKCLAIMS off): enqueued-only work firings with closed claims, all before the run line.
        for _ in range(5):
            record = self.c.create("SECRET", owner=None, cwd=str(self.work), by=sid(), dispatch="handoff", status="open",
                                   origin_session=sid())
            self.fire(how="never-started", minutes_ago=600, work=True, claim=record["id"])
        self.since(300)
        self.beat()
        self.assertEqual(self.lines, [])

    def test_started_or_finished_firings_before_the_boundary_are_still_judged(self):
        # Only the enqueued-only case is unknown; a failed run before the boundary has its facts and stays reported.
        failed, _ = self.fire(how="failed", minutes_ago=600, work=True)
        self.since(300)
        self.beat()
        self.assertEqual(self.keys(), [f"wake:{failed}:failed"])

    def test_firing_states_names_the_state(self):
        f, _ = self.fire(how="never-started", minutes_ago=600)
        facts, _ = coordination.snapshot(self.state)
        states = heartbeat.firing_states(facts, self.now, [], lambda *a: False, {}, {}, since=self.now - timedelta(hours=5))
        self.assertEqual(states[f], "pre-ledger")
        self.assertEqual(heartbeat.firing_states(facts, self.now, [], lambda *a: False, {}, {})[f], "never-started")


class PreLedgerClaimsOn(PreLedger):
    claims_on = True

    def test_an_open_hand_off_from_before_the_boundary_is_not_retried(self):
        record = self.c.create("SECRET", owner=None, cwd=str(self.work), by=sid(), dispatch="handoff", status="open",
                               origin_session=sid())
        self.fire(how="never-started", minutes_ago=600, work=True, claim=record["id"])
        self.since(300)
        self.beat()
        self.assertEqual(self.items(), [])
        self.assertFalse(any("never-started" in l for l in self.lines), self.lines)


class PreLedgerStalledLayer(stalled.Base):
    """The stalled layer takes its own snapshot (stalled_layer's re-snapshot), so it needs the boundary too (Arden's
    point 2 on #38), and a queued entry whose wake is pre-ledger is unqueued, not left queued forever and not counted
    as a failed attempt (point 3). Both R and Q are fired, then never start; the boundary is written after their
    enqueue, which can't happen today (heartbeat items come after any boundary) but is the case the states must cover."""

    def setUp(self):
        super().setUp()  # as test_heartbeat_stalled.Lifecycle: activity at 06:33 and one reported unit, so R and Q fire
        self.activity(at(6, 33))
        data = self.hb()
        data["reported"]["unit:x.service:ab:failed"] = at(5, 0).isoformat()
        heartbeat.save(self.state, data)

    def run_it(self, with_since):
        self.beat(at(8, 8))
        self.drain("never-started", ack=False)
        if with_since:
            (self.state / "ledger-since").write_text(at(8, 5).isoformat() + "\n")
        self.beat(at(8, 18))
        d = self.hb()
        return d.get("delivery_fault"), d["quiet"], d["reports_q"]["unit:x.service:ab:failed"]

    def test_without_the_boundary_both_are_failed_attempts(self):
        fault, q, r = self.run_it(False)
        self.assertEqual(fault["counts"]["hb_failed"], 2)
        self.assertEqual((q["failures"], r["failures"], r["state"]), (1, 1, "none"))

    def test_pre_ledger_heartbeat_firings_are_no_fault_and_no_failed_attempt(self):
        fault, q, r = self.run_it(True)
        self.assertIsNone(fault)
        self.assertEqual((q["failures"], r["failures"], r["state"]), (0, 0, "none"))
        self.assertEqual((q["state"], q["attempt"]), ("queued", 2))  # unqueued, then Q queued again this beat


class Seed(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, fact, at):
        coordination.append(self.state, {"firing": sid(), "fact": fact, "at": at})

    def test_the_earliest_started_fact_is_the_boundary(self):
        self.append("enqueued", "2026-11-12T15:45:12+00:00")
        self.append("started", "2026-11-13T04:45:12+00:00")
        self.append("started", "2026-11-14T01:00:00+00:00")
        self.assertEqual(coordination.seed_ledger_since(self.state, now=T0), ("2026-11-13T04:45:12+00:00", True))
        self.assertEqual(coordination.ledger_since(self.state).isoformat(), "2026-11-13T04:45:12+00:00")

    def test_an_empty_or_start_free_ledger_takes_the_time_of_seeding(self):
        self.append("enqueued", "2026-11-10T00:00:00+00:00")
        self.assertEqual(coordination.seed_ledger_since(self.state, now=T0)[0], T0.isoformat(timespec="seconds"))
        empty = self.state / "fresh"
        self.assertEqual(coordination.seed_ledger_since(empty, now=T0), (T0.isoformat(timespec="seconds"), True))

    def test_written_once_never_rewritten(self):
        coordination.seed_ledger_since(self.state, now=T0)
        before = (self.state / "ledger-since").read_bytes()
        self.append("started", "2026-11-01T00:00:00+00:00")
        self.assertEqual(coordination.seed_ledger_since(self.state, now=T0 + timedelta(days=1)),
                         (before.decode().strip(), False))
        self.assertEqual((self.state / "ledger-since").read_bytes(), before)

    def test_racing_seeders_write_once(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(coordination.seed_ledger_since(self.state, now=T0)[1]))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)

    def test_missing_or_garbage_reads_as_no_boundary(self):
        self.assertIsNone(coordination.ledger_since(self.state))
        for bad in ("", "yesterday", "2026-11-13T04:45:12"):  # the last is naive: refused, not guessed
            (self.state / "ledger-since").write_text(bad)
            self.assertIsNone(coordination.ledger_since(self.state), bad)

    def test_the_command_seeds_the_state_it_is_given(self):
        import subprocess
        script = Path(coordination.__file__)
        run = lambda: subprocess.run([sys.executable, "-I", str(script), "seed-ledger-since"], capture_output=True, text=True,
                                     env=dict(os.environ, PERSISTENCE_STATE_DIR=str(self.state)), check=True).stdout
        self.assertIn("written", run())
        self.assertIn("already", run())


if __name__ == "__main__":
    unittest.main()
