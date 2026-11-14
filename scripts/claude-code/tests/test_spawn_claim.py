"""Run-due's spawn claim, the claims side (docs/design/task-claims.md revision 4, sections 2.4 and 4 "Locks" and "Crash
between the two files"; Ember 1570181248079626261 and Arden 1570189766400417927: built in the claims PR with only the
shared pieces it needs). Tests split per Ember 1570181249534795788, point 3: spawn ownership and the claims side of the
contract here, with an integration test at the seam; stall decisions, retries, deferrals, reports and cross-document
scenarios are the heartbeat PR's.

`pwake` from a room session writes an open/handoff claim and a wake item carrying only its id; run-due, in its locked
transition, moves the claim open -> claimed by the pre-minted id (`by: harness`, `firing` = the item's id), appends
the `enqueued` fact, rewrites the queue, releases every lock, then launches through cwd-exec in the claim's cwd."""
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "testguard"))
import no_real_claude  # noqa: E402,F401  first: no test can start a real claude (Arden 1570199499278909491)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hook
import claims
import coordination

ROOM = "1569586907955073130"


def sid():
    return str(uuid.uuid4())


class Launched:
    """A fake launcher in claims.launch's place: records the directory the fd names, argv, environment, stdin text and
    the locks held when it is called, closes the fd, starts nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, fd, argv, **popen):
        where = os.readlink(f"/proc/self/fd/{fd}")
        os.close(fd)
        stdin = popen.get("stdin")
        text = Path(stdin.name).read_text(encoding="utf-8") if hasattr(stdin, "name") else None
        self.calls.append({"cwd": where, "argv": list(argv), "env": dict(popen.get("env") or {}), "prompt": text,
                           "held": coordination.held_locks()})
        return mock.Mock(pid=0)


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
        self.room = sid()
        self.launched = Launched()
        self.patches = [
            mock.patch.object(hook, "STATE", self.state),
            mock.patch.object(hook, "WAKES", self.state.parent / "wakes.json"),
            mock.patch.object(hook, "store", side_effect=AssertionError("the spawn called the store")),
            mock.patch.object(hook, "channel_history", return_value=None),
            mock.patch.object(hook, "server_environ", side_effect=lambda e: dict(e)),
            mock.patch.object(claims, "process_table", return_value=[]),
            mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                         if not k.startswith("PERSISTENCE_") and k != "CLAUDE_CODE_SESSION_ID"}, clear=True),
        ]
        for p in self.patches:
            p.start()
        # The launcher is faked, and any other process start fails the test; the seam test lifts both.
        self.fakes = [mock.patch.object(claims, "launch", self.launched),
                      mock.patch.object(hook.subprocess, "Popen", side_effect=AssertionError("a process started outside cwd-exec"))]
        for p in self.fakes:
            p.start()
        self.c = claims.Claims(self.state, home=self.home)

    def tearDown(self):
        for p in reversed(self.fakes):
            p.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def pwake(self, task="Build the spawn claim", minutes="0", flags=()):
        with mock.patch.dict(os.environ, {"PERSISTENCE_ROOM_CHANNEL": ROOM, "CLAUDE_CODE_SESSION_ID": self.room}), \
                mock.patch.object(hook.Path, "home", return_value=self.home):
            wake = hook.wake_request([minutes, *flags, *task.split(" ")])
        return hook.set_handoff(wake, home=self.home)

    def run_due(self):
        return hook.run_due(home=self.home)

    def queue(self):
        return hook.wakes()

    def facts(self):
        return coordination.snapshot(self.state)[0]

    def claim_file(self, record):
        return self.state / "tasks" / f"{record['id']}.json"


class Pwake(Base):
    def test_pwake_from_a_room_session_writes_an_open_hand_off_and_an_item_with_its_id(self):
        record, item = self.pwake()
        stored = self.c.get(record["id"])
        self.assertEqual((stored["status"], stored["dispatch"], stored["owner"]), ("open", "handoff", None))
        self.assertEqual(stored["task"], "Build the spawn claim")
        self.assertEqual(stored["origin_session"], self.room)
        self.assertEqual(stored["room_channel"], ROOM)
        self.assertEqual(stored["cwd"], str(self.home / "work"))
        self.assertEqual([(h["action"], h["by"]) for h in stored["history"]], [("create", self.room)])
        [queued] = self.queue()
        self.assertEqual(queued["claim"], record["id"])
        self.assertEqual(queued["cwd"], stored["cwd"])
        self.assertTrue(queued["work"])
        self.assertEqual(uuid.UUID(queued["session"]).version, 4)
        self.assertNotIn("handoff", queued, "the item carries the claim id, not a copy of the record")
        self.assertEqual(uuid.UUID(queued["id"]).version, 4)

    def test_a_room_hand_off_on_a_pid_is_refused_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.pwake(minutes="pid:1")
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.c.all(), [])

    def test_pwake_takes_wakes_then_tasks(self):
        seen = []
        with mock.patch.object(coordination, "LOCK_OBSERVER", seen.append):
            self.pwake()
        self.assertEqual([e[1] for e in seen if e[0] == "acquire"], ["wakes", "tasks"])

    def test_pwake_canonicalises_the_default_directory(self):
        (self.home / "real-work").mkdir()
        (self.home / "work").rmdir()
        (self.home / "work").symlink_to(self.home / "real-work")
        record, item = self.pwake()
        self.assertEqual(record["cwd"], str(self.home / "real-work"))
        self.assertEqual(item["cwd"], str(self.home / "real-work"))


class HandOffSubject(Base):
    """The deploy guard, part 3: a room hand-off with --subject is refused while a claim for that subject is live, and
    writes neither a claim nor a queue item; the room must close the old claim first (the 05:20Z second deploy claim)."""

    def test_a_second_hand_off_for_a_live_subject_is_refused_and_writes_nothing(self):
        first, _ = self.pwake("Stage 1 deploy at b3be589", flags=("--subject", "deploy"))
        self.assertEqual(first["subject"], "deploy")
        queue, files = self.queue(), sorted((self.state / "tasks").glob("*.json"))
        with self.assertRaises(claims.Refused) as e:
            self.pwake("Stage 1 deploy: notice 05:55Z", flags=("--subject", "deploy"))
        self.assertIn(first["id"], str(e.exception))
        self.assertEqual(self.queue(), queue)
        self.assertEqual(sorted((self.state / "tasks").glob("*.json")), files)

    def test_after_the_old_claim_is_abandoned_the_hand_off_goes_through(self):
        first, _ = self.pwake("Deploy", flags=("--subject", "deploy"))
        self.c.abandon(first["id"], self.room)
        second, item = self.pwake("Deploy", flags=("--subject", "deploy"))
        self.assertEqual((second["subject"], item["claim"]), ("deploy", second["id"]))

    def test_the_claim_run_due_takes_keeps_the_subject_so_it_still_blocks(self):
        first, _ = self.pwake("Deploy", flags=("--subject", "deploy"))
        self.run_due()
        self.assertEqual(self.c.get(first["id"])["status"], "claimed")
        with self.assertRaises(claims.Refused):
            self.pwake("Deploy again", flags=("--subject", "deploy"))

    def test_subject_is_for_a_room_hand_off_only(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": self.room}):
            with self.assertRaises(ValueError):
                hook.wake_request(["5", "--subject", "deploy", "carry", "on"])

class SpawnClaim(Base):
    """Claim at spawn: a hand-off worker that never calls ptask leaves the claim claimed by its pre-minted id, and the
    next run starts nothing."""

    def test_run_due_claims_at_spawn_and_launches_the_pre_minted_id_in_the_claims_cwd(self):
        record, item = self.pwake()
        self.run_due()
        after = self.c.get(record["id"])
        self.assertEqual((after["status"], after["owner"]), ("claimed", item["session"]))
        last = after["history"][-1]
        self.assertEqual((last["action"], last["by"], last["firing"], last["from_status"], last["to_status"]),
                         ("claim", "harness", item["id"], "open", "claimed"))
        [call] = self.launched.calls
        self.assertEqual(call["cwd"], after["cwd"])
        argv = call["argv"]
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], item["session"])
        self.assertNotIn("--resume", argv)
        self.assertIn(record["id"], call["prompt"])
        self.assertEqual(self.queue(), [])
        [fact] = self.facts()
        self.assertEqual((fact["fact"], fact["firing"], fact["claim"], fact["session"], fact["cwd"]),
                         ("enqueued", item["id"], record["id"], item["session"], after["cwd"]))

    def test_a_worker_that_never_runs_ptask_leaves_it_claimed_and_the_next_run_starts_nothing(self):
        record, item = self.pwake()
        self.run_due()
        before = self.claim_file(record).read_bytes()
        for _ in range(3):
            self.run_due()
        self.assertEqual(len(self.launched.calls), 1)
        self.assertEqual(self.claim_file(record).read_bytes(), before)
        self.assertEqual(len(self.facts()), 1)

    def test_with_task_claims_off_the_spawn_claim_is_still_recorded(self):
        """Ember 1570181248079626261 point 2, Arden 1570189766400417927 point 2: off means no promise check and no
        heartbeat automation, not no ownership record."""
        for value in (None, "0"):
            with self.subTest(setting=value):
                env = {} if value is None else {"PERSISTENCE_TASKCLAIMS": value}
                (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))
                self.assertFalse(claims.enabled(self.home))
                record, item = self.pwake()
                self.run_due()
                after = self.c.get(record["id"])
                self.assertEqual((after["status"], after["owner"], after["history"][-1]["by"]), ("claimed", item["session"], "harness"))

    def test_a_live_owner_by_exact_id_holds_the_item_and_a_prefix_or_another_id_does_not(self):
        record, item = self.pwake()
        s = item["session"]
        with mock.patch.object(claims, "process_table", return_value=[["claude", "--session-id", s]]):
            self.run_due()
        self.assertEqual(self.launched.calls, [])
        self.assertEqual(self.c.get(record["id"])["status"], "open")
        self.assertEqual(len(self.queue()), 1)
        with mock.patch.object(claims, "process_table", return_value=[["claude", "-p", "--resume", s[:8]], ["claude", "--session-id", sid()],
                                                                      ["claude", f"--resume={s}"], ["grep", s]]):
            self.run_due()
        self.assertEqual(len(self.launched.calls), 1)

    def test_free_text_of_the_task_reaches_no_argv_environment_or_file_name(self):
        marker = "MARKERX --resume {cwd} $(rm -rf ~)"
        record, item = self.pwake(task=marker)
        self.run_due()
        [call] = self.launched.calls
        self.assertNotIn("MARKERX", " ".join(call["argv"]))
        self.assertFalse(any("MARKERX" in str(k) + str(v) for k, v in call["env"].items()))
        for path in self.home.rglob("*"):
            self.assertNotIn("MARKERX", path.name)
        self.assertIn(marker, call["prompt"], "the task reaches the worker only as the prompt's text")


class SurvivedMutations(Base):
    """Arden's mutations I, J and G (1570199516513046549), which survived cf63ad0's tests."""

    def test_spawn_locked_refuses_an_open_claim_with_dispatch_none(self):
        """I: Ember's point 7. An open record with dispatch none is unowned and nothing assigns it in v1, so the spawn
        claim refuses it as claim_open does, writing nothing and binding no descriptor."""
        record = self.c.create("not a hand-off", owner=None, cwd=str(self.home / "work"), by=self.room, dispatch="none",
                               status="open")
        before = self.claim_file(record).read_bytes()
        opened = []
        with mock.patch.object(self.c, "open_cwd", side_effect=lambda cwd: opened.append(cwd)), \
                coordination.lock(self.state, "wakes"), coordination.lock(self.state, "tasks"):
            with self.assertRaises(claims.Refused):
                self.c.spawn_locked(record["id"], sid(), sid(), record["cwd"])
        self.assertEqual(self.claim_file(record).read_bytes(), before)
        self.assertEqual(opened, [])

    def test_run_due_with_dispatch_none_on_the_item_s_claim_launches_nothing(self):
        record = self.c.create("not a hand-off", owner=None, cwd=str(self.home / "work"), by=self.room, dispatch="none",
                               status="open")
        _, item = self.pwake()
        hook.wakes(lambda items: [dict(w, claim=record["id"]) for w in items])
        self.run_due()
        self.assertEqual(self.launched.calls, [])
        self.assertEqual((self.c.get(record["id"])["status"], self.c.get(record["id"])["owner"]), ("open", None))

    def test_unknown_liveness_waits(self):
        """J: when the process table can't be read, claims.running is None, and the wake waits: no claim, no fact,
        no launch, the item stays queued."""
        record, item = self.pwake()
        before = self.claim_file(record).read_bytes()
        with mock.patch.object(claims, "process_table", return_value=None):
            self.run_due()
        self.assertIsNone(claims.running(item["session"], None))
        self.assertEqual(self.launched.calls, [])
        self.assertEqual(self.claim_file(record).read_bytes(), before)
        self.assertEqual(self.facts(), [])
        self.assertEqual([w["id"] for w in self.queue()], [item["id"]])
        self.run_due()  # the table readable again: it fires
        self.assertEqual(len(self.launched.calls), 1)

    def test_a_mixed_due_queue_takes_tasks_lock_and_fires_both(self):
        """G: one due item names a claim and one doesn't. tasks/.lock is taken because any item names a claim, the
        hand-off is spawned by its claim, and the plain item still goes the plain way."""
        record, item = self.pwake()
        other = sid()
        hook.wakes(lambda items: items + [{"id": str(uuid.uuid4()), "reason": "carry on", "session": other, "at": 0,
                                           "set": "2026-11-11T21:00:00+00:00"}])
        plain, seen = [], []
        with mock.patch.object(hook, "_resume_or_start", side_effect=lambda s, mine: plain.append((s, [w["reason"] for w in mine]))), \
                mock.patch.object(coordination, "LOCK_OBSERVER", seen.append):
            try:
                self.run_due()
            except coordination.LockOrderError as e:
                self.fail(f"run-due reached the spawn claim without tasks/.lock: {e}")
        self.assertIn(("acquire", "tasks"), seen)
        [call] = self.launched.calls
        self.assertEqual(call["argv"][call["argv"].index("--session-id") + 1], item["session"])
        self.assertEqual(plain, [(other, ["carry on"])])
        self.assertEqual(self.c.get(record["id"])["status"], "claimed")
        self.assertEqual(self.queue(), [])


class EachLaunchOnItsOwn(Base):
    """Arden's nit (1570199516513046549): one failed launch doesn't drop the ones after it, and a launch that fails
    before cwd-exec takes its descriptor leaves none open."""

    def open_fds(self):
        return set(os.listdir("/proc/self/fd"))

    def test_a_failed_launch_doesn_t_drop_the_next(self):
        first, _ = self.pwake()
        second, _ = self.pwake(task="The second hand-off")
        real, calls = self.launched, []

        def fail_first(fd, argv, **popen):
            calls.append(fd)
            if len(calls) == 1:
                os.close(fd)
                raise OSError("the first launch failed")
            return real(fd, argv, **popen)
        with mock.patch.object(claims, "launch", fail_first):
            with self.assertRaisesRegex(OSError, "the first launch failed"):
                self.run_due()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.launched.calls), 1)

    def test_a_launch_failing_before_cwd_exec_closes_its_descriptor(self):
        self.pwake()
        before = self.open_fds()
        with mock.patch.object(hook, "server_environ", side_effect=RuntimeError("before the launch")):
            with self.assertRaises(RuntimeError):
                self.run_due()
        self.assertEqual(self.launched.calls, [])
        self.assertEqual(self.open_fds(), before)


class SpawnLockOrder(Base):
    """Lock order, the spawn path (Ember 1570158593645355039): wakes.lock -> tasks/.lock -> fired.lock, fired.lock
    released before the queue rewrite, all three released before the launcher is called."""

    def test_the_recorder_sees_the_spawn_paths_order(self):
        self.pwake()
        seen = []
        with mock.patch.object(coordination, "LOCK_OBSERVER", seen.append):
            self.run_due()
        self.assertEqual(seen, [("acquire", "wakes"), ("acquire", "tasks"), ("acquire", "fired"), ("release", "fired"),
                                ("acquire", "fired"), ("release", "fired"), ("write", "wakes.json"),
                                ("release", "tasks"), ("release", "wakes")])
        self.assertEqual([c["held"] for c in self.launched.calls], [[]])

    def test_the_enqueued_append_happens_while_wakes_and_tasks_are_held(self):
        self.pwake()
        held = []
        real = coordination.append

        def spy(state, fact):
            held.append((fact["fact"], coordination.held_locks()))
            return real(state, fact)
        with mock.patch.object(coordination, "append", spy):
            self.run_due()
        self.assertEqual(held, [("enqueued", ["wakes", "tasks"])])


class SplitWrites(Base):
    """Crash between the two files, the spawn's halves (task-claims.md section 4): reconciled by firing id. The next
    run-due leaves exactly one claim entry and one enqueued fact per firing, and at most one worker."""

    def test_crash_after_the_claim_before_the_enqueued_append(self):
        record, item = self.pwake()
        with mock.patch.object(coordination, "append", side_effect=RuntimeError("killed after step 1")):
            with self.assertRaises(RuntimeError):
                self.run_due()
        mid = self.c.get(record["id"])
        self.assertEqual((mid["status"], mid["history"][-1]["firing"]), ("claimed", item["id"]))
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.launched.calls, [])
        self.run_due()
        after = self.c.get(record["id"])
        self.assertEqual(after["history"], mid["history"], "no second claim entry")
        self.assertEqual([f["firing"] for f in self.facts()], [item["id"]])
        self.assertEqual(self.queue(), [])
        self.assertEqual(len(self.launched.calls), 1)
        self.run_due()
        self.assertEqual(len(self.launched.calls), 1)

    def test_crash_after_the_enqueued_append_before_the_queue_rewrite(self):
        record, item = self.pwake()
        with mock.patch.object(hook, "write_queue_locked", side_effect=RuntimeError("killed after step 2")):
            with self.assertRaises(RuntimeError):
                self.run_due()
        mid = self.c.get(record["id"])
        self.assertEqual(mid["status"], "claimed")
        self.assertEqual(len(self.queue()), 1)
        self.run_due()
        self.assertEqual(self.queue(), [], "the item whose firing is already enqueued is only removed")
        self.assertEqual(self.launched.calls, [], "nothing fired: the claim is now owner never started, the heartbeat's")
        self.assertEqual(self.c.get(record["id"])["history"], mid["history"])
        self.assertEqual(len(self.facts()), 1)

    def test_enqueued_with_the_claim_still_open_writes_the_spawn_claim_and_launches_nothing(self):
        record, item = self.pwake()
        hook.wakes(lambda items: items)  # the id is already minted; nothing changes
        with coordination.lock(self.state, "wakes"):
            coordination.append(self.state, coordination.enqueued_fact(self.queue()[0], "2026-11-11T21:00:00+00:00"))
        self.run_due()
        after = self.c.get(record["id"])
        self.assertEqual((after["status"], after["owner"], after["history"][-1]["firing"]), ("claimed", item["session"], item["id"]))
        self.assertEqual(self.launched.calls, [])
        self.assertEqual(self.queue(), [])
        self.assertEqual(len(self.facts()), 1)

    def test_a_claim_already_claimed_by_another_firing_is_not_this_ones(self):
        """Reconciled by firing id, not claim id: an item for a claim the harness claimed for another firing is
        refused, writes no entry, launches nothing."""
        record, item = self.pwake()
        self.c.claim_open(record["id"], item["session"], by="harness", firing=sid())
        before = self.claim_file(record).read_bytes()
        self.run_due()
        self.assertEqual(self.launched.calls, [])
        self.assertEqual(self.claim_file(record).read_bytes(), before)
        self.assertEqual(self.queue(), [])
        self.assertEqual([f["firing"] for f in self.facts()], [item["id"]])


class CwdThroughTheHandOff(Base):
    """`cwd` through the hand-off path: the claim and item carry the same cwd and the spawn runs there; an item whose
    cwd differs from its claim's is refused; a vanished or retargeted cwd gets no spawn. A refusal is before the spawn
    claim: the claim stays open, the firing is enqueued (never-started), the item leaves the queue."""

    def refused(self, record):
        self.run_due()
        self.assertEqual(self.launched.calls, [])
        after = self.c.get(record["id"])
        self.assertEqual((after["status"], after["owner"]), ("open", None))
        self.assertEqual(self.queue(), [])
        self.assertEqual(len(self.facts()), 1)

    def test_an_item_whose_cwd_differs_from_its_claims_is_refused(self):
        (self.home / "work" / "other").mkdir()
        record, item = self.pwake()
        hook.wakes(lambda items: [dict(w, cwd=str(self.home / "work" / "other")) for w in items])
        self.refused(record)

    def test_a_vanished_cwd_gets_no_spawn(self):
        record, item = self.pwake()
        (self.home / "work").rmdir()
        self.refused(record)

    def test_a_cwd_retargeted_by_a_symlink_after_pwake_gets_no_spawn(self):
        record, item = self.pwake()
        (self.home / "work").rename(self.home / "work.aside")
        (self.home / "work").symlink_to(self.outside)
        self.refused(record)

    def test_the_launch_uses_the_descriptor_bound_in_the_transition_not_the_path_again(self):
        """Retargeted after run-due bound the cwd and before it launched: the worker still starts in the directory that
        was verified, not where the string now points."""
        record, item = self.pwake()
        real = hook._run_due_locked

        def then_swap(*a, **k):
            real(*a, **k)
            (self.home / "work").rename(self.home / "work.aside")
            (self.home / "work").symlink_to(self.outside)
        with mock.patch.object(hook, "_run_due_locked", then_swap):
            self.run_due()
        [call] = self.launched.calls
        self.assertEqual(call["cwd"], str(self.home / "work.aside"))

    def test_a_missing_claim_is_refused(self):
        record, item = self.pwake()
        self.claim_file(record).unlink()
        self.run_due()
        self.assertEqual(self.launched.calls, [])
        self.assertEqual(self.queue(), [])


class Seam(Base):
    """The integration test at the seam: pwake -> run-due -> the real cwd-exec launching a fake worker, which then
    reads its claim with ptask show. The claim is claimed by the id the worker was started with, in the cwd it runs in."""

    def test_pwake_run_due_and_the_real_launcher(self):
        import time

        out = self.outside / "worker.json"
        here = str(Path(claims.__file__).resolve().parent)
        worker = ("import json, os, sys; sys.path.insert(0, sys.argv[1]); import claims\n"
                  "args = sys.argv[3:]; session = args[args.index('--session-id') + 1]\n"
                  "mine = claims.Claims(os.environ['TEST_STATE'], home=os.environ['TEST_HOME']).by_owner(session)\n"
                  "json.dump({'cwd': os.getcwd(), 'argv': args, 'claim': mine and mine['id'], 'stdin': sys.stdin.read()}, open(sys.argv[2], 'w'))")
        record, item = self.pwake()
        # Only the launcher is lifted. Popen stays guarded: the one process it may start is cwd-exec running the fake
        # worker, and the plain path is refused outright, so a wrong run_due (Arden's mutation R: an item with no claim
        # took _resume_or_start and started a real `claude -p` with the tester's PATH and HOME) fails here, launching
        # nothing.
        self.fakes[1].stop()
        real_popen = hook.subprocess.Popen
        started = []

        def only_cwd_exec(argv, *a, **k):
            if list(argv[:3]) != [sys.executable, "-I", str(claims.CWD_EXEC)] or list(argv[4:7]) != [sys.executable, "-c", worker]:
                raise AssertionError(f"a process other than cwd-exec running the fake worker: {argv!r}")
            started.append(argv)
            return real_popen(argv, *a, **k)
        self.fakes[0].stop()
        guards = [mock.patch.object(hook.subprocess, "Popen", only_cwd_exec),
                  mock.patch.object(hook, "_resume_or_start", side_effect=AssertionError("the plain path, not the claim's"))]
        for g in guards:
            g.start()
        try:
            with mock.patch.object(hook, "WORK_ARGV", [sys.executable, "-c", worker, here, str(out)]), \
                    mock.patch.dict(os.environ, {"TEST_STATE": str(self.state), "TEST_HOME": str(self.home)}):
                self.run_due()
            for _ in range(200):
                if out.exists() and out.stat().st_size:
                    break
                time.sleep(0.05)
        finally:
            for g in reversed(guards):
                g.stop()
            for p in self.fakes:
                p.start()
        self.assertEqual(len(started), 1)
        self.assertTrue(out.exists(), "the worker ran (it exits at once without --session-id)")
        seen = json.loads(out.read_text())
        self.assertEqual(seen["cwd"], str(self.home / "work"))
        self.assertEqual(seen["argv"][seen["argv"].index("--session-id") + 1], item["session"])
        self.assertNotIn("--resume", seen["argv"])
        self.assertEqual(seen["claim"], record["id"])
        self.assertIn(record["id"], seen["stdin"])


if __name__ == "__main__":
    unittest.main()
