"""Task claims, the claims core (docs/design/task-claims.md, revision 4, sha256 8440c4dd…6b33).

Each class names the design's section 6 test it covers. Run-due's spawn claim (claim at spawn, the spawn lock path,
the spawn's split writes, `cwd` through the hand-off, the seam test) is tests/test_spawn_claim.py; the shared locks and
ledger are tests/test_coordination.py. Per Ember 1570181249534795788 point 3, the heartbeat PR owns stall decisions
(spell/silent stall through the beat), retries and deferrals (owner never started, the hand-off retry bound),
reporting, claims off with the heartbeat on, the nudge's lock path and split writes, and the heartbeat contract.

Every claim here lives in a temporary state directory under a temporary home; nothing touches the real ones."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "testguard"))
import no_real_claude  # noqa: E402,F401  first: no test can start a real claude (Arden 1570199499278909491)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hook
import claims
import coordination

CASES = Path(__file__).resolve().parent / "cases" / "process-holds-session.json"


def sid():
    return str(uuid.uuid4())


class Clock:
    def __init__(self, at=datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, **kw):
        self.at += timedelta(**kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / "home"
        (self.home / "work").mkdir(parents=True)
        (self.home / ".claude").mkdir()
        self.state = self.home / ".claude" / "persistence" / "state"
        self.state.mkdir(parents=True)
        self.outside = root / "outside"
        self.outside.mkdir()
        self.clock = Clock()
        self.c = claims.Claims(self.state, home=self.home, now=self.clock)
        self.env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                if not k.startswith("PERSISTENCE_") and k != "CLAUDE_CODE_SESSION_ID"}, clear=True)
        self.env.start()
        self.me = sid()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def mine(self, task="Build the claims core", owner=None, cwd=None):
        return self.c.claim(task, owner or self.me, cwd=str(cwd or self.home / "work"))

    def files(self):
        return sorted(p.name for p in (self.state / "tasks").glob("*.json"))


class KeySet(Base):
    """Key set (regression): reordered keys accepted by add_wake and claims; a missing or extra key refused."""

    def test_a_claims_key_set_is_exactly_the_task_fields_and_the_claim_fields(self):
        self.assertEqual(claims.KEYS, set(hook.TASK_FIELDS) | set(claims.CLAIM_FIELDS))
        self.assertEqual(set(self.mine()), claims.KEYS)

    def test_a_reordered_record_is_valid_and_a_missing_or_extra_key_is_not(self):
        record = self.mine()
        self.assertTrue(claims.valid_record({k: record[k] for k in reversed(list(record))}))
        self.assertFalse(claims.valid_record({k: v for k, v in record.items() if k != "cwd"}))
        self.assertFalse(claims.valid_record(dict(record, command="git push --force")))

    def test_a_claim_file_with_a_missing_or_extra_key_is_refused_on_read(self):
        record = self.mine()
        path = self.state / "tasks" / f"{record['id']}.json"
        for bad in ({k: v for k, v in record.items() if k != "owner"}, dict(record, extra=1)):
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(claims.Refused):
                self.c.touch(record["id"], self.me)

    def test_add_wake_still_accepts_a_reordered_handoff_and_refuses_a_bad_one(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hook, "WAKES", Path(d) / "wakes.json"):
            record = dict.fromkeys(hook.TASK_FIELDS, "x")
            hook.add_wake({"reason": "r", "session": "s", "in": 1, "handoff": {k: record[k] for k in reversed(hook.TASK_FIELDS)}})
            hook.add_wake({"reason": "r", "session": "s", "in": 1, "handoff": dict(record, extra="y")})
            stored = hook.wakes()
            self.assertEqual(tuple(stored[0]["handoff"]), hook.TASK_FIELDS)
            self.assertNotIn("handoff", stored[1])


class OneClaimPerSubject(Base):
    """The deploy guard, part 3 (2026-11-14 duplicate deploy claims, #18519): a subject allows one live claim; a second is refused
    under the lock, and writes nothing, until the first is done or abandoned. A pause doesn't free it."""

    def deploy(self, owner=None):
        return self.c.claim("Deploy the store", owner or self.me, str(self.home / "work"), subject="deploy")

    def test_a_second_live_claim_for_a_subject_is_refused_and_writes_nothing(self):
        first = self.deploy()
        before = self.files()
        with self.assertRaises(claims.Refused) as e:
            self.deploy(owner=sid())
        self.assertIn(first["id"], str(e.exception))
        self.assertIn("take", str(e.exception))
        self.assertEqual(self.files(), before)

    def test_an_open_hand_off_blocks_too_and_says_abandon_frees_it(self):
        first = self.c.create("Deploy", None, str(self.home / "work"), by=self.me, dispatch="handoff", status="open",
                              subject="deploy")
        with self.assertRaises(claims.Refused) as e:
            self.deploy()
        self.assertIn(f"ptask abandon {first['id']}", str(e.exception))
        self.c.abandon(first["id"], self.me)  # anyone may abandon an open claim: that's how the room frees it
        self.deploy()

    def test_a_paused_claim_still_holds_its_subject(self):
        first = self.deploy()
        self.c.pause(first["id"], self.me, why="waiting")
        with self.assertRaises(claims.Refused):
            self.deploy(owner=sid())

    def test_done_or_abandoned_frees_the_subject(self):
        for close in ("done", "abandon"):
            first = self.deploy()
            getattr(self.c, close)(first["id"], self.me)
            self.assertEqual(self.deploy()["subject"], "deploy")
            self.c.done(self.c.live_for("deploy")[0]["id"], self.me)

    def test_other_subjects_and_no_subject_are_unaffected(self):
        self.deploy()
        self.c.claim("Something else", sid(), str(self.home / "work"), subject="ledger-since")
        self.mine()
        self.mine()
        self.assertEqual(len(self.c.live_for("deploy")), 1)
        self.assertEqual(self.c.live_for(None), [])

    def test_a_subject_is_a_slug(self):
        for bad in ("Deploy", "-x", "a b", "x" * 41, "deploy;rm", ""):
            with self.assertRaises(claims.Refused, msg=bad):
                self.c.claim("t", self.me, str(self.home / "work"), subject=bad)
        self.assertEqual(self.files(), [])

    def test_two_racing_creators_one_wins(self):
        results = []

        def go():
            try:
                results.append(self.deploy(owner=sid())["id"])
            except claims.Refused:
                results.append(None)
        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_a_claim_from_before_subjects_reads_and_writes_back_with_subject_null(self):
        record = self.mine()
        path = self.state / "tasks" / f"{record['id']}.json"
        legacy = {k: v for k, v in record.items() if k != "subject"}
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertIsNone(self.c.get(record["id"])["subject"])
        self.c.touch(record["id"], self.me)
        self.assertIsNone(json.loads(path.read_text())["subject"])
        self.assertIn("subject", json.loads(path.read_text()))

    def test_only_subject_may_be_missing(self):
        record = self.mine()
        self.assertFalse(claims.valid_record({k: v for k, v in record.items() if k not in ("subject", "outcome")}))


class Holds(Base):
    """The deploy guard, part 2: `ptask holds <id> <subject>` passes only for this session's claimed claim with that
    subject (Arden 1571042268406882415: a lock alone doesn't stop a second session deploying after the first)."""

    def setUp(self):
        super().setUp()
        self.record = self.c.claim("Deploy", self.me, str(self.home / "work"), subject="deploy")

    def cli(self, *args, session=None):
        out, err = [], []
        code = claims.cli(list(args), env={"CLAUDE_CODE_SESSION_ID": session or self.me}, state=self.state,
                          home=self.home, out=out.append, err=err.append, now=self.clock)
        return code, "\n".join(out + err)

    def test_the_holder_passes(self):
        self.assertEqual(self.cli("holds", self.record["id"], "deploy")[0], 0)

    def test_another_session_fails(self):
        code, text = self.cli("holds", self.record["id"], "deploy", session=sid())
        self.assertEqual(code, 1)
        self.assertIn("not this session", text)

    def test_the_wrong_subject_fails(self):
        self.assertEqual(self.cli("holds", self.record["id"], "ledger-since")[0], 1)
        plain = self.mine()
        self.assertEqual(self.cli("holds", plain["id"], "deploy")[0], 1)

    def test_paused_or_closed_fails(self):
        self.c.pause(self.record["id"], self.me, why="w")
        self.assertEqual(self.cli("holds", self.record["id"], "deploy")[0], 1)
        self.c.resume(self.record["id"], self.me)
        self.c.done(self.record["id"], self.me)
        code, text = self.cli("holds", self.record["id"], "deploy")
        self.assertEqual(code, 1)
        self.assertIn("done", text)

    def test_no_session_or_a_bad_id_fails(self):
        self.assertEqual(claims.cli(["holds", self.record["id"], "deploy"], env={}, state=self.state, home=self.home,
                                    out=lambda _: None, err=lambda _: None), 1)
        self.assertEqual(self.cli("holds", self.record["id"][:8], "deploy")[0], 1)

    def test_ptask_claim_takes_a_subject(self):
        code, text = self.cli("claim", "--subject", "ledger-since", "--cwd", str(self.home / "work"), "Fix", "it")
        self.assertEqual(code, 0, text)
        self.assertIn("subject ledger-since", text)
        code, text = self.cli("claim", "--cwd", str(self.home / "work"), "--subject", "deploy", "Again")
        self.assertEqual(code, 1)
        self.assertIn("already has a live claim", text)

class OneOwner(Base):
    """One owner: two claim/take races on one id under the lock: exactly one wins; the loser is told the owner and
    whether it's running."""

    def open_one(self):
        return self.c.create("A hand-off", owner=None, cwd=str(self.home / "work"), by="harness", dispatch="handoff", status="open")

    def race(self, fn, n=8):
        start, results = threading.Barrier(n), []

        def go(i):
            start.wait()
            try:
                results.append(("won", fn(i)))
            except claims.Refused as e:
                results.append(("lost", str(e)))
        threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return results

    def test_racing_claims_of_one_open_record_one_wins(self):
        record = self.open_one()
        racers = [sid() for _ in range(8)]
        results = self.race(lambda i: self.c.claim_open(record["id"], racers[i], by="harness", firing=sid()))
        won = [r for r in results if r[0] == "won"]
        self.assertEqual(len(won), 1, results)
        owner = self.c.get(record["id"])["owner"]
        for kind, text in results:
            if kind == "lost":
                self.assertIn(owner, text)
                self.assertIn("not running", text)
        self.assertEqual([h["action"] for h in self.c.get(record["id"])["history"]], ["create", "claim"])

    def test_racing_takes_one_wins_and_the_loser_is_told_the_owner_and_that_it_is_running_or_not(self):
        record = self.mine()
        takers = [sid() for _ in range(8)]
        results = self.race(lambda i: self.c.take(record["id"], takers[i], table=[], items=[], expected_owner=self.me))
        self.assertEqual(len([r for r in results if r[0] == "won"]), 1, results)
        after = self.c.get(record["id"])
        self.assertIn(after["owner"], takers)
        self.assertEqual([h["action"] for h in after["history"]], ["claim", "take"])
        self.assertEqual(after["history"][-1]["from_owner"], self.me)

    def test_a_take_is_refused_while_the_owner_runs_and_says_so(self):
        record = self.mine()
        with self.assertRaises(claims.Refused) as e:
            self.c.take(record["id"], sid(), table=[["claude", "--resume", self.me]], items=[])
        self.assertIn(self.me, str(e.exception))
        self.assertIn("which is running", str(e.exception))

    def test_a_take_is_refused_while_a_wake_is_pending_for_the_owner_or_the_claim(self):
        record = self.mine()
        for item in ({"session": self.me, "at": 0, "reason": "r"}, {"session": sid(), "claim": record["id"], "at": 0, "reason": "r"}):
            with self.subTest(item=item), self.assertRaises(claims.Refused):
                self.c.take(record["id"], sid(), table=[], items=[item])

    def test_a_take_with_no_taker_named_is_refused(self):
        record = self.mine()
        for taker in ("", None, self.me[:8]):
            with self.subTest(taker=taker), self.assertRaises(claims.Refused):
                self.c.take(record["id"], taker, table=[], items=[])

    def test_take_reads_the_wake_queue_itself_when_not_given_one(self):
        record = self.mine()
        (self.state.parent / "wakes.json").write_text(json.dumps([{"session": self.me, "at": 0, "reason": "r"}]))
        with self.assertRaises(claims.Refused):
            self.c.take(record["id"], sid(), table=[])


class RoomSessionsCantOwn(Base):
    """Room sessions can't own: ptask claim and take with PERSISTENCE_ROOM_CHANNEL set are refused and name pwake."""

    def run_cli(self, *args, env=None):
        out, err = [], []
        code = claims.cli(list(args), env=env or {}, state=self.state, home=self.home, cwd=str(self.home / "work"),
                          out=out.append, err=err.append, now=self.clock)
        return code, "\n".join(out), "\n".join(err)

    def test_claim_and_take_from_a_room_session_are_refused_and_name_pwake(self):
        record = self.mine()
        room = {"PERSISTENCE_ROOM_CHANNEL": "1569586907955073130", "CLAUDE_CODE_SESSION_ID": sid()}
        for args in (("claim", "Do the thing"), ("take", record["id"])):
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args, env=room)
                self.assertNotEqual(code, 0)
                self.assertIn("pwake", err)
        self.assertEqual(self.files(), [f"{record['id']}.json"])
        self.assertEqual(self.c.get(record["id"])["owner"], self.me)

    def test_a_room_session_may_list_show_and_abandon_an_open_handoff(self):
        record = self.c.create("A hand-off", owner=None, cwd=str(self.home / "work"), by="harness", dispatch="handoff", status="open")
        room_id = sid()
        room = {"PERSISTENCE_ROOM_CHANNEL": "1569586907955073130", "CLAUDE_CODE_SESSION_ID": room_id}
        self.assertEqual(self.run_cli("list", env=room)[0], 0)
        self.assertEqual(self.run_cli("show", record["id"], env=room)[0], 0)
        self.assertEqual(self.run_cli("abandon", record["id"], "not needed", env=room)[0], 0)
        after = self.c.get(record["id"])
        self.assertEqual(after["status"], "abandoned")
        self.assertEqual(after["history"][-1]["by"], room_id)

    def test_a_working_session_claims_for_itself_through_the_cli(self):
        code, out, err = self.run_cli("claim", "Build it", env={"CLAUDE_CODE_SESSION_ID": self.me})
        self.assertEqual(code, 0, err)
        [name] = self.files()
        record = self.c.get(name[:-5])
        self.assertEqual((record["owner"], record["status"], record["dispatch"]), (self.me, "claimed", "none"))
        self.assertEqual(record["cwd"], str(self.home / "work"))
        self.assertIn(record["id"], out)

    def test_a_claim_with_no_session_id_is_refused(self):
        code, out, err = self.run_cli("claim", "Build it", env={})
        self.assertNotEqual(code, 0)
        self.assertEqual(self.files(), [])


