"""The opt-in preflight (heartbeat_optin.py): a read-only check that says whether this self is ready to turn its heartbeat
on, and refuses to say "ready" while a switch would fail silently. Fake home, fake systemctl, fake PATH: nothing real
is read or changed, and no test calls a model."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "testguard"))
import no_real_claude  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import heartbeat  # noqa: E402
import heartbeat_optin  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
# What the core will look like once Arden's STALLED code lands (heartbeat-core-interface.md: heartbeat.stalled_on(home)),
# with a83ef07's rule: the settings file only, "true" or "1".
STALLED_CORE = ("\n\ndef stalled_on(home=None):\n"
                "    return _setting(home, \"PERSISTENCE_HEARTBEAT_STALLED\", \"\").strip().lower() in (\"true\", \"1\")\n")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class Systemctl:
    """Answers `show -p LoadState --value -- <unit>` and `is-enabled -- <unit>` only; anything else fails the test. Not
    test_heartbeat_core.Systemctl: that one refuses these two verbs, as the beat must never use them."""

    def __init__(self, missing=(), enabled=()):
        self.missing, self.enabled, self.calls = set(missing), set(enabled), []

    def __call__(self, argv):
        self.calls.append(list(argv))
        assert argv[0] == "systemctl" and argv[-2] == "--", argv
        verb, unit = argv[1], argv[-1]
        if verb == "show" and argv[2:5] == ["-p", "LoadState", "--value"]:
            return 0, ("not-found\n" if unit in self.missing else "loaded\n")
        if verb == "is-enabled" and len(argv) == 4:
            return (0, "enabled\n") if unit in self.enabled else (1, "disabled\n")
        raise AssertionError(f"systemctl {verb} must never run")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.deployed = self.home / ".claude" / "persistence"
        self.deployed.mkdir(parents=True)
        # A throwaway git repo standing in for the reviewed checkout: the preflight compares against a commit's
        # blobs, never the working files. Its heartbeat.py already has the STALLED core.
        self.root = self.tmp / "repo"
        self.repo = self.root / "scripts" / "claude-code"
        self.repo.mkdir(parents=True)
        for name in heartbeat_optin.DEPLOYED:
            shutil.copy(REPO / name, self.repo / name)
        with open(self.repo / "heartbeat.py", "a", encoding="utf-8") as f:
            f.write(STALLED_CORE)
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "reviewed")
        self.reviewed = git(self.root, "rev-parse", "HEAD")
        for name in heartbeat_optin.DEPLOYED:
            shutil.copy(self.repo / name, self.deployed / name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "pheartbeat").write_text('#!/bin/sh\nexec python3 "$HOME/.claude/persistence/heartbeat.py" "$@"\n')
        (self.bin / "pheartbeat").chmod(0o755)
        self.systemctl = Systemctl()
        self.env = {"PERSISTENCE_HEARTBEAT": "true", "PERSISTENCE_TASKCLAIMS": "1"}

    def settings(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": self.env}), encoding="utf-8")

    def check(self, user="ubuntu", stalled=False, at=None):
        self.settings()
        return heartbeat_optin.check(home=self.home, user=user, repo=self.repo, systemctl=self.systemctl,
                                     path=str(self.bin), stalled=stalled, at=at or self.reviewed)

    def item(self, items, name):
        return next(i for i in items if i.name == name)

    def failing(self, items):
        return {i.name for i in items if i.required and not i.ok}


class Ready(Base):
    def test_everything_in_place_is_ready(self):
        items = self.check()
        self.assertEqual(self.failing(items), set())
        self.assertTrue(heartbeat_optin.ready(items))

    def test_stalled_ready_with_the_source_and_the_wrapper(self):
        self.env.update(PERSISTENCE_HEARTBEAT_STALLED="true", PERSISTENCE_OBSERVE_SOURCES="prs,heartbeat")
        self.assertEqual(self.failing(self.check(stalled=True)), set())

    def test_read_only_systemctl_verbs(self):
        self.check(stalled=True)
        self.assertTrue(self.systemctl.calls)
        self.assertLessEqual({c[1] for c in self.systemctl.calls}, {"show", "is-enabled"})

    def test_writes_nothing(self):
        self.settings()
        before = sorted((p, p.stat().st_mtime_ns) for p in self.tmp.rglob("*"))
        heartbeat_optin.check(home=self.home, user="ubuntu", repo=self.repo, systemctl=self.systemctl,
                              path=str(self.bin), stalled=True, at=self.reviewed)
        self.assertEqual(sorted((p, p.stat().st_mtime_ns) for p in self.tmp.rglob("*")), before)


class NotReady(Base):
    def test_deployed_file_missing(self):
        (self.deployed / "heartbeat.py").unlink()
        self.assertIn("deployed heartbeat.py", self.failing(self.check()))

    def test_deployed_file_differs_from_the_tree(self):
        (self.deployed / "coordination.py").write_text("# old\n")
        self.assertIn("deployed coordination.py", self.failing(self.check()))

    def test_every_module_heartbeat_imports_is_checked(self):
        # The beat imports these beside itself; an old copy of any of them runs the new heartbeat against old rules.
        self.assertLessEqual({"heartbeat.py", "coordination.py", "claims.py", "hook.py", "observed.py"},
                             set(heartbeat_optin.DEPLOYED))

    def test_checks_exactly_what_deploy_remote_installs(self):
        # Ember's point 2: run_line.py (heartbeat.wrapper_alive imports it) and cwd_exec.py (claims spawns through it)
        # were deployed but never compared. One list, so the two can't drift again.
        text = (REPO / "deploy-remote.sh").read_text()
        py = re.search(r"^py=\(([^)]*)\)", text, re.M).group(1).split()
        self.assertEqual(set(heartbeat_optin.DEPLOYED), set(py))

    def test_stale_run_line_is_not_ready(self):
        (self.deployed / "run_line.py").write_text("# an old run line\n")
        self.assertIn("deployed run_line.py", self.failing(self.check()))

    def test_missing_cwd_exec_is_not_ready(self):
        (self.deployed / "cwd_exec.py").unlink()
        self.assertIn("deployed cwd_exec.py", self.failing(self.check()))

    def test_an_unreviewed_edit_deployed_identically_is_not_ready(self):
        # Ember's point 2: "the tree" was the working files, so an uncommitted edit deployed the same way passed.
        edited = (self.repo / "heartbeat.py").read_text() + "\n# unreviewed edit\n"
        (self.repo / "heartbeat.py").write_text(edited)
        (self.deployed / "heartbeat.py").write_text(edited)
        self.assertIn("deployed heartbeat.py", self.failing(self.check()))

    def test_compared_against_a_named_commit(self):
        detail = self.item(self.check(), "deployed heartbeat.py").detail
        self.assertIn(self.reviewed, detail)

    def test_pinned_to_the_reviewed_commit(self):
        # A later commit, deployed, is not the reviewed one: --at <reviewed> refuses it, --at HEAD accepts it.
        (self.repo / "claims.py").write_text((self.repo / "claims.py").read_text() + "\n# later\n")
        git(self.root, "commit", "-qam", "later, unreviewed")
        shutil.copy(self.repo / "claims.py", self.deployed / "claims.py")
        self.assertNotIn("deployed claims.py", self.failing(self.check(at="HEAD")))
        self.assertIn("deployed claims.py", self.failing(self.check(at=self.reviewed)))
        self.assertIn("deployed claims.py", self.failing(self.check(at=self.reviewed[:12])))

    def test_unknown_commit_is_not_ready(self):
        self.assertIn("commit", self.failing(self.check(at="0" * 40)))

    def test_unknown_user_has_no_units(self):
        self.assertIn("units", self.failing(self.check(user="nobody")))

    def test_a_unit_in_the_tuple_is_not_installed(self):
        self.systemctl.missing = {"persistence-mirror.service"}
        items = self.check()
        self.assertIn("units", self.failing(items))
        self.assertIn("persistence-mirror.service", next(i for i in items if i.name == "units").detail)

    def test_every_unit_of_the_tuple_is_asked_about(self):
        self.check()
        asked = {c[-1] for c in self.systemctl.calls if c[1] == "show"}
        self.assertLessEqual(set(heartbeat.units_for("ubuntu")), asked)

    def test_timer_not_installed(self):
        self.systemctl.missing = {"persistence-heartbeat@ubuntu.timer"}
        self.assertIn("timer", self.failing(self.check()))

    def test_claims_unset_is_a_note_not_missing(self):
        # Arden's point 3: the design lets a heartbeat run with claims off, so unset is a choice; say so, don't refuse.
        del self.env["PERSISTENCE_TASKCLAIMS"]
        items = self.check()
        self.assertNotIn("PERSISTENCE_TASKCLAIMS", self.failing(items))
        claims = self.item(items, "PERSISTENCE_TASKCLAIMS")
        self.assertFalse(claims.ok)
        self.assertFalse(claims.required)
        self.assertIn("row A", claims.detail)

    def test_claims_true_is_not_on(self):
        # claims.py reads "1" only; "true" would leave row A off while looking on.
        self.env["PERSISTENCE_TASKCLAIMS"] = "true"
        self.assertIn("PERSISTENCE_TASKCLAIMS", self.failing(self.check()))


class TheSilentFailures(Base):
    def test_stalled_on_without_the_observe_source_is_never_ready(self):
        # The footgun: R and Q wakes would fire, never render, and go undeliverable. Caught even without --stalled.
        self.env.update(PERSISTENCE_HEARTBEAT_STALLED="true", PERSISTENCE_OBSERVE_SOURCES="prs,claims")
        self.assertIn("PERSISTENCE_OBSERVE_SOURCES", self.failing(self.check()))
        self.assertIn("PERSISTENCE_OBSERVE_SOURCES", self.failing(self.check(stalled=True)))

    def test_source_name_matched_exactly(self):
        self.env.update(PERSISTENCE_HEARTBEAT_STALLED="1", PERSISTENCE_OBSERVE_SOURCES="heartbeats, prs")
        self.assertIn("PERSISTENCE_OBSERVE_SOURCES", self.failing(self.check()))
        self.env["PERSISTENCE_OBSERVE_SOURCES"] = " prs , heartbeat "
        self.assertNotIn("PERSISTENCE_OBSERVE_SOURCES", self.failing(self.check()))

    def test_stalled_on_with_the_heartbeat_off(self):
        self.env.update(PERSISTENCE_HEARTBEAT="false", PERSISTENCE_HEARTBEAT_STALLED="true",
                        PERSISTENCE_OBSERVE_SOURCES="heartbeat")
        self.assertIn("PERSISTENCE_HEARTBEAT_STALLED", self.failing(self.check()))

    def test_stalled_needs_a_core_that_has_it(self):
        # Ember's point 7: main's heartbeat.py has no STALLED code, so "ready: STALLED" would turn on nothing.
        # Built here, not taken from the tree: once the core lands, the tree's heartbeat.py has stalled_on.
        core = (self.repo / "heartbeat.py").read_text().replace("def stalled_on(", "def _not_stalled_on(")
        self.assertNotIn("def stalled_on(", core)
        (self.repo / "heartbeat.py").write_text(core)
        git(self.root, "commit", "-qam", "core without STALLED")
        self.reviewed = git(self.root, "rev-parse", "HEAD")  # the commit under review is the one without it
        shutil.copy(self.repo / "heartbeat.py", self.deployed / "heartbeat.py")
        self.env.update(PERSISTENCE_HEARTBEAT_STALLED="true", PERSISTENCE_OBSERVE_SOURCES="heartbeat")
        self.assertIn("STALLED support", self.failing(self.check(stalled=True)))
        # Already switched on in settings, it's the same silent failure at the base level.
        self.assertIn("STALLED support", self.failing(self.check()))
        # Off and not asked for: no business of the base check.
        del self.env["PERSISTENCE_HEARTBEAT_STALLED"]
        self.assertNotIn("STALLED support", self.failing(self.check()))

    def test_stalled_needs_pheartbeat_on_path(self):
        # The tick's fixed text tells the session to run `pheartbeat detect`/`continue`; without it a tick can't stop.
        (self.bin / "pheartbeat").unlink()
        self.env.update(PERSISTENCE_HEARTBEAT_STALLED="true", PERSISTENCE_OBSERVE_SOURCES="heartbeat")
        self.assertIn("pheartbeat", self.failing(self.check(stalled=True)))

    def test_pheartbeat_not_required_before_stalled(self):
        (self.bin / "pheartbeat").unlink()
        self.assertEqual(self.failing(self.check()), set())

    def test_os_environ_does_not_count(self):
        # The heartbeat reads settings.json only, so the preflight does too.
        self.env = {}
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"PERSISTENCE_TASKCLAIMS": "1"}):
            self.assertFalse(self.item(self.check(), "PERSISTENCE_TASKCLAIMS").ok)


class OneParser(Base):
    """Arden's non-blocking (a) on ec69fdf: once the core has stalled_on, the preflight asks it, not its own copy of the
    rule. The fixture's stalled_on agrees with the env rule, so these make the two disagree."""

    def core(self, body):
        core = (self.repo / "heartbeat.py").read_text() + f"\n\ndef stalled_on(home=None):\n    return {body}\n"
        (self.repo / "heartbeat.py").write_text(core)
        git(self.root, "commit", "-qam", f"stalled_on returns {body}")
        self.reviewed = git(self.root, "rev-parse", "HEAD")
        shutil.copy(self.repo / "heartbeat.py", self.deployed / "heartbeat.py")

    def test_stalled_on_says_on_while_the_setting_is_unset(self):
        self.core("True")
        self.env["PERSISTENCE_OBSERVE_SOURCES"] = "prs"
        failing = self.failing(self.check())
        self.assertIn("PERSISTENCE_OBSERVE_SOURCES", failing)  # STALLED is on as the core reads it: the footgun counts
        self.env["PERSISTENCE_HEARTBEAT"] = "false"
        self.assertIn("PERSISTENCE_HEARTBEAT_STALLED", self.failing(self.check()))

    def test_stalled_on_says_off_while_the_setting_is_true(self):
        self.core("False")
        self.env.update(PERSISTENCE_HEARTBEAT="false", PERSISTENCE_HEARTBEAT_STALLED="true",
                        PERSISTENCE_OBSERVE_SOURCES="prs")
        self.assertEqual(self.failing(self.check()), set())  # off as the core reads it: nothing STALLED to refuse


