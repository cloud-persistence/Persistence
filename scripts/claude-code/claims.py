"""Task claims: the claims core (docs/design/task-claims.md, revision 4, sha256 8440c4dd…6b33).

A claim is the hand-off's task record (`hook.TASK_FIELDS`) extended by `CLAIM_FIELDS`: one JSON file per claim,
`state/tasks/<uuid>.json`, written under `state/tasks/.lock` by write-then-rename, with one history entry per write.
A claim exists only because a session (`ptask`) or the harness made it; nothing here reads room text, a reply, a
note or a transcript for tasks, and no free text of a claim reaches argv, the environment or a file name.

This module is the claims core: the record, its transitions and history, pause expiry, exact-id liveness,
pending-by-structure, the canonical and bound `cwd`, the `cwd-exec` launch, the one setting, `ptask`, and the spawn
claim run-due makes (`spawn_locked`). The locks and the fired-wake ledger are coordination.py's. Everything the
heartbeat does with claims (nudges, reports, the never-started recovery) is the heartbeat PR's.

`PERSISTENCE_TASKCLAIMS` (settings.json only, off by default) gates nothing in this module: claims are recorded
whether it is on or off, the spawn claim included (section 5; Ember 1570181248079626261 and Arden
1570189766400417927: off means no promise check and no heartbeat automation, not no ownership record). hook.py calls
this module for `ptask`, for `pwake` from a room session (`set_handoff`) and in `run_due`.

Standard library only, Linux for the descriptor binding."""
import ctypes
import json
import os
import re
import stat
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook  # noqa: E402  TASK_FIELDS stays the single list of hand-off keys

TASK_FIELDS = hook.TASK_FIELDS
CLAIM_FIELDS = ("id", "dispatch", "status", "cwd", "created", "updated", "pause_until", "nudged", "reported", "history", "outcome",
                "subject")
KEYS = set(TASK_FIELDS) | set(CLAIM_FIELDS)
# `subject` came after the first claims (the deploy guard, after the 2026-11-14 duplicate deploy claims): a file written before
# it has every other key and no subject, and reads as subject None. No other key may be missing.
LEGACY_KEYS = KEYS - {"subject"}
# A subject names the job a claim is for, so two claims can't be live for one job: at most one claim per subject is
# not done or abandoned. A short slug, never free text: the deploy checks it by equality ("deploy").
SUBJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ENTRY_FIELDS = ("at", "action", "from_status", "to_status", "from_owner", "to_owner", "by", "firing")
STATUSES = ("open", "claimed", "paused", "done", "abandoned")
TERMINAL = ("done", "abandoned")
DISPATCHES = ("handoff", "none")
FIXED_ACTORS = ("harness", "pause-expiry", "heartbeat")
CWD_EXEC = Path(__file__).resolve().with_name("cwd_exec.py")


class Refused(Exception):
    """A claims write that the table, the lock or the record refuses. The file is left byte for byte."""


def full_id(value):
    """A full UUID in its canonical text form, or None: never a prefix, never free text."""
    try:
        return str(uuid.UUID(str(value))) if value and str(uuid.UUID(str(value))) == str(value) else None
    except ValueError:
        return None


# Locks: the one global order, checked when each lock is taken, lives in coordination.py (shared with run-due and the
# heartbeat). Re-exported here so claims code and tests name them in one place.
from coordination import LOCK_ORDER, LOCK_FILES, LockOrderError, held_locks, lock  # noqa: E402,F401


# Liveness by exact id (section 4): the owner is running when some process's argv has --session-id or --resume as
# one entry and the owner's full id as the next, as FragmentStore.ProcessHoldsSession reads it. The cases are one
# table, tests/cases/process-holds-session.json.
def holds_session(argv, session):
    return any(argv[i] in ("--session-id", "--resume") and argv[i + 1] == session for i in range(len(argv) - 1))