class ExactLiveness(Base):
    """Exact liveness: a live process for another id, or for a prefix of the owner's id, is not the owner running.
    The cases are one table, shared with FragmentStore.ProcessHoldsSession's."""

    def test_the_shared_case_table(self):
        table = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertGreater(len(table["cases"]), 5)
        for case in table["cases"]:
            with self.subTest(case=case["why"]):
                self.assertEqual(claims.holds_session(case["argv"], case["session"]), case["holds"])

    def test_running_over_a_table_and_unknown_without_one(self):
        owner = sid()
        self.assertTrue(claims.running(owner, [["bash"], ["claude", "-p", "--session-id", owner]]))
        self.assertFalse(claims.running(owner, [["claude", "--resume", owner[:8]], ["claude", "--resume", sid()],
                                               ["bash", "-lc", f"claude -p --resume {owner}"]]))
        self.assertIsNone(claims.running(owner, None))

    def test_the_process_table_sees_a_real_process_by_exact_id(self):
        owner = sid()
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--session-id", owner])
        try:
            time.sleep(0.2)
            table = claims.process_table()
            self.assertTrue(claims.running(owner, table))
            self.assertFalse(claims.running(owner[:8], table))
            self.assertFalse(claims.running(sid(), table))
        finally:
            p.kill()
            p.wait()

    def test_no_proc_means_unknown(self):
        self.assertIsNone(claims.process_table(proc="/nonexistent-proc"))


