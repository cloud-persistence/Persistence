"""The coordination foundation shared by claims and the heartbeat: the four locks in their one order, and the fired-wake
ledger's append and snapshot. Written to docs/design/heartbeat.md as merged at 2524fe7 ("Lock order", "The ledger of
fired wakes") and task-claims.md section 4 ("Locks"); built in the claims PR because run-due's spawn claim needs them
(Ember 1570181248079626261, Arden 1570189766400417927). Only what the spawn claim needs is here: the `enqueued` fact
and the ledger's append/snapshot protocol. The `started`/`finished`/`taken` facts, the derived states, retries,
deferrals and reports are the heartbeat PR's.

Standard library only; no import of hook.py or claims.py, so both can import this."""
try:
    import fcntl
except ImportError:  # Windows (John's PC runs hook.py): the order is still checked; no OS lock, as before
    fcntl = None
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

# Lock order (task-claims.md section 4 and heartbeat.md "Lock order", the same sentence): a path takes only the locks it
# needs, and whenever it holds more than one of state/heartbeat.lock, state/wakes.lock, state/tasks/.lock and
# state/fired.lock it takes them in that order, never waits for an earlier one while holding a later one, and takes no
# other lock while holding state/fired.lock; no path holds any of them while it launches a process or model or writes a
# report to the journal. The order is checked when each lock is taken, so a wrong path fails instead of deadlocking.
LOCK_ORDER = ("heartbeat", "wakes", "tasks", "fired")
LOCK_FILES = {"heartbeat": "heartbeat.lock", "wakes": "wakes.lock", "tasks": "tasks/.lock", "fired": "fired.lock"}
LOCK_OBSERVER = None  # tests: called with ("acquire" | "release", name) and ("write", what) for the writes they order
_held = threading.local()


class LockOrderError(RuntimeError):
    """A lock taken against the one global order."""


def held_locks():
    return list(getattr(_held, "names", ()))


def observe(event):
    if LOCK_OBSERVER:
        LOCK_OBSERVER(event)


class LockBusy(RuntimeError):
    """A lock asked for with a wait (0 = don't wait) that another holder kept past it."""


def _take(fd, name, wait):
    """flock, blocking when wait is None (every claims and run-due path), else trying until `wait` seconds pass: the
    heartbeat's phase 1 doesn't wait (a second beat exits at once) and its phase 3 waits 30 s (heartbeat.md)."""
    if not fcntl:
        return
    if wait is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    import time

    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockBusy(f"{name}.lock is held")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