def process_table(proc="/proc"):
    """Every readable process's argv, or None when liveness can't be told (no /proc)."""
    if not os.path.isdir(proc):
        return None
    table = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        table.append(raw.decode("utf-8", "replace").split("\0")[:-1] if raw.endswith(b"\0") else raw.decode("utf-8", "replace").split("\0"))
    return table


def running(session, table):
    """True, False, or None when the process table is unknown."""
    if table is None:
        return None
    return any(holds_session(argv, session) for argv in table)


def pending_for(record, items):
    """A wake is pending for a claim only by structure: its item's `session` is the owner's full id, or its `claim` is
    the claim's id. No substring search over reasons."""
    return any((record.get("owner") and item.get("session") == record["owner"]) or item.get("claim") == record["id"]
               for item in items)


def valid_record(record):
    """Valid by key set, not order. A record from before `subject` is valid without it (LEGACY_KEYS)."""
    return isinstance(record, dict) and set(record) in (KEYS, LEGACY_KEYS)


def valid_subject(subject):
    """None (no subject) or a slug: lower-case letters, digits and hyphens, at most 40, not starting with a hyphen."""
    if subject is not None and not (isinstance(subject, str) and SUBJECT.fullmatch(subject)):
        raise Refused(f"{subject!r} is not a subject: lower-case letters, digits and hyphens, at most 40")
    return subject


def enabled(home=None):
    """PERSISTENCE_TASKCLAIMS from this self's ~/.claude/settings.json env block only, never os.environ (hook.py's
    setting() prefers the environment, so a unit's Environment= could split it). On only when it is "1"."""
    try:
        env = json.loads((Path(home or Path.home()) / ".claude" / "settings.json").read_text(encoding="utf-8")).get("env") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return str(env.get("PERSISTENCE_TASKCLAIMS", "")).strip() == "1" if isinstance(env, dict) else False


# The cwd (section 1): stored as the canonical real path computed at creation; at each use resolved beneath the
# self's home as a descriptor with no symlink followed at any depth, checked with fstat, and launched from that
# descriptor through cwd-exec. Any failure refuses that use: "no valid cwd". Nothing falls back to another path.
RESOLVE_NO_SYMLINKS, RESOLVE_BENEATH = 0x04, 0x08
SYS_OPENAT2 = 437  # the same number on every architecture since Linux 5.6
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


try:
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.syscall.restype = ctypes.c_long
except OSError:
    _libc = None