class RetryIdentity(Base):
    """Retry identity: a pending wake for an owner-prefix collision, and a wake belonging to another claim, do not
    count as pending for this one."""

    def test_pending_is_by_exact_owner_or_exact_claim_id(self):
        record = self.mine()
        other = sid()
        self.assertFalse(claims.pending_for(record, [{"session": self.me[:8], "reason": record["id"]},
                                                     {"session": self.me + "x"},
                                                     {"session": other, "claim": sid(), "reason": f"{self.me} {record['id']}"}]))
        self.assertTrue(claims.pending_for(record, [{"session": self.me}]))
        self.assertTrue(claims.pending_for(record, [{"session": other, "claim": record["id"]}]))

    def test_matching_on_owner_alone_misses_a_wake_for_the_claim(self):
        record = self.mine()
        self.assertTrue(claims.pending_for(record, [{"session": sid(), "claim": record["id"]}]))


class Transitions(Base):
    """open → abandoned from a room session; any transition not in the table refused."""

    def test_every_transition_in_the_table(self):
        r = self.mine()
        i, me = r["id"], self.me
        self.c.pause(i, me, why="waiting on review")
        self.assertEqual(self.c.get(i)["status"], "paused")
        self.c.resume(i, me)
        self.c.touch(i, me)
        self.c.done(i, me, outcome="built")
        after = self.c.get(i)
        self.assertEqual(after["status"], "done")
        self.assertEqual(after["outcome"], "built")
        p = self.mine()
        self.c.pause(p["id"], me)
        self.c.abandon(p["id"], me, outcome="dropped")
        self.assertEqual(self.c.get(p["id"])["status"], "abandoned")
        o = self.c.create("h", owner=None, cwd=str(self.home / "work"), by="harness", dispatch="handoff", status="open")
        self.c.abandon(o["id"], sid())
        self.assertEqual(self.c.get(o["id"])["status"], "abandoned")

    def test_anything_else_is_refused_and_changes_no_byte(self):
        r = self.mine()
        o = self.c.create("h", owner=None, cwd=str(self.home / "work"), by="harness", dispatch="handoff", status="open")
        d = self.mine()
        self.c.done(d["id"], self.me)
        bad = [(self.c.resume, r["id"]), (self.c.claim_open, r["id"]),
               (self.c.pause, o["id"]), (self.c.resume, o["id"]), (self.c.touch, o["id"]), (self.c.done, o["id"]),
               (self.c.take, o["id"]),
               (self.c.touch, d["id"]), (self.c.pause, d["id"]), (self.c.resume, d["id"]), (self.c.done, d["id"]),
               (self.c.abandon, d["id"]), (self.c.claim_open, d["id"])]
        for fn, i in bad:
            with self.subTest(fn=fn.__name__, status=self.c.get(i)["status"]):
                path = self.state / "tasks" / f"{i}.json"
                before = path.read_bytes()
                with self.assertRaises(claims.Refused):
                    if fn == self.c.take:
                        fn(i, sid(), table=[], items=[])
                    else:
                        fn(i, sid())
                self.assertEqual(path.read_bytes(), before)

    def test_a_touch_while_paused_keeps_it_paused(self):
        r = self.mine()
        self.c.pause(r["id"], self.me)
        self.c.touch(r["id"], self.me)
        self.assertEqual(self.c.get(r["id"])["status"], "paused")