@contextmanager
def lock(state, name, wait=None):
    names = held_locks()
    if "fired" in names:
        raise LockOrderError(f"{name}.lock taken while holding fired.lock, which is a leaf")
    if names and LOCK_ORDER.index(name) <= LOCK_ORDER.index(names[-1]):
        raise LockOrderError(f"{name}.lock taken while holding {names[-1]}.lock: the order is {' -> '.join(LOCK_ORDER)}")
    path = Path(state) / LOCK_FILES[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        _take(fd, name, wait)
        _held.names = names + [name]
        observe(("acquire", name))
        try:
            yield
        finally:
            _held.names = names
            observe(("release", name))
    finally:
        os.close(fd)  # closing the descriptor releases the flock


# The ledger (heartbeat.md, "The ledger of fired wakes"): state/fired.jsonl, one JSON object per line, each with
# `firing`, `fact`, `at`. Appended and read under state/fired.lock only.
LEDGER = "fired.jsonl"
# What an `enqueued` fact carries from its item: structure only, never the reason (its sha256 instead). The item's own
# `at` is carried as `item_at`, since every fact's `at` is the time it was written (heartbeat.md names it `item_at`
# too, since 1e4f42e).
ENQUEUED_FIELDS = ("session", "cwd", "item_at", "work", "by", "claim", "launch", "retry_of", "follows", "attempt",
                   "deferral", "reason_sha256")
# The stalled-work layer's two (heartbeat-core-interface.md): `hb`, a heartbeat item's correlation, and `set`, when the
# item was added (activity and progress read it). Carried only when the item has an hb or the layer is on, so with
# PERSISTENCE_HEARTBEAT_STALLED off every fact is byte for byte what it was (Ember's G2).
STALLED_FIELDS = ("hb", "set")


class LedgerError(RuntimeError):
    """An append that did not land whole (cut back, nothing half-written left), or a ledger line that isn't the tail and
    can't be parsed (real corruption: no ledger-based decision is made)."""


def enqueued_fact(item, at, stalled=False):
    """The `enqueued` fact for a wake item: its firing id and structure, no text. With `stalled` (the layer's switch, as
    run-due reads it) or an item that carries hb, STALLED_FIELDS follow."""
    fact = {"firing": item["id"], "fact": "enqueued", "at": at}
    for key in ENQUEUED_FIELDS + (STALLED_FIELDS if stalled or item.get("hb") else ()):
        if key == "item_at":
            fact[key] = item.get("at")
        elif key == "reason_sha256":
            fact[key] = hashlib.sha256(str(item.get("reason") or "").encode("utf-8")).hexdigest()
        else:
            fact[key] = item.get(key)
    return fact


def _repair_tail(fd, state):
    """A trailing line with no newline can only be a writer killed mid-append. Move it aside verbatim to
    fired.partial-<byte offset> and truncate to the last newline, with fsync. True when it repaired one."""
    size = os.fstat(fd).st_size
    if size == 0:
        return False
    data = os.pread(fd, size, 0)
    if data.endswith(b"\n"):
        return False
    cut = data.rfind(b"\n") + 1
    aside = Path(state) / f"fired.partial-{cut}"
    tmp = aside.with_name(aside.name + f".{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data[cut:])
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, aside)
    os.ftruncate(fd, cut)
    os.fsync(fd)
    return True


def _write(fd, data):
    """One write; a test fault hook replaces this to split it."""
    return os.write(fd, data)


def append(state, fact):
    """Append one fact: under fired.lock, one write on an O_APPEND descriptor, the whole line checked, fsync. A short
    write is cut back to the length before it and the append fails loudly."""
    if set(("firing", "fact", "at")) - set(fact):
        raise LedgerError("a fact needs firing, fact and at")
    data = (json.dumps(fact, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    with lock(state, "fired"):
        fd = os.open(str(Path(state) / LEDGER), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            _repair_tail(fd, state)
            before = os.fstat(fd).st_size
            written = _write(fd, data)
            if written != len(data):
                os.ftruncate(fd, before)
                os.fsync(fd)
                raise LedgerError(f"short write to the ledger ({written} of {len(data)} bytes), cut back")
            os.fsync(fd)
        finally:
            os.close(fd)


def snapshot(state):
    """(facts, complete) read whole under fired.lock. A partial tail is repaired and makes the snapshot incomplete
    ("incomplete snapshot, retry next beat"); an unparseable line that isn't the tail raises LedgerError."""
    path = Path(state) / LEDGER
    with lock(state, "fired"):
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError:
            return [], True
        try:
            repaired = _repair_tail(fd, state)
            data = os.pread(fd, os.fstat(fd).st_size, 0)
        finally:
            os.close(fd)
    facts, offset = [], 0
    for raw in data.split(b"\n")[:-1]:
        try:
            fact = json.loads(raw)
            if not isinstance(fact, dict):
                raise ValueError("not an object")
        except ValueError:
            err = LedgerError(f"ledger line at byte {offset} can't be read")
            err.offset = offset  # the heartbeat's report key names it
            raise err
        facts.append(fact)
        offset += len(raw) + 1
    return facts, not repaired


def enqueued_ids(state):
    """Every firing id with an `enqueued` fact."""
    facts, _ = snapshot(state)
    return {f.get("firing") for f in facts if f.get("fact") == "enqueued"}


# Where the ledger's coverage begins (ledger_since; Arden 1571129057112752299, after the 2026-11-14 false "never
# started" for 92acf9f9): the run line wrote no started/finished facts before it was deployed, so an `enqueued`-only
# firing from before then says nothing about whether it started. state/ledger-since holds one ISO UTC time, written
# once at deploy (deploy-remote.sh runs `seed-ledger-since` as each named user) and never rewritten. Its value is the
# ledger's earliest `started` fact, which is when the run line was first live; with none, the time of seeding, which is
# right for a self that has never launched anything ("empty-safe"). Missing or unreadable reads as None: no boundary,
# every firing judged as before.
LEDGER_SINCE = "ledger-since"


def _iso(value):
    from datetime import datetime
    try:
        moment = datetime.fromisoformat(value.strip()) if isinstance(value, str) else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo else None


def ledger_since(state):
    """The boundary as an aware datetime, or None (no file, or not one aware ISO time)."""
    try:
        return _iso((Path(state) / LEDGER_SINCE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def seed_ledger_since(state, now=None):
    """Write state/ledger-since once: (value, True) when written, (existing text, False) when it was already there.
    Created with O_EXCL, so two seeders can't both write and an existing file is never replaced."""
    from datetime import datetime, timezone
    state = Path(state)
    path = state / LEDGER_SINCE
    if path.exists():
        return path.read_text(encoding="utf-8").strip(), False
    state.mkdir(parents=True, exist_ok=True)
    facts, _ = snapshot(state)
    starts = [m for m in (_iso(f.get("at")) for f in facts if f.get("fact") == "started") if m is not None]
    moment = min(starts) if starts else (now or datetime.now(timezone.utc))
    value = moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip(), False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
        f.flush()
        os.fsync(f.fileno())
    return value, True


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["seed-ledger-since"]:
        print("usage: coordination.py seed-ledger-since   (state: $PERSISTENCE_STATE_DIR or ~/.claude/persistence/state)",
              file=sys.stderr)
        sys.exit(2)
    where = os.environ.get("PERSISTENCE_STATE_DIR") or str(Path.home() / ".claude" / "persistence" / "state")
    value, wrote = seed_ledger_since(where)
    print(f"ledger-since {'written' if wrote else 'already'} {value}")