class FromTheCommit(Base):
    """Arden's non-blocking (b) on ec69fdf: the deployed files were compared with the --at commit, but heartbeat (its
    UNITS, enabled, _setting, stalled_on) was imported from the working tree. Now every rule comes from the commit."""

    def commit_core(self, extra):
        (self.repo / "heartbeat.py").write_text((self.repo / "heartbeat.py").read_text() + extra)
        git(self.root, "commit", "-qam", "core change")
        self.reviewed = git(self.root, "rev-parse", "HEAD")
        shutil.copy(self.repo / "heartbeat.py", self.deployed / "heartbeat.py")

    def test_units_come_from_the_commit(self):
        self.commit_core('\nUNITS = {**UNITS, "ubuntu": UNITS["ubuntu"] + ("persistence-only-at-commit.service",)}\n')
        self.check()
        self.assertIn("persistence-only-at-commit.service", {c[-1] for c in self.systemctl.calls if c[1] == "show"})

    def test_a_different_heartbeat_already_imported_changes_nothing(self):
        # Whatever `heartbeat` (and the modules it imports) the process already holds, the verdict is the commit's.
        from unittest import mock
        import types
        before = [(i.name, i.ok, i.required) for i in self.check(stalled=True)]
        poisoned = types.ModuleType("heartbeat")
        poisoned.units_for = lambda user: ()
        poisoned.enabled = lambda home=None: False
        poisoned.stalled_on = lambda home=None: True
        poisoned._setting = lambda home, name, default: "nonsense"
        poisoned.systemctl_run = lambda argv: (1, "")
        with mock.patch.dict(sys.modules, {"heartbeat": poisoned, "coordination": types.ModuleType("coordination")}):
            after = [(i.name, i.ok, i.required) for i in self.check(stalled=True)]
        self.assertEqual(after, before)

    def test_a_dirty_tree_changes_nothing(self):
        before = [(i.name, i.ok, i.required) for i in self.check(stalled=True)]
        (self.repo / "heartbeat.py").write_text("raise SystemExit('the working tree was imported')\n")
        (self.repo / "coordination.py").write_text("raise SystemExit('the working tree was imported')\n")
        self.assertEqual([(i.name, i.ok, i.required) for i in self.check(stalled=True)], before)

    def test_imports_are_left_as_they_were(self):
        # Sentinels of its own, so the test doesn't depend on what an earlier test left imported.
        from unittest import mock
        import types
        held = {n: types.ModuleType(n) for n in ("heartbeat", "coordination", "claims", "hook")}
        with mock.patch.dict(sys.modules, held):
            path = list(sys.path)
            self.check(stalled=True)
            self.assertEqual(sys.path, path)
            self.assertEqual({n: sys.modules.get(n) for n in held}, held)

    def test_a_clean_process_is_left_clean(self):
        # Ember's M3 on 9c2b7f8: with none of the commit-served modules imported beforehand, none may be left behind,
        # or a later check (or `import heartbeat`) in the same process would get this commit's copy.
        from unittest import mock
        served = [n[:-3] for n in heartbeat_optin.DEPLOYED]
        with mock.patch.dict(sys.modules):
            for n in served:
                sys.modules.pop(n, None)
            path = list(sys.path)
            items = self.check(stalled=True)
            self.assertNotIn("core", self.failing(items))  # the commit's heartbeat really was loaded
            self.assertEqual([n for n in served if n in sys.modules], [])
            self.assertEqual(sys.path, path)

    def test_a_core_that_does_not_load_is_not_ready(self):
        self.commit_core("\ndef broken(:\n")
        items = self.check()
        self.assertIn("core", self.failing(items))
        self.assertIn("SyntaxError", self.item(items, "core").detail)

    def test_the_core_names_the_commit(self):
        self.assertIn(self.reviewed, self.item(self.check(), "core").detail)