class History(Base):
    """History: every write appends exactly one entry with its actor; take records the old owner."""

    def test_every_write_appends_exactly_one_entry_with_its_actor_and_updates_updated(self):
        r = self.mine()
        i, me = r["id"], self.me
        steps = [(lambda: self.c.touch(i, me), "touch", "claimed", "claimed", me),
                 (lambda: self.c.pause(i, me, why="w"), "pause", "claimed", "paused", me),
                 (lambda: self.c.resume(i, me), "resume", "paused", "claimed", me),
                 (lambda: self.c.take(i, (t := sid()), table=[], items=[]), "take", "claimed", "claimed", None),
                 (lambda: self.c.done(i, self.c.get(i)["owner"]), "done", "claimed", "done", None)]
        for step, action, a, b, by in steps:
            with self.subTest(action=action):
                n = len(self.c.get(i)["history"])
                self.clock.advance(minutes=1)
                step()
                after = self.c.get(i)
                self.assertEqual(len(after["history"]), n + 1)
                entry = after["history"][-1]
                self.assertEqual((entry["action"], entry["from_status"], entry["to_status"]), (action, a, b))
                self.assertEqual(set(entry), {"at", "action", "from_status", "to_status", "from_owner", "to_owner", "by", "firing"}
                                 | ({"why"} if action == "pause" else set()))
                if by:
                    self.assertEqual(entry["by"], by)
                self.assertEqual(after["updated"], entry["at"])
                self.assertEqual(entry["at"], self.clock().isoformat(timespec="seconds"))

    def test_take_records_the_old_owner_and_the_taker(self):
        r = self.mine()
        taker = sid()
        self.c.take(r["id"], taker, table=[], items=[])
        entry = self.c.get(r["id"])["history"][-1]
        self.assertEqual((entry["from_owner"], entry["to_owner"], entry["by"]), (self.me, taker, taker))

    def test_the_spawn_claim_and_expiry_are_by_their_fixed_actors(self):
        o = self.c.create("h", owner=None, cwd=str(self.home / "work"), by="harness", dispatch="handoff", status="open")
        firing, worker = sid(), sid()
        self.c.claim_open(o["id"], worker, by="harness", firing=firing)
        entry = self.c.get(o["id"])["history"][-1]
        self.assertEqual((entry["by"], entry["firing"], entry["to_owner"]), ("harness", firing, worker))
        self.c.pause(o["id"], worker, until=self.clock() + timedelta(minutes=5))
        self.clock.advance(minutes=6)
        self.c.settle_pauses()
        entry = self.c.get(o["id"])["history"][-1]
        self.assertEqual((entry["action"], entry["by"]), ("expire", "pause-expiry"))

    def test_a_created_claim_has_one_entry(self):
        r = self.mine()
        self.assertEqual([(h["action"], h["from_status"], h["to_status"], h["by"]) for h in r["history"]],
                         [("claim", None, "claimed", self.me)])