def _openat2(dirfd, path):
    how = _OpenHow(DIR_FLAGS, 0, RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)
    fd = _libc.syscall(ctypes.c_long(SYS_OPENAT2), ctypes.c_int(dirfd), ctypes.c_char_p(os.fsencode(path)),
                       ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    return fd


def _have_openat2():
    if _libc is None or not sys.platform.startswith("linux"):
        return False
    try:
        root = os.open("/", DIR_FLAGS)
    except OSError:
        return False
    try:
        os.close(_openat2(root, "."))
        return True
    except OSError:
        return False
    finally:
        os.close(root)


HAVE_OPENAT2 = _have_openat2()


def _openat_walk(dirfd, rel):
    """The fallback: component by component, each fd the parent of the next, O_NOFOLLOW at every depth; `..`, `.` and
    empty components refused."""
    if rel == ".":
        return os.dup(dirfd)
    parent = os.dup(dirfd)
    try:
        for name in rel.split("/"):
            if name in ("", ".", ".."):
                raise OSError(f"component {name!r} refused")
            child = os.open(name, DIR_FLAGS | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        result, parent = parent, None
        return result
    finally:
        if parent is not None:
            os.close(parent)


def static_cwd_problem(path, home_real):
    """Why a cwd string fails "valid when" without touching the filesystem, or None."""
    if not isinstance(path, str) or not path:
        return "empty"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return "control characters"
    if not os.path.isabs(path):
        return "not absolute"
    if os.path.normpath(path) != path:
        return "not normalised"
    if path != home_real and not path.startswith(home_real.rstrip("/") + "/"):
        return "outside home"
    return None


def launch(fd, argv, **popen):
    """Start argv in the directory `fd` through cwd-exec: only that descriptor crosses (pass_fds, close_fds), and
    cwd-exec closes it before exec. The harness's copy is closed here, whatever happens."""
    try:
        return subprocess.Popen([sys.executable, "-I", str(CWD_EXEC), str(fd), *argv], pass_fds=(fd,), close_fds=True, **popen)
    finally:
        os.close(fd)


def _parse_time(value):
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    if moment.tzinfo is None:
        raise ValueError("a time needs Z or an offset")
    return moment.astimezone(timezone.utc)


def spell_start(record):
    """The history sequence number that began the current spell: the last transition into `claimed` from another
    status, or the last take (heartbeat.md's report key uses it)."""
    for i in range(len(record["history"]) - 1, -1, -1):
        h = record["history"][i]
        if h["to_status"] == "claimed" and (h["from_status"] != "claimed" or h["action"] == "take"):
            return i
    return None


_UNSET = object()


class Claims:
    def __init__(self, state=None, home=None, now=None, wakes_path=None):
        self.state = Path(state or hook.STATE)
        self.home = Path(home or Path.home())
        self.home_real = os.path.realpath(str(self.home))
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.dir = self.state / "tasks"
        self.wakes_path = Path(wakes_path) if wakes_path else self.state.parent / "wakes.json"

    def lock(self, name):
        return lock(self.state, name)

    def stamp(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec="seconds")

    # Files.
    def _path(self, claim_id):
        if not full_id(claim_id):
            raise Refused(f"{claim_id!r} is not a claim id")
        return self.dir / f"{claim_id}.json"

    def get(self, claim_id):
        """A claim, read without the lock and without writing (`ptask show`). Terminal claims stay where they are: no
        archiving in v1, retention unbounded (Ember 1570189950421565481, point 5)."""
        path = self._path(claim_id)
        if path.exists():
            return self._load(path)
        raise Refused(f"no claim {claim_id}")

    def _load(self, path):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Refused(f"claim file {path.name} can't be read: {e}")
        if not valid_record(record):
            raise Refused(f"claim file {path.name} doesn't have exactly the claim keys")
        record.setdefault("subject", None)  # a claim from before subjects; written back with subject null
        return record

    def all(self):
        """Every claim, terminal ones included, oldest first."""
        out = []
        for path in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else []:
            if full_id(path.stem):
                try:
                    out.append(self._load(path))
                except Refused:
                    continue
        return sorted(out, key=lambda r: r["created"] or "")

    def by_owner(self, session):
        """The non-terminal claim owned by this exact session id, if any (hand-off's task_file, by owner)."""
        return next((r for r in self.all() if r["owner"] == session and r["status"] not in TERMINAL), None)

    def _write(self, record):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record["id"])
        tmp = path.with_name(f".{record['id']}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(record, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _append(self, record, action, to_status, by, to_owner=_UNSET, firing=None, **extra):
        at = self.stamp()
        entry = {"at": at, "action": action, "from_status": record["status"] if action not in ("create",) and record["history"] else None,
                 "to_status": to_status, "from_owner": record["owner"],
                 "to_owner": record["owner"] if to_owner is _UNSET else to_owner, "by": by, "firing": firing, **extra}
        record["history"].append(entry)
        record["status"], record["owner"], record["updated"] = to_status, entry["to_owner"], at
        return entry

    @staticmethod
    def _actor(by, fixed=()):
        if by in fixed or full_id(by):
            return by
        raise Refused(f"{by!r} is not a session's full id")

    # Pause expiry: the one executor, run first in every claims write, under tasks/.lock.
    def settle_pauses(self, now=None):
        with self.lock("tasks"):
            return self._settle_locked()

    def _settle_locked(self):
        settled, now = 0, self.now()
        for record in self.all():
            if record["status"] != "paused" or not record["pause_until"]:
                continue
            try:
                until = _parse_time(record["pause_until"])
            except ValueError:
                continue
            if until > now:
                continue
            self._append(record, "expire", "claimed", "pause-expiry", pause_until=record["pause_until"])
            record["pause_until"] = record["nudged"] = record["reported"] = None
            self._write(record)
            settled += 1
        return settled

    @contextmanager
    def _locked(self, claim_id):
        with self.lock("tasks"):
            self._settle_locked()
            yield self._load(self._path(claim_id)) if self._path(claim_id).exists() else self._missing(claim_id)

    def _missing(self, claim_id):
        raise Refused(f"no claim {claim_id}")

    def _expect(self, record, action, *statuses):
        if record["status"] not in statuses:
            raise Refused(f"claim {record['id']} is {record['status']}; {action} needs {' or '.join(statuses)}")

    @staticmethod
    def _owner_only(record, by, verb):
        """Only the exact owner session may write a claimed or paused claim (Ember 1570189950421565481, point 6): not
        another session of this self, not an id sharing a prefix, not the harness. take is the one guarded transfer."""
        if by != record["owner"]:
            raise Refused(f"claim {record['id']} is held by {record['owner']}; only that session may {verb} it (take "
                          f"transfers a claim whose owner has stopped)")

    def _held_by(self, record):
        alive = running(record["owner"], process_table())
        state = "liveness unknown" if alive is None else ("which is running" if alive else "which is not running")
        return f"claim {record['id']} is {record['status']}, held by {record['owner']}, {state}"

    # Creation.
    def canonical_cwd(self, given, base=None):
        """The canonical real path of a supplied or default directory, validated, and the only value ever stored (Ember
        1570181249534795788, point 4). Harmless non-canonical input (`.`, `..`, a trailing or double slash, a symlink
        reaching an allowed directory) is resolved, not refused; a relative path resolves against `base`, the caller's
        directory. The string as given is never stored or used again; the descriptor-bound open at each use stays the
        security check."""
        if not isinstance(given, str) or not given:
            raise Refused("no valid cwd: empty")
        real = os.path.realpath(os.path.join(base or os.getcwd(), given))
        problem = static_cwd_problem(real, self.home_real)
        if problem:
            raise Refused(f"no valid cwd: {problem}")
        os.close(self.open_cwd(real))
        return real

    def live_for(self, subject):
        """The claims for `subject` that are not done or abandoned (paused counts: a pause expires back to claimed)."""
        return [r for r in self.all() if subject and r["subject"] == subject and r["status"] not in TERMINAL]

    def create(self, task, owner, cwd, by, dispatch="none", status="claimed", origin_session=None,
               room_channel=None, room_from=None, room_upto=None, base=None, subject=None):
        if dispatch not in DISPATCHES or status not in ("open", "claimed"):
            raise Refused(f"a claim is created open or claimed, dispatch handoff or none")
        if status == "claimed" and not full_id(owner):
            raise Refused(f"{owner!r} is not a session's full id")
        if status == "open" and owner is not None:
            raise Refused("an open claim has no owner")
        self._actor(by, ("harness",))
        valid_subject(subject)
        record = dict.fromkeys(TASK_FIELDS)
        record.update(task=str(task), origin_session=origin_session, room_channel=room_channel, room_from=room_from,
                      room_upto=room_upto, owner=None)
        record.update(id=str(uuid.uuid4()), dispatch=dispatch, status=None, cwd=self.canonical_cwd(cwd, base),
                      created=self.stamp(), updated=None, pause_until=None, nudged=None, reported=None, history=[], outcome=None,
                      subject=subject)
        with self.lock("tasks"):
            self._settle_locked()
            # One live claim per subject, checked under the lock so two creators can't both pass (the 2026-11-14 double
            # deploy: a second hand-off for the deploy was made while the first claim was live). The old claim must be
            # closed, not paused: a pause expires back to claimed, and its session can resume.
            for live in self.live_for(subject):
                how = (f"`ptask abandon {live['id']}` closes it, since it is open" if live["status"] == "open" else
                       f"its owner closes it with `ptask done` or `ptask abandon`, or, if the owner has stopped, "
                       f"`ptask take {live['id']}` then abandon")
                raise Refused(f"subject {subject!r} already has a live claim: {self._held_by(live)}. One claim per "
                              f"subject until it is done or abandoned; {how}")
            self._append(record, "claim" if status == "claimed" else "create", status, by, to_owner=owner if status == "claimed" else None)
            self._write(record)
        return record

    def claim(self, task, session, cwd, base=None, subject=None):
        """`ptask claim "<task>"`: always a new record, claimed, dispatch none, owned by the calling session."""
        if not full_id(session):
            raise Refused("ptask claim needs this session's full id (CLAUDE_CODE_SESSION_ID)")
        return self.create(task, session, cwd, by=session, origin_session=session, base=base, subject=subject)

    def holds(self, claim_id, session, subject):
        """`ptask holds <id> <subject>`, the deploy's check that its caller holds the live claim for the job: the claim
        is claimed (not open, paused or closed), owned by exactly this session, and has exactly this subject. Returns
        the record, or Refused saying which of the three failed. Reads only."""
        valid_subject(subject)
        if not full_id(session):
            raise Refused("no session id to check against (CLAUDE_CODE_SESSION_ID)")
        record = self.get(claim_id)
        if record["subject"] != subject:
            raise Refused(f"claim {claim_id} has subject {record['subject']!r}, not {subject!r}")
        if record["status"] != "claimed":
            raise Refused(f"claim {claim_id} is {record['status']}, not claimed")
        if record["owner"] != session:
            raise Refused(f"claim {claim_id} is held by {record['owner']}, not this session {session}")
        return record

    # Transitions (section 1). Anything else is refused, under the lock.
    def claim_open(self, claim_id, owner, by="harness", firing=None):
        """open/handoff -> claimed, by run-due at spawn only (section 2.4; Ember 1570189950421565481, point 7): there is
        no session syntax for claiming an existing open record, and open/none has no assignment policy in v1."""
        if by != "harness":
            raise Refused("an open hand-off is claimed only by run-due at spawn; ptask claim makes a new claim")
        if not full_id(owner):
            raise Refused(f"{owner!r} is not a session's full id")
        if not full_id(firing):
            raise Refused("the spawn claim names its firing id")
        with self._locked(claim_id) as record:
            if record["status"] != "open":
                raise Refused(self._held_by(record) if record["owner"] else f"claim {claim_id} is {record['status']}; claim needs open")
            if record["dispatch"] != "handoff":
                raise Refused(f"claim {claim_id} is open with dispatch none: unowned, and nothing assigns it in v1")
            self._append(record, "claim", "claimed", by, to_owner=owner, firing=firing)
            record["nudged"] = record["reported"] = None
            self._write(record)
            return record

    def spawn_locked(self, claim_id, owner, firing, item_cwd, bind=True):
        """run-due's spawn claim (section 2.4, section 4 "Locks"), called holding wakes.lock and tasks/.lock: open/handoff
        -> claimed with owner = the item's pre-minted session, `by: harness`, `firing` = the item's id. Reconciled by
        firing id: a claim already claimed by the harness for this firing and this owner gets no second entry. The
        record's cwd must equal the item's, and is bound to a descriptor before anything is written, so a cwd that
        fails gets no claim. Returns (record, fd or None, fresh); Refused leaves the file byte for byte."""
        if held_locks()[-1:] != ["tasks"] or "wakes" not in held_locks():
            raise LockOrderError("the spawn claim runs holding wakes.lock and tasks/.lock")
        if not full_id(owner) or not full_id(firing):
            raise Refused("the spawn claim needs the pre-minted session's full id and the firing id")
        self._settle_locked()
        path = self._path(claim_id)
        if not path.exists():
            raise Refused(f"no claim {claim_id}")
        record = self._load(path)
        if record["cwd"] != item_cwd:
            raise Refused(f"no valid cwd: the item's cwd differs from claim {claim_id}'s")
        if record["status"] == "claimed" and record["owner"] == owner and any(
                h["action"] == "claim" and h["by"] == "harness" and h["firing"] == firing for h in record["history"]):
            fresh = False
        elif record["status"] == "open" and record["dispatch"] == "handoff":
            fresh = True
        else:
            raise Refused(f"claim {claim_id} is {record['status']}" + (f" by {record['owner']}" if record["owner"] else "")
                          + f", not open for firing {firing}")
        fd = self.open_cwd(record["cwd"]) if bind else None
        if fresh:
            try:
                self._append(record, "claim", "claimed", "harness", to_owner=owner, firing=firing)
                record["nudged"] = record["reported"] = None
                self._write(record)
            except BaseException:
                if fd is not None:
                    os.close(fd)
                raise
        return record, fd, fresh

    # The heartbeat's marks (heartbeat.md, "Writes go through the claims module"): claims code, called in-process under
    # tasks/.lock, one history entry each, `by: heartbeat`, the same-status form touch uses. Added for the heartbeat PR;
    # no existing transition changes.
    def mark_nudged(self, claim_id, firing):
        """`nudged` set, with an `action: nudge` entry carrying the nudge's firing id. A claimed claim only; a second
        mark for the same firing in this spell writes nothing (the split-nudge reconcile may call it twice)."""
        if not full_id(firing):
            raise Refused("a nudge mark needs the nudge's firing id")
        with self._locked(claim_id) as record:
            self._expect(record, "nudge", "claimed")
            start = spell_start(record) or 0
            if any(h["action"] == "nudge" and h["firing"] == firing for h in record["history"][start:]):
                return record
            entry = self._append(record, "nudge", "claimed", "heartbeat", firing=firing)
            record["nudged"] = entry["at"]
            self._write(record)
            return record

    def mark_reported(self, claim_id, spell):
        """`reported` set, with an `action: report` entry, only while the claim is still claimed in the spell the
        report's key names. False (nothing written) when that spell has ended."""
        with self._locked(claim_id) as record:
            if record["status"] != "claimed" or spell_start(record) != spell:
                return False
            if record["reported"]:
                return True
            entry = self._append(record, "report", "claimed", "heartbeat")
            record["reported"] = entry["at"]
            self._write(record)
            return True

    def pause(self, claim_id, by, until=None, why=""):
        self._actor(by)
        until_text = None
        if until is not None:
            try:
                until_text = _parse_time(until).isoformat(timespec="seconds")
            except ValueError as e:
                raise Refused(f"--until: {e}")
        with self._locked(claim_id) as record:
            self._expect(record, "pause", "claimed")
            self._owner_only(record, by, "pause")
            self._append(record, "pause", "paused", by, why=str(why or ""))
            record["pause_until"] = until_text
            self._write(record)
            return record

    def resume(self, claim_id, by):
        self._actor(by)
        with self._locked(claim_id) as record:
            self._expect(record, "resume", "paused")
            self._owner_only(record, by, "resume")
            self._append(record, "resume", "claimed", by)
            record["pause_until"] = record["nudged"] = record["reported"] = None
            self._write(record)
            return record

    def touch(self, claim_id, by):
        """A history entry, not a transition: not a new spell, so nudged and reported stay (Arden)."""
        self._actor(by)
        with self._locked(claim_id) as record:
            self._expect(record, "touch", "claimed", "paused")
            self._owner_only(record, by, "touch")
            self._append(record, "touch", record["status"], by)
            self._write(record)
            return record

    def done(self, claim_id, by, outcome=""):
        return self._close(claim_id, by, "done", outcome, ("claimed", "paused"))

    def abandon(self, claim_id, by, outcome=""):
        return self._close(claim_id, by, "abandoned", outcome, ("open", "claimed", "paused"))

    def _close(self, claim_id, by, to, outcome, allowed):
        self._actor(by)
        with self._locked(claim_id) as record:
            self._expect(record, "done" if to == "done" else "abandon", *allowed)
            if record["status"] != "open":
                self._owner_only(record, by, "done" if to == "done" else "abandon")
            self._append(record, "done" if to == "done" else "abandon", to, by)
            record["outcome"], record["pause_until"] = str(outcome or ""), None
            self._write(record)
            return record

    def take(self, claim_id, taker, table=_UNSET, items=_UNSET, expected_owner=None, resolver=None):
        """claimed -> claimed with a new owner, only if the owner holds no process by exact id, no wake is pending for
        the owner or the claim id, and the taker names itself. `expected_owner` makes it a compare-and-swap: of two
        takers who saw the same owner, one wins. The cwd is kept, and bound to a descriptor first."""
        if not full_id(taker):
            raise Refused("a take names its taker: this session's full id (CLAUDE_CODE_SESSION_ID)")
        with self.lock("wakes"):
            if items is _UNSET:
                items = self._read_wakes()
            with self._locked(claim_id) as record:
                self._expect(record, "take", "claimed")
                if expected_owner is not None and record["owner"] != expected_owner:
                    raise Refused(self._held_by(record) + f"; it changed hands since you looked (was {expected_owner})")
                alive = running(record["owner"], process_table() if table is _UNSET else table)
                if alive is None:
                    raise Refused(f"claim {claim_id}: can't tell whether its owner {record['owner']} is running")
                if alive:
                    raise Refused(f"claim {claim_id} is held by {record['owner']}, which is running")
                if pending_for(record, items):
                    raise Refused(f"claim {claim_id}: a wake is pending for its owner {record['owner']} or for the claim")
                os.close(self.open_cwd(record["cwd"], resolver=resolver))
                self._append(record, "take", "claimed", taker, to_owner=taker)
                record["nudged"] = record["reported"] = None
                self._write(record)
                return record

    def _read_wakes(self):
        try:
            items = json.loads(self.wakes_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise Refused(f"the wake queue can't be read, so a pending wake can't be ruled out: {e}")
        return items if isinstance(items, list) else []

    # The cwd at use.
    def open_cwd(self, path, resolver=None):
        """The stored path, resolved beneath home as a verified directory descriptor (O_CLOEXEC), or Refused."""
        problem = static_cwd_problem(path, self.home_real)
        if problem:
            raise Refused(f"no valid cwd: {problem}")
        rel = os.path.relpath(path, self.home_real)
        use_openat2 = HAVE_OPENAT2 if resolver is None else resolver == "openat2"
        if use_openat2 and not HAVE_OPENAT2:
            raise Refused("no valid cwd: openat2 unavailable")
        try:
            home = os.open(self.home_real, DIR_FLAGS)
        except OSError as e:
            raise Refused(f"no valid cwd: home can't be opened ({e.strerror})")
        try:
            fd = _openat2(home, rel) if use_openat2 else _openat_walk(home, rel)
        except OSError as e:
            raise Refused(f"no valid cwd: {e.strerror or e}")
        finally:
            os.close(home)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            os.close(fd)
            raise Refused("no valid cwd: not a directory owned by this self")
        return fd


# ptask: the session's own command. `claim` and `take` are refused in a room session (section 2.2).
USAGE = """ptask claim [--cwd <dir>] [--subject <slug>] "<task>"
                                         a new claim, owned by this session (not from a room session: use pwake);
                                         a subject allows one live claim per job (deploy: --subject deploy)
ptask list                               this self's claims not yet closed
ptask show <id>                          one claim, whole
ptask pause <id> [--until <UTC time>] "<why>"
ptask resume <id> | touch <id>
ptask done <id> ["<outcome>"] | abandon <id> ["<outcome>"]
ptask take <id>                          take a claim whose owner isn't running and has no wake pending
ptask holds <id> <subject>               exit 0 only if this session holds claim <id>, claimed, with that subject
ptask closed <id>                        exit 0 only if claim <id> is in this self's claims and done or abandoned"""


def _describe(record, now):
    status = record["status"]
    if status == "paused" and record["pause_until"]:
        passed = _parse_time(record["pause_until"]) <= now
        status = f"paused (until {record['pause_until']}{', passed' if passed else ''})"
    owner = (record["owner"] or "")[:8] or "unowned"
    return f"{record['id']}  {status}  owner {owner}  updated {record['updated']}"


def cli(args, env=None, state=None, home=None, cwd=None, out=print, err=None, now=None):
    env = os.environ if env is None else env
    err = err or (lambda text: print(text, file=sys.stderr))
    c = Claims(state, home=home, now=now)
    session = env.get("CLAUDE_CODE_SESSION_ID", "")
    room = env.get("PERSISTENCE_ROOM_CHANNEL")
    if not args or args[0] in ("-h", "--help", "help"):
        out(USAGE)
        return 0 if args else 2
    verb, rest = args[0], list(args[1:])
    try:
        if verb in ("claim", "take") and room:
            raise Refused(f"a room session can't own a claim (its id is resumed by the bridge); use pwake, which "
                          f"starts a working session that the harness claims for")
        if verb == "claim":
            here = cwd or os.getcwd()
            where, subject = here, None
            while rest[:1] in (["--cwd"], ["--subject"]) and len(rest) > 1:
                if rest[0] == "--cwd":
                    where = rest[1]
                else:
                    subject = rest[1]
                rest = rest[2:]
            task = " ".join(rest).strip()
            if not task:
                raise Refused('ptask claim needs the task: ptask claim "<task>"')
            record = c.claim(task, session, where, base=here, subject=subject)
            out(f"claimed {record['id']} in {record['cwd']}" + (f", subject {subject}" if subject else ""))
        elif verb == "closed" and len(rest) == 1:
            record = c.get(rest[0])
            if record["status"] not in TERMINAL:
                raise Refused(f"claim {record['id']} is {record['status']}, not closed")
            out(f"{record['id']} is {record['status']}")
        elif verb == "holds" and len(rest) == 2:
            record = c.holds(rest[0], session, rest[1])
            out(f"this session holds {record['id']}, subject {record['subject']}")
        elif verb == "list":
            moment = c.now()
            lines = [_describe(r, moment) for r in c.all() if r["status"] not in TERMINAL]
            out("\n".join(lines) if lines else "(no claims open)")
        elif verb == "show" and len(rest) == 1:
            record = c.get(rest[0])
            out(_describe(record, c.now()))
            out(json.dumps(record, indent=1, ensure_ascii=False))
        elif verb == "pause" and rest:
            claim_id, rest = rest[0], rest[1:]
            until = None
            if rest[:1] == ["--until"] and len(rest) > 1:
                until, rest = rest[1], rest[2:]
            record = c.pause(claim_id, session, until=until, why=" ".join(rest))
            out(f"paused {record['id']}" + (f" until {record['pause_until']}" if record["pause_until"] else ""))
        elif verb in ("resume", "touch") and len(rest) == 1:
            record = getattr(c, verb)(rest[0], session)
            out(f"{verb}d {record['id']}: {record['status']}" if verb == "resume" else f"touched {record['id']}")
        elif verb in ("done", "abandon") and rest:
            record = getattr(c, verb)(rest[0], session, outcome=" ".join(rest[1:]))
            out(f"{record['status']} {record['id']}")
        elif verb == "take" and len(rest) == 1:
            seen = c.get(rest[0])
            record = c.take(rest[0], session, expected_owner=seen["owner"])
            out(f"took {record['id']} from {seen['owner']}")
        else:
            err(USAGE)
            return 2
    except Refused as e:
        err(f"ptask {verb}: {e}")
        return 1
    return 0


def main(argv=None):
    return cli(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