class Cli(Base):
    def main(self, argv, out):
        self.settings()
        return heartbeat_optin.main(argv, home=self.home, user="ubuntu", repo=self.repo, systemctl=self.systemctl,
                                    path=str(self.bin), out=out.append)

    def test_exit_status_and_lines(self):
        out = []
        self.assertEqual(self.main(["check", "--at", self.reviewed], out), 0)
        self.assertEqual(out[-1], f"ready: PERSISTENCE_HEARTBEAT, as deployed from {self.reviewed}")
        self.env["PERSISTENCE_TASKCLAIMS"] = "true"
        out.clear()
        self.assertEqual(self.main(["check", "--stalled", "--at", self.reviewed], out), 1)
        self.assertTrue(out[-1].startswith("NOT READY for PERSISTENCE_HEARTBEAT_STALLED:"), out[-1])
        self.assertTrue(any(line.startswith("MISSING PERSISTENCE_TASKCLAIMS") for line in out), out)

    def test_at_flag(self):
        out = []
        self.assertEqual(self.main(["check", "--at", self.reviewed], out), 0)
        self.assertEqual(self.main(["check", "--stalled", "--at", "0" * 40], out), 1)
        for bad in (["check", "--at"], ["check", "--bogus"], ["check", "--at", "a", "--at", "b"]):
            self.assertEqual(self.main(bad, out), 2, bad)

    def test_at_is_required(self):
        # Arden (msg 1570857831513587753): the preflight certifies the reviewed commit; a HEAD fallback would certify
        # whatever is checked out. No --at: exit 2, says why, checks nothing.
        for argv in (["check"], ["check", "--stalled"]):
            out = []
            self.assertEqual(self.main(argv, out), 2, argv)
            self.assertTrue(out[0].startswith("--at <commit> is required"), out)
            self.assertFalse(any(line.startswith(("ok ", "ready", "NOT READY")) for line in out), out)
        with self.assertRaises(TypeError):
            heartbeat_optin.check(home=self.home, user="ubuntu", repo=self.repo, systemctl=self.systemctl,
                                  path=str(self.bin))