class PauseExpiry(Base):
    """Pause expiry: settle_pauses is the only writer of expire: one transition with by: pause-expiry,
    nudged/reported cleared; two concurrent callers produce one entry; without --until the claim stays paused across
    any number of beats; with the heartbeat and PERSISTENCE_TASKCLAIMS off, the next ptask write settles it."""

    def paused(self, minutes=5):
        r = self.mine()
        self.c.pause(r["id"], self.me, until=self.clock() + timedelta(minutes=minutes) if minutes else None, why="later")
        return r["id"]

    def test_one_expiry_clears_the_spell_marks(self):
        i = self.paused()
        path = self.state / "tasks" / f"{i}.json"
        record = json.loads(path.read_text())
        record.update(nudged="2026-11-11T21:01:00+00:00", reported="2026-11-11T21:02:00+00:00")  # as the heartbeat would mark it
        path.write_text(json.dumps(record))
        self.clock.advance(minutes=5)
        self.assertEqual(self.c.settle_pauses(), 1)
        after = self.c.get(i)
        self.assertEqual(after["status"], "claimed")
        self.assertEqual((after["nudged"], after["reported"], after["pause_until"]), (None, None, None))
        entry = after["history"][-1]
        self.assertEqual((entry["action"], entry["by"], entry["from_status"], entry["to_status"]), ("expire", "pause-expiry", "paused", "claimed"))
        self.assertEqual(entry["at"], self.clock().isoformat(timespec="seconds"))
        self.assertIn("pause_until", entry)
        self.assertEqual(self.c.settle_pauses(), 0)

    def test_not_before_its_time(self):
        i = self.paused()
        self.clock.advance(minutes=4)
        self.assertEqual(self.c.settle_pauses(), 0)
        self.assertEqual(self.c.get(i)["status"], "paused")

    def test_two_concurrent_callers_produce_one_entry(self):
        i = self.paused()
        self.clock.advance(minutes=10)
        barrier, counts = threading.Barrier(6), []

        def go():
            barrier.wait()
            counts.append(claims.Claims(self.state, home=self.home, now=self.clock).settle_pauses())
        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sum(counts), 1)
        self.assertEqual([h["action"] for h in self.c.get(i)["history"]].count("expire"), 1)

    def test_without_until_it_stays_paused(self):
        i = self.paused(minutes=None)
        for _ in range(50):
            self.clock.advance(days=1)
            self.assertEqual(self.c.settle_pauses(), 0)
        self.assertEqual(self.c.get(i)["status"], "paused")

    def test_the_next_ptask_write_settles_it_with_the_setting_off(self):
        i = self.paused()
        self.clock.advance(minutes=10)
        other = self.mine(task="something else")
        self.c.touch(other["id"], self.me)
        after = self.c.get(i)
        self.assertEqual(after["status"], "claimed")
        self.assertEqual(after["history"][-1]["by"], "pause-expiry")

    def test_settle_is_the_only_writer_of_expire(self):
        i = self.paused()
        self.clock.advance(minutes=10)
        self.c.touch(i, self.me)  # the settle inside this write expires it first; the touch is then on a claimed record
        actions = [(h["action"], h["by"]) for h in self.c.get(i)["history"]]
        self.assertEqual(actions[-2:], [("expire", "pause-expiry"), ("touch", self.me)])

    def test_show_and_list_do_not_write_and_say_passed(self):
        i = self.paused()
        self.clock.advance(minutes=10)
        path = self.state / "tasks" / f"{i}.json"
        before = path.read_bytes()
        out = []
        claims.cli(["show", i], env={}, state=self.state, home=self.home, cwd=str(self.home), out=out.append, err=out.append, now=self.clock)
        claims.cli(["list"], env={}, state=self.state, home=self.home, cwd=str(self.home), out=out.append, err=out.append, now=self.clock)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("passed", "\n".join(out))


class CwdCanonicalAndBound(Base):
    """`cwd` is canonical and bound, and the launcher (`cwd-exec`)."""

    def test_a_cwd_through_a_symlink_is_stored_as_its_real_path(self):
        (self.home / "link").symlink_to(self.home / "work")
        r = self.mine(cwd=self.home / "link")
        self.assertEqual(r["cwd"], str(self.home / "work"))

    def test_bad_cwds_are_refused_at_creation(self):
        (self.home / "out").symlink_to(self.outside)
        (self.home / "afile").write_text("x")
        (self.home / "wo\nrk").mkdir()
        for bad in ("", str(self.outside), str(self.home / "out"), str(self.home / "gone"), str(self.home / "afile"),
                    str(self.home / "wo\nrk"), str(self.home / "work" / ".." / ".."), "..", str(self.home) + "/../outside"):
            with self.subTest(cwd=bad), self.assertRaises(claims.Refused):
                self.c.claim("t", self.me, cwd=bad, base=str(self.home))
        self.assertEqual(self.files(), [])

    def test_harmless_noncanonical_input_is_accepted_and_only_its_canonical_path_stored(self):
        """Ember 1570181249534795788, point 4: canonicalise at creation, validate and store only that; don't refuse
        input that reaches an allowed directory through `.`, `..` or a symlink; never store the string as given."""
        work = str(self.home / "work")
        (self.home / "work" / "proj").mkdir()
        (self.home / "link").symlink_to(self.home / "work")
        cases = [(work + "/", None), (str(self.home) + "//work", None), (work + "/proj/..", None), (work + "/./proj/../", None),
                 (str(self.home / "link"), None), (".", work), ("..", work + "/proj"), ("proj/..", work), ("link", str(self.home))]
        for given, base in cases:
            with self.subTest(cwd=given, base=base):
                r = self.c.claim("t", self.me, cwd=given, base=base)
                self.assertEqual(r["cwd"], work)
                if os.path.isabs(given):
                    self.assertNotIn(given, json.dumps(r), "the string as given is never stored")

    def test_ptask_claim_takes_the_callers_directory_or_a_relative_cwd_against_it(self):
        work = str(self.home / "work")
        (self.home / "work" / "proj").mkdir()
        lines = []
        env = {"CLAUDE_CODE_SESSION_ID": self.me}
        self.assertEqual(claims.cli(["claim", "--cwd", "..", "t"], env=env, state=self.state, home=self.home,
                                    cwd=work + "/proj", out=lines.append, now=self.clock), 0)
        self.assertEqual(claims.cli(["claim", "t2"], env=env, state=self.state, home=self.home,
                                    cwd=work + "/proj/..", out=lines.append, now=self.clock), 0)
        self.assertEqual(sorted(r["cwd"] for r in self.c.all()), [work, work])

    def setup_tree(self):
        (self.home / "work" / "proj" / "deep").mkdir(parents=True)
        (self.home / "elsewhere" / "deep").mkdir(parents=True)
        (self.outside / "deep").mkdir()
        return self.mine(cwd=self.home / "work" / "proj" / "deep")

    def swap(self, path, target):
        aside = path.with_name(path.name + ".aside")
        path.rename(aside)
        path.symlink_to(target)

    def worker_cwd(self, fd):
        out = Path(tempfile.mkdtemp(dir=self.outside)) / "cwd"
        p = claims.launch(fd, [sys.executable, "-c", f"import os; open({str(out)!r}, 'w').write(os.getcwd())"])
        p.wait(10)
        return out.read_text() if out.exists() else None

    def test_a_retargeted_final_or_intermediate_component_is_refused_by_both_resolvers(self):
        for resolver in ("openat2", "openat"):
            if resolver == "openat2" and not claims.HAVE_OPENAT2:
                self.skipTest("no openat2 on this kernel")
            for part, target_kind in (("final", "inside"), ("final", "outside"), ("intermediate", "inside"), ("intermediate", "outside")):
                with self.subTest(resolver=resolver, part=part, target=target_kind):
                    for p in sorted(self.home.rglob("*"), reverse=True):
                        if p.is_symlink() or p.is_file():
                            p.unlink()
                    for p in sorted((self.home / "work").rglob("*"), reverse=True):
                        p.rmdir() if p.is_dir() else None
                    for p in sorted(self.home.glob("elsewhere*")) + sorted(self.outside.glob("deep*")):
                        import shutil
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
                    for f in (self.state / "tasks").glob("*.json"):
                        f.unlink()
                    r = self.setup_tree()
                    self.assertEqual(self.worker_cwd(self.c.open_cwd(r["cwd"], resolver=resolver)), r["cwd"])
                    if part == "final":
                        self.swap(self.home / "work" / "proj" / "deep", (self.home / "elsewhere" / "deep") if target_kind == "inside" else (self.outside / "deep"))
                    else:
                        self.swap(self.home / "work" / "proj", (self.home / "elsewhere") if target_kind == "inside" else self.outside)
                    with self.assertRaises(claims.Refused) as e:
                        self.c.open_cwd(r["cwd"], resolver=resolver)
                    self.assertIn("no valid cwd", str(e.exception))
                    with self.assertRaises(claims.Refused):
                        self.c.take(r["id"], sid(), table=[], items=[], resolver=resolver)
                    self.assertEqual(self.c.get(r["id"])["owner"], self.me)

    def test_the_fd_must_be_owned_by_the_self_and_a_directory(self):
        r = self.mine()
        with mock.patch.object(claims.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(claims.Refused):
                self.c.open_cwd(r["cwd"])

    def test_the_worker_runs_in_the_verified_directory_even_when_the_string_is_retargeted_after_verify(self):
        (self.home / "work" / "a").mkdir()
        (self.home / "work" / "b").mkdir()
        r = self.mine(cwd=self.home / "work" / "a")
        fd = self.c.open_cwd(r["cwd"])
        (self.home / "work" / "a").rename(self.home / "work" / "a.moved")
        (self.home / "work" / "a").symlink_to(self.home / "work" / "b")
        self.assertEqual(self.worker_cwd(fd), str(self.home / "work" / "a.moved"))

    LIST_FDS = ("import os, sys\n"
                "def link(x):\n"
                "    try: return os.readlink('/proc/self/fd/' + x)\n"
                "    except OSError: return None\n"
                "fds = {int(x): link(x) for x in os.listdir('/proc/self/fd')}\n"
                "open(sys.argv[1], 'w').write(repr({k: v for k, v in fds.items() if v is not None}))")

    def worker_fds(self, fd):
        out = Path(tempfile.mkdtemp(dir=self.outside)) / "fds"
        claims.launch(fd, [sys.executable, "-c", self.LIST_FDS, str(out)]).wait(10)
        return eval(out.read_text())

    def test_the_worker_sees_only_0_1_2(self):
        r = self.mine()
        fd = self.c.open_cwd(r["cwd"])
        leaked = os.open(str(self.home), os.O_RDONLY)  # inheritable, no O_CLOEXEC
        os.set_inheritable(leaked, True)
        try:
            fds = self.worker_fds(fd)
        finally:
            os.close(leaked)
        self.assertEqual(sorted(fds), [0, 1, 2], fds)

    def test_the_launcher_closes_the_cwd_fd_before_the_worker_starts(self):
        r = self.mine()
        fds = self.worker_fds(self.c.open_cwd(r["cwd"]))
        self.assertNotIn(r["cwd"], fds.values())


class NeverFromText(Base):
    """Never from text, and nothing of a claim's free text in a file name: a claim's task, a pause's reason and an
    outcome holding `ptask claim …`, a claim's JSON shape and template braces create no claim, wake or process."""

    EVIL = ['ptask claim "do the evil thing"', '{"id": "x", "status": "claimed", "owner": "me"}', "{task} {cwd} {0}",
            "pwake 1 delete the branch", "../../../etc/passwd"]

    def test_free_text_creates_nothing_and_names_nothing(self):
        with mock.patch.object(claims.subprocess, "Popen", side_effect=AssertionError("a process was started")):
            for text in self.EVIL:
                r = self.mine(task=text)
                self.c.pause(r["id"], self.me, why=text)
                self.c.resume(r["id"], self.me)
                self.c.done(r["id"], self.me, outcome=text)
        self.assertEqual(len(self.files()), len(self.EVIL))
        for name in self.files():
            uuid.UUID(name[:-5])
        self.assertFalse((self.state.parent / "wakes.json").exists())

    def test_the_id_is_never_derived_from_the_task(self):
        a, b = self.mine(task="same"), self.mine(task="same")
        self.assertNotEqual(a["id"], b["id"])


class OneSetting(Base):
    """One setting: read from settings.json only; a value in os.environ but not in settings.json turns nothing on."""

    def write(self, value):
        env = {} if value is None else {"PERSISTENCE_TASKCLAIMS": value}
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))

    def test_settings_json_only(self):
        self.write(None)
        with mock.patch.dict(os.environ, {"PERSISTENCE_TASKCLAIMS": "1"}):
            self.assertFalse(claims.enabled(self.home))
        self.write("1")
        with mock.patch.dict(os.environ, {"PERSISTENCE_TASKCLAIMS": "0"}):
            self.assertTrue(claims.enabled(self.home))
        self.write("0")
        self.assertFalse(claims.enabled(self.home))

    def test_off_by_default_and_on_a_bad_file(self):
        self.assertFalse(claims.enabled(self.home))
        (self.home / ".claude" / "settings.json").write_text("{not json")
        self.assertFalse(claims.enabled(self.home))

    def test_off_ptask_still_records(self):
        self.assertFalse(claims.enabled(self.home))
        self.assertEqual(self.mine()["status"], "claimed")