class Docs(unittest.TestCase):
    STEPS = (REPO.parents[1] / "docs" / "design" / "heartbeat-opt-in-claude.md").read_text()

    def test_no_deploy_names_another_self(self):
        # Arden's (d): their beat is live, so a deploy naming them replaces its running code. My steps deploy me only.
        for line in self.STEPS.splitlines():
            if "deploy-remote" in line:
                self.assertNotRegex(line.lower(), r"\b(arden|ember)\b", line)

    def test_way_back_is_concrete_and_reversed(self):
        # Arden's (e) and Ember's point 5: no "previous copies" nobody saved; a full undo in reverse order.
        self.assertNotIn("the previous copies", self.STEPS)
        self.assertIn("## The way back", self.STEPS)
        self.assertIn("redeploy", self.STEPS.lower())
        self.assertIn("persistence-store", self.STEPS)  # step 1 restarts the stores: not inert


class FakeHost:
    """deploy-remote.sh run against a fake host, with a deploy claim held by this session."""
    TEXT = (REPO / "deploy-remote.sh").read_text()

    def fake_host(self):
        # The script itself, run against fakes: ssh runs its remote command locally with /tmp, /home and /opt/persistence
        # moved into a scratch dir; sudo drops the root-only chown/chmod/systemctl; dotnet, scp and sleep are stand-ins.
        # The user is whoever runs the test, so `install -o` needs no root. Nothing outside the scratch dir is touched.
        # The caller holds a deploy claim in a scratch state dir (PERSISTENCE_STATE_DIR), as this session.
        import pwd, os, uuid
        sys.path.insert(0, str(REPO))
        import claims
        self.user = user = pwd.getpwuid(os.geteuid()).pw_name
        self.w = w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w)
        bins, self.tmp, self.dest = w / "bin", w / "tmp", w / "home" / user / ".claude" / "persistence"
        self.opt, self.state = w / "opt" / "persistence", w / "state"
        for d in (bins, self.tmp, self.dest, self.opt / "store", self.state):
            d.mkdir(parents=True)
        fakes = {
            "dotnet": 'while [ "$1" != "-o" ]; do shift; done; mkdir -p "$2"; echo x > "$2/persistence-store"',
            "scp": (f'shift; a=("$@"); d="${{a[-1]#*:}}"; d=${{d//\\/tmp\\//{self.tmp}/}}; unset "a[-1]"; '
                    f'cp "${{a[@]}}" "$d"'),
            "ssh": (f'c="$2"; c=${{c//\\/tmp\\//{self.tmp}/}}; c=${{c//\\/home\\//{w}/home/}}; '
                    f'c=${{c//\\/opt\\/persistence\\//{self.opt}/}}; exec bash -c "$c"'),
            "sudo": 'case "$1" in chown|chmod|systemctl) exit 0;; esac; exec "$@"',
            "systemctl": "exit 0", "sleep": "exit 0"}
        for name, body in fakes.items():
            (bins / name).write_text(f"#!/bin/bash\n{body}\n")
            (bins / name).chmod(0o755)
        py = re.search(r"^py=\(([^)]*)\)", self.TEXT, re.M).group(1).split()
        for f in py:
            shutil.copy(REPO / f, self.dest / f)
        self.me = str(uuid.uuid4())
        self.claims = claims.Claims(self.state, home=w)
        (w / "work").mkdir()
        self.claim = self.claims.claim("Deploy", self.me, str(w / "work"), subject="deploy")["id"]
        self.env = dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}", PERSISTENCE_STATE_DIR=str(self.state),
                        CLAUDE_CODE_SESSION_ID=self.me)

    def deploy(self, *args, check=True, env=None):
        args = args or ("--claim", self.claim, "fakehost", self.user)
        run = subprocess.run(["bash", str(REPO / "deploy-remote.sh"), *args], env=env or self.env, capture_output=True,
                             text=True)
        if check:
            self.assertEqual(run.returncode, 0, run.stderr)
        return run

    def untouched(self):
        self.assertEqual((self.dest / "heartbeat.py").read_text(), "# a local edit\n")
        self.assertFalse((self.opt / "store" / "persistence-store").exists())
        self.assertFalse((self.dest / "previous").exists())