class LockOrder(Base):
    """Lock order, for the paths this core has: `ptask take` takes wakes.lock then tasks/.lock; plain writes take
    tasks/.lock alone. An out-of-order acquisition is refused at run time, not only in tests."""

    def test_take_takes_wakes_then_tasks(self):
        r = self.mine()
        seen = []
        with mock.patch.object(coordination, "LOCK_OBSERVER", seen.append):
            self.c.take(r["id"], sid(), table=[])
        self.assertEqual([n for n in seen if n[0] == "acquire"], [("acquire", "wakes"), ("acquire", "tasks")])

    def test_a_plain_write_takes_tasks_alone(self):
        r = self.mine()
        seen = []
        with mock.patch.object(coordination, "LOCK_OBSERVER", seen.append):
            self.c.touch(r["id"], self.me)
        self.assertEqual([n for n in seen if n[0] == "acquire"], [("acquire", "tasks")])

    def test_out_of_order_is_refused(self):
        with self.c.lock("tasks"):
            with self.assertRaises(claims.LockOrderError):
                with self.c.lock("wakes"):
                    pass
        with self.c.lock("fired"):
            with self.assertRaises(claims.LockOrderError):
                with self.c.lock("tasks"):
                    pass

    def test_no_lock_is_held_when_the_launcher_is_called(self):
        r = self.mine()
        held = []
        real = claims.launch
        with mock.patch.object(claims, "launch", side_effect=lambda *a, **k: (held.append(list(claims.held_locks())), real(*a, **k))[1]):
            fd = self.c.open_cwd(r["cwd"])
            claims.launch(fd, [sys.executable, "-c", "pass"]).wait(10)
        self.assertEqual(held, [[]])


class Retention(Base):
    """Ember 1570189950421565481, point 5: no archiving in v1. Terminal records stay in place, no command moves them as a
    side effect, nothing is deleted, and `ptask show` reads them where they are."""

    def test_terminal_records_stay_in_place_through_every_command(self):
        d, a, live = self.mine(), self.mine(), self.mine()
        self.c.done(d["id"], self.me, outcome="built")
        self.c.abandon(a["id"], self.me)
        before = {n: (self.state / "tasks" / n).read_bytes() for n in (f"{d['id']}.json", f"{a['id']}.json")}
        self.clock.advance(days=400)
        env = {"CLAUDE_CODE_SESSION_ID": self.me}
        for args in (["list"], ["show", d["id"]], ["show", a["id"]], ["claim", "another"], ["touch", live["id"]],
                     ["pause", live["id"], "--until", "2027-01-01T00:00:00Z", "why"], ["resume", live["id"]]):
            claims.cli(args, env=env, state=self.state, home=self.home, cwd=str(self.home / "work"), out=lambda t: None, now=self.clock)
        self.c.settle_pauses()
        for name, data in before.items():
            self.assertEqual((self.state / "tasks" / name).read_bytes(), data)
        self.assertFalse((self.state / "tasks" / "closed").exists())
        self.assertEqual(self.c.get(d["id"])["status"], "done")

    def test_list_leaves_terminal_ones_out_and_show_finds_them(self):
        d = self.mine(task="finished thing")
        self.c.done(d["id"], self.me)
        lines = []
        claims.cli(["list"], env={}, state=self.state, home=self.home, out=lines.append, now=self.clock)
        self.assertNotIn(d["id"], "\n".join(lines))
        lines = []
        claims.cli(["show", d["id"]], env={}, state=self.state, home=self.home, out=lines.append, now=self.clock)
        self.assertIn(d["id"], "\n".join(lines))


class OwnerOnly(Base):
    """Ember 1570189950421565481, point 6: only the exact owner session may touch, pause, resume, done or abandon a
    claimed task; take is the one guarded transfer; the harness may do only the spawn claim and pause expiry; a room
    session may abandon an open hand-off before spawn but never close or alter a claimed task."""

    def others(self):
        prefix_twin = self.me[:9] + str(uuid.uuid4())[9:]  # the same first 8 (and the dash): an owner-prefix collision
        return {"another session of this self": sid(), "an owner-prefix collision": prefix_twin,
                "a prefix of the owner": self.me[:8], "the harness": "harness", "pause-expiry": "pause-expiry",
                "the heartbeat": "heartbeat"}

    def test_nobody_but_the_exact_owner_may_write_a_claimed_or_paused_claim(self):
        r, p = self.mine(), self.mine()
        self.c.pause(p["id"], self.me)
        calls = [("touch", r, {}), ("pause", r, {}), ("done", r, {}), ("abandon", r, {}),
                 ("touch", p, {}), ("resume", p, {}), ("done", p, {}), ("abandon", p, {})]
        for who, by in self.others().items():
            for verb, rec, kw in calls:
                with self.subTest(by=who, verb=verb, status=self.c.get(rec["id"])["status"]):
                    path = self.state / "tasks" / f"{rec['id']}.json"
                    before = path.read_bytes()
                    with self.assertRaises(claims.Refused):
                        getattr(self.c, verb)(rec["id"], by, **kw)
                    self.assertEqual(path.read_bytes(), before)

    def test_the_exact_owner_may(self):
        r = self.mine()
        self.c.touch(r["id"], self.me)
        self.c.pause(r["id"], self.me)
        self.c.resume(r["id"], self.me)
        self.assertEqual(self.c.done(r["id"], self.me)["status"], "done")

    def test_a_room_session_may_abandon_an_open_hand_off_but_not_a_claimed_task(self):
        room = sid()
        env = {"CLAUDE_CODE_SESSION_ID": room, "PERSISTENCE_ROOM_CHANNEL": "1569586907955073130"}
        o = self.c.create("h", owner=None, cwd=str(self.home / "work"), by=room, dispatch="handoff", status="open")
        r = self.mine()
        errs = []
        for verb in ("done", "abandon", "touch", "pause"):
            path = self.state / "tasks" / f"{r['id']}.json"
            before = path.read_bytes()
            self.assertEqual(claims.cli([verb, r["id"]], env=env, state=self.state, home=self.home, out=lambda t: None,
                                        err=errs.append, now=self.clock), 1, verb)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(claims.cli(["abandon", o["id"], "not needed"], env=env, state=self.state, home=self.home,
                                    out=lambda t: None, err=errs.append, now=self.clock), 0, errs)
        after = self.c.get(o["id"])
        self.assertEqual((after["status"], after["history"][-1]["by"]), ("abandoned", room))

    def test_the_harness_may_not_close_or_abandon_an_open_one_either(self):
        o = self.c.create("h", owner=None, cwd=str(self.home / "work"), by="harness", dispatch="handoff", status="open")
        for actor in ("harness", "pause-expiry", "heartbeat", "abc12345"):
            with self.subTest(by=actor), self.assertRaises(claims.Refused):
                self.c.abandon(o["id"], actor)
        self.assertEqual(self.c.get(o["id"])["status"], "open")


class OpenRecords(Base):
    """Ember 1570189950421565481, point 7: no general syntax for claiming an existing open record in v1. `ptask claim`
    creates a new claimed/none record owned by its caller; open/handoff is claimed only by run-due at spawn (the
    harness); open/none has no assignment; take does not apply to open records."""

    def open(self, dispatch="handoff"):
        return self.c.create("h", owner=None, cwd=str(self.home / "work"), by="harness", dispatch=dispatch, status="open")

    def test_only_the_harness_claims_an_open_hand_off(self):
        o = self.open()
        path = self.state / "tasks" / f"{o['id']}.json"
        before = path.read_bytes()
        for actor in (self.me, sid(), "pause-expiry", "heartbeat"):
            with self.subTest(by=actor), self.assertRaises(claims.Refused):
                self.c.claim_open(o["id"], self.me, by=actor, firing=sid())
        self.assertEqual(path.read_bytes(), before)
        after = self.c.claim_open(o["id"], self.me, by="harness", firing=sid())
        self.assertEqual((after["status"], after["owner"], after["history"][-1]["by"]), ("claimed", self.me, "harness"))

    def test_an_open_none_record_is_never_claimed_or_taken(self):
        o = self.open(dispatch="none")
        path = self.state / "tasks" / f"{o['id']}.json"
        before = path.read_bytes()
        with self.assertRaises(claims.Refused):
            self.c.claim_open(o["id"], self.me, by="harness", firing=sid())
        with self.assertRaises(claims.Refused):
            self.c.take(o["id"], self.me, table=[], items=[])
        self.assertEqual(path.read_bytes(), before)

    def test_take_does_not_apply_to_an_open_hand_off(self):
        o = self.open()
        with self.assertRaises(claims.Refused):
            self.c.take(o["id"], self.me, table=[], items=[])

    def test_ptask_claim_naming_an_open_record_creates_a_new_claim_and_leaves_the_open_one(self):
        o = self.open()
        path = self.state / "tasks" / f"{o['id']}.json"
        before = path.read_bytes()
        lines = []
        self.assertEqual(claims.cli(["claim", o["id"]], env={"CLAUDE_CODE_SESSION_ID": self.me}, state=self.state,
                                    home=self.home, cwd=str(self.home / "work"), out=lines.append, now=self.clock), 0)
        self.assertEqual(path.read_bytes(), before)
        new = [r for r in self.c.all() if r["id"] != o["id"]]
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0]["status"], new[0]["dispatch"], new[0]["owner"]), ("claimed", "none", self.me))


if __name__ == "__main__":
    unittest.main()