class DeployRemote(FakeHost, unittest.TestCase):

    def test_parses(self):
        subprocess.run(["bash", "-n", str(REPO / "deploy-remote.sh")], check=True)

    def test_keeps_what_it_replaces(self):
        # Ember's point 6: it overwrote every file with no compare and no copy. Now a differing file is kept first.
        keep, install = self.TEXT.find("cmp -s"), self.TEXT.find("sudo install -o")
        self.assertGreater(keep, 0)
        self.assertLess(keep, install)
        self.assertIn("previous/", self.TEXT)

    def test_run_keeps_differing_copies(self):
        self.fake_host()
        (self.dest / "heartbeat.py").write_text("# a local edit\n")
        out = self.deploy().stdout
        kept = list((self.dest / "previous").glob("*/heartbeat.py"))
        self.assertEqual(len(kept), 1, out)
        self.assertEqual(kept[0].read_text(), "# a local edit\n")
        self.assertEqual([p.name for p in kept[0].parent.iterdir()], ["heartbeat.py"])  # identical files aren't kept
        self.assertIn("replaced heartbeat.py", out)
        self.assertEqual((self.dest / "heartbeat.py").read_bytes(), (REPO / "heartbeat.py").read_bytes())
        self.assertIn("no hook file changed", self.deploy().stdout)
        self.assertEqual(len(list((self.dest / "previous").iterdir())), 1)
        self.assertEqual(list(self.tmp.iterdir()), [], "each run's staging directory is removed")
        self.assertIn(f"claim {self.claim}, session {self.me}", (self.opt / ".deploy.holder").read_text())


class DeployGuard(FakeHost, unittest.TestCase):
    """The deploy guard, parts 1 and 2 (2026-11-14 double deploy, #18519): no run without the caller holding the live
    deploy claim, and no run while another holds the host's lock. A refused run changes nothing on the host."""

    def setUp(self):
        self.fake_host()
        (self.dest / "heartbeat.py").write_text("# a local edit\n")

    def test_no_claim_is_refused(self):
        run = self.deploy("fakehost", self.user, check=False)
        self.assertEqual(run.returncode, 2)
        self.assertIn("--claim", run.stderr)
        self.untouched()

    def test_a_claim_this_session_doesnt_hold_is_refused(self):
        import uuid
        run = self.deploy(check=False, env=dict(self.env, CLAUDE_CODE_SESSION_ID=str(uuid.uuid4())))
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertIn("not this session", run.stderr)
        self.untouched()

    def test_a_finished_deploy_claim_is_refused(self):
        # Arden's case: the first deployer finished; a second session's stale claim, or the closed one, can't deploy.
        self.claims.done(self.claim, self.me)
        run = self.deploy(check=False)
        self.assertEqual(run.returncode, 3)
        self.assertIn("done", run.stderr)
        self.untouched()

    def test_a_claim_with_another_subject_is_refused(self):
        other = self.claims.claim("Not a deploy", self.me, str(self.w / "work"))["id"]
        self.assertEqual(self.deploy("--claim", other, "fakehost", self.user, check=False).returncode, 3)
        self.untouched()

    def test_a_held_lock_is_refused_and_names_the_holder(self):
        import fcntl
        (self.opt / ".deploy.lock").touch()
        (self.opt / ".deploy.holder").write_text("claim 728d3911, session ea465313\n")
        with open(self.opt / ".deploy.lock") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run = self.deploy(check=False)
        self.assertEqual(run.returncode, 4, run.stderr)
        self.assertIn("claim 728d3911", run.stderr)
        self.untouched()
        self.assertEqual(list(self.tmp.iterdir()), [], "the refused run's staging directory is removed")
        self.deploy()  # released: the same claim deploys

if __name__ == "__main__":
    unittest.main()
