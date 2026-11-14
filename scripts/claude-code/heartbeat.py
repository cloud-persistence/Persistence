"""The heartbeat (docs/design/heartbeat.md, merged at 2524fe7, approved by Ember at 027382d): a script on a 10-minute
timer that compares facts and calls no model. It nudges, retries or defers only by adding a wake item; everything else
is a report to the journal.

Built in slices on arden/heartbeat-impl: the switch, the beat's three phases, reports (keys, lines, the outbox,
at-least-once delivery), checks A to D, the hourly cap and the usage-limit reset parser. The stalled-work layer
(docs/design/heartbeat-stalled-work.md; interface in docs/design/heartbeat-core-interface.md) adds R, Q, the working
mode's tick and one standing delivery fault, behind PERSISTENCE_HEARTBEAT_STALLED, and the `pheartbeat` commands.

It reads only JSON records it or the harness wrote, the process table and systemd: never the room, a reply, a note, a
transcript, a fragment, a wake's reason or a task's text. Standard library only."""
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordination  # noqa: E402

STATE = Path(os.environ.get("PERSISTENCE_STATE_DIR") or Path.home() / ".claude" / "persistence" / "state")
RUN_DUE_GRACE = 120  # hook.run_due's default grace for an item whose session may still have a live waiter
OVERDUE = 600  # B: still queued 10 min past `at` plus the grace
MARK_WAIT = 30  # phase 3 retakes heartbeat.lock blocking with this timeout
PER_HOUR = 3

# D: the fixed unit list, a literal per self, chosen by the effective user's name. Nothing here comes from a setting,
# an argument or systemctl's output; an unknown user checks nothing. Each self adds its own tuple when it opts in
# (Claude's is "ubuntu"); Ember's waits.
UNITS = {
    "arden": ("persistence-wakes@arden.service", "persistence-room@arden.service", "persistence-remote@arden.service",
              "persistence-sleep@arden.service", "persistence-room-coverage@arden.service",
              "persistence-store-arden.service", "persistence-backup-arden.service"),
    # Claude (Linux user ubuntu), opted in 2026-11-13. The last four are system units Claude runs on home: the store,
    # its backup, the embedder recall uses, and the read-only mirror of main that Arden pulls from.
    "ubuntu": ("persistence-wakes@ubuntu.service", "persistence-room@ubuntu.service",
               "persistence-remote@ubuntu.service", "persistence-sleep@ubuntu.service",
               "persistence-room-coverage@ubuntu.service", "persistence-daily@ubuntu.service",
               "persistence-store.service", "persistence-backup.service", "persistence-embedder.service",
               "persistence-mirror.service"),
}

# Reports: closed sets. Claim conditions are claims' (task-claims.md section 4, b0b8383); the rest are this design's.
CLAIM_CONDITIONS = {"stalled after nudge": "stalled", "stalled after nudge, touched since": "stalled",
                    "unowned": "unowned", "liveness unknown": "liveness-unknown", "start failed": "start-failed",
                    "no valid cwd": "no-valid-cwd"}
WAKE_CONDITIONS = {"overdue": "overdue", "never started": "never-started", "crashed": "crashed", "failed": "failed",
                   "usage limit, deferrals used up": "usage-deferrals-used-up", "no valid cwd": "no-valid-cwd"}
WAKE_KINDS = set(WAKE_CONDITIONS.values())
CLAIM_KINDS = set(CLAIM_CONDITIONS.values())


def enabled(home=None):
    """PERSISTENCE_HEARTBEAT from this self's ~/.claude/settings.json env block only, never os.environ (as claims'
    switch): on when "true" or "1" (switch_on)."""
    return switch_on(_setting(home, "PERSISTENCE_HEARTBEAT", ""))


def per_hour(home=None):
    try:
        return max(0, int(_setting(home, "PERSISTENCE_HEARTBEAT_PER_HOUR", str(PER_HOUR))))
    except ValueError:
        return PER_HOUR


def _setting(home, name, default):
    try:
        env = json.loads((Path(home or Path.home()) / ".claude" / "settings.json").read_text(encoding="utf-8")).get("env") or {}
    except (OSError, ValueError, AttributeError):
        return default
    value = env.get(name) if isinstance(env, dict) else None
    return default if value is None else str(value)


def stalled_on(home=None):
    """PERSISTENCE_HEARTBEAT_STALLED (heartbeat-core-interface.md): the stalled-work layer. Off, a beat is B, C, A, D
    only, as before it existed. Read like PERSISTENCE_HEARTBEAT: the settings file only, switch_on's rule.

    The one predicate for this switch: hook.stalled_layer_on (run-due) asks this function and keeps no copy, so the beat
    and run-due can't disagree on a value (Claude's point on #28)."""
    return switch_on(_setting(home, "PERSISTENCE_HEARTBEAT_STALLED", ""))


def switch_on(value):
    """The on/off rule for the heartbeat's switches, as _setting returns them (a str, JSON values through str()): on
    only for "true" or "1", trimmed, any case, so JSON true and 1 are on too. Everything else is off, the string
    "false" above all: "false", "0", "", "yes", JSON false, 0 and null, and a missing setting or settings file."""
    return isinstance(value, str) and value.strip().lower() in ("true", "1")


def source_named(home=None):
    """Whether 'heartbeat' is named in PERSISTENCE_OBSERVE_SOURCES (the settings file only). The R and Q blocks and the
    fault reach a session only through that source, so STALLED without it is refused (Ember's G5): source_off."""
    names = _setting(home, "PERSISTENCE_OBSERVE_SOURCES", "")
    return "heartbeat" in (n.strip() for n in names.split(","))


def settings_path(home=None):
    return Path(home or Path.home()) / ".claude" / "settings.json"


QUIET_AFTER = 90  # H, minutes
TICK_GAP = 30  # minutes, and the floor
BACKOFF_MAX = timedelta(hours=12)


def quiet_after(home=None):
    try:
        minutes = int(_setting(home, "PERSISTENCE_HEARTBEAT_QUIET_AFTER", str(QUIET_AFTER)))
    except ValueError:
        minutes = QUIET_AFTER
    return timedelta(minutes=minutes if minutes >= 1 else QUIET_AFTER)


def tick_gap(home=None):
    """(gap, raised): a gap under 30 minutes is raised to 30, and `raised` says so (one journal line per beat)."""
    try:
        minutes = int(_setting(home, "PERSISTENCE_HEARTBEAT_TICK_GAP", str(TICK_GAP)))
    except ValueError:
        return timedelta(minutes=TICK_GAP), False
    return timedelta(minutes=max(minutes, TICK_GAP)), minutes < TICK_GAP


def units_for(user):
    return UNITS.get(user, ())


# Keys and lines. A key is ids and a closed kind slug only; a line is the content plus " key=<key>" (sink framing).
def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError(f"{value!r} is not a full id")
    return value


def hhmm(moment):
    return moment.astimezone(timezone.utc).strftime("%H:%MZ")


def wake_key(firing, kind):
    if kind not in WAKE_KINDS:
        raise ValueError(f"{kind!r} is not a wake report kind")
    return f"wake:{_uuid(firing)}:{kind}"


def claim_key(claim, spell, kind):
    if kind not in CLAIM_KINDS or not isinstance(spell, int) or spell < 0:
        raise ValueError(f"bad claim key parts {kind!r}, {spell!r}")
    return f"claim:{_uuid(claim)}:{spell}:{kind}"


UNIT_NAME = re.compile(r"^[a-z0-9-]+(@[a-z0-9-]+)?\.service$")
INVOCATION = re.compile(r"^[0-9a-f]*$")


def unit_key(unit, invocation):
    if not UNIT_NAME.match(unit) or not INVOCATION.match(invocation):
        raise ValueError(f"bad unit key parts {unit!r}, {invocation!r}")
    return f"unit:{unit}:{invocation}:failed"


def cap_key(window_start):
    return f"cap:{window_start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"


def ledger_key(offset):
    return f"ledger:{int(offset)}:unreadable"


def claim_line(claim, condition, owner, at):
    if condition not in CLAIM_CONDITIONS:
        raise ValueError(f"{condition!r} is not a claims report condition")
    return f"heartbeat claim {_uuid(claim)}: {condition}, owner {_uuid(owner)[:8] if owner else 'unowned'}, at {hhmm(at)}"


def wake_line(firing, condition, owner, at):
    if condition not in WAKE_CONDITIONS:
        raise ValueError(f"{condition!r} is not a wake report condition")
    return f"heartbeat wake {_uuid(firing)}: {condition}, owner {_uuid(owner)[:8] if owner else 'unowned'}, at {hhmm(at)}"


# Usage limit (heartbeat.md "Usage limit"): keyed on is_error and the api error, never on subtype. Claude Code's output
# has no structured reset field (checked again 2026-11-13 on two real usage-limit outputs: api_error,
# api_error_status 429, and `result` text only). So the reset is read with one anchored pattern from `result`, nothing
# else in `result` is used, and what comes back is structured: a time within 24 h, or None for unknown. Unknown is
# never turned into an invented reset (Ember, 1570857894172168193): it is a visible hold that is rechecked every beat
# and tries again after UNKNOWN_RECHECK, and C's and A's deferrals say so in the journal.
RESET = re.compile(r"resets (1[0-2]|[1-9])(?::([0-5][0-9]))?(am|pm) \(([A-Za-z]+(?:/[A-Za-z_+-]+){0,2})\)")
UNKNOWN_RECHECK = timedelta(hours=1)
# How long an ack intent whose owner is alive counts as in flight: the ack's lock wait (observed.HB_ACK_WAIT, 5 s)
# plus a margin. Past it the owner has given up on the lock, so the intent is abandoned whatever its pid says.
ACK_INTENT_FRESH = timedelta(seconds=5 + 25)


def usage_limited(output):
    """The real shape (Claude's wake-b88e7b01): `api_error` is "usage_limit_reached" and `api_error_status` 429. Either
    alone is enough; there is no `error` or `api_error_type` field to read."""
    if not isinstance(output, dict) or output.get("is_error") is not True:
        return False
    return output.get("api_error_status") in (429, "429") or output.get("api_error") == "usage_limit_reached"


def reset_time(result, finished):
    """The next occurrence of the reset's local time after `finished`, within 24 h; None when the reset is unknown
    (missing, malformed, an unknown zone, or not within 24 h)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    m = RESET.search(result if isinstance(result, str) else "")
    if not m:
        return None
    try:
        zone = ZoneInfo(m.group(4))
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    local = finished.astimezone(zone)
    for days in (0, 1):
        day = (local + timedelta(days=days)).date()
        candidate = datetime(day.year, day.month, day.day, hour, int(m.group(2) or 0), tzinfo=zone)
        if finished < candidate <= finished + timedelta(hours=24):
            return candidate.astimezone(timezone.utc)
    return None


# Reading what the heartbeat compares. Only a missing file is empty (Ember's B1, B2): a malformed or unreadable one
# would read as "nothing pending" or "nothing reported" and the beat would act on that. Instead the beat decides
# nothing, leaves the file exactly as it is, and says so on stderr (the journal) with a non-zero exit.
class QueueUnreadable(RuntimeError):
    """wakes.json exists but can't be read as a list."""


class StateUnreadable(RuntimeError):
    """heartbeat.json exists but can't be read as the heartbeat's own state."""


def read_queue(state):
    """wakes.json, read plainly (no lock): B only reads it. Missing is empty; malformed or unreadable raises
    QueueUnreadable. The message names the file and the error's kind, never its content."""
    path = Path(state).parent / "wakes.json"
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise QueueUnreadable(f"{path} can't be read ({type(e).__name__})") from None
    if not isinstance(items, list):
        raise QueueUnreadable(f"{path} isn't a list")
    return [w for w in items if isinstance(w, dict)]


def systemctl_run(argv):
    import subprocess

    r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    return r.returncode, r.stdout


class Beat:
    """One beat's phase 1, holding heartbeat.lock (taken without waiting). Decisions go into the outbox; `take_cap`
    counts heartbeat wakes against the hourly cap. Leaving the block writes heartbeat.json and releases the lock."""

    def __init__(self, state, home, now, sink, systemctl, user):
        self.state, self.home, self.clock, self.sink, self.systemctl, self.user = Path(state), home, now, sink, systemctl, user
        self.now = now()
        self._lock = None
        self.stalled = stalled_on(home)
        self.notes = []  # journal lines that aren't report keys (deferrals, adoption, a malformed mode file)
        self.ledger_ok = False

    def __enter__(self):
        self._lock = coordination.lock(self.state, "heartbeat", wait=0)
        self._lock.__enter__()
        try:
            self.data = load(self.state)
        except BaseException:
            self._lock.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc):
        try:
            if exc[0] is None:
                save(self.state, self.data)
        finally:
            self._lock.__exit__(*exc)
        return False

    def report(self, key, line):
        """Into the outbox, unless the key is already there or already marked. The line is fixed now, at decision."""
        if key in self.data["reported"] or key in self.data["outbox"]:
            return False
        self.data["outbox"][key] = f"{line} key={key}"
        return True

    def _cap_window(self):
        """(window start, count) as the cap rule reads them now; a window an hour old or more counts as a new one."""
        cap = self.data["cap"]
        start = _time(cap.get("window")) if cap else None
        if cap and (start is None or not _count(cap.get("count"))):
            raise StateUnreadable("heartbeat.json's cap can't be read")  # never a silent reset to 0 (Ember's B2)
        if start is None or self.now - start >= timedelta(hours=1):
            return self.now.replace(second=0, microsecond=0), 0
        return start, cap["count"]

    def cap_has_room(self):
        """The cap's rule, read-only: True when take_cap would succeed now. Writes and reports nothing (stalled-work
        section 3, "deferral is not a fault"): Q and the tick ask this first and skip quietly when it is full."""
        return self._cap_window()[1] < per_hour(self.home)

    def take_cap(self, required=True):
        """One heartbeat wake against the hourly cap; False (and one `cap: reached` report per window) when full. With
        the stalled layer on, a refused required wake (B, C, A, D or R) also updates the standing delivery fault, once
        per window. An optional one (Q, tick) only gets here after cap_has_room(), under the same lock."""
        start, count = self._cap_window()
        if count == 0 and self.data["cap"].get("window") != start.isoformat():
            self.data["cap"].update(window=start.isoformat(), count=0)  # a new window is written even when refused (main)
        if count < per_hour(self.home):
            self.data["cap"].update(window=start.isoformat(), count=count + 1)
            return True
        if not required:
            return False
        if self.report(cap_key(start), f"heartbeat cap: reached, at {hhmm(self.now)}") and self.stalled:
            self.raise_fault("cap", cap_key(start))
        return False

    def check_overdue(self):
        """B: a wake still queued 10 min past its `at` plus run-due's grace (none for a room's work item)."""
        for w in read_queue(self.state):
            if not isinstance(w.get("at"), (int, float)) or not w.get("id"):
                continue
            late = w["at"] + (0 if w.get("work") else RUN_DUE_GRACE) + OVERDUE
            if self.now.timestamp() > late:
                try:
                    owner = w.get("session") if w.get("session") else None
                    self.report(wake_key(w["id"], "overdue"), wake_line(w["id"], "overdue", owner, self.now))
                except ValueError:
                    continue  # an item whose id or session isn't a full id: nothing structural to name it by

    def check_ledger(self):
        """The ledger, read whole under fired.lock. A partial tail (repaired by the read) means no ledger-based decision
        this beat; a bad line that isn't the tail is reported once and nothing is decided."""
        try:
            facts, complete = coordination.snapshot(self.state)
        except coordination.LedgerError as e:
            offset = getattr(e, "offset", 0)
            self.report(ledger_key(offset), f"heartbeat ledger: unreadable, at {hhmm(self.now)}")
            return
        if complete:
            self.ledger_decisions(facts)

    def outputs(self, facts):
        """Each finished firing's output, parsed, by its plain file name in state/; None when missing or unreadable."""
        out = {}
        for f in facts:
            name = f.get("output") if f.get("fact") == "finished" else None
            if isinstance(name, str) and name and "/" not in name and name not in out:
                try:
                    out[name] = json.loads((self.state / name).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    out[name] = None
        return out

    def ledger_decisions(self, facts):
        """Rows A and C from a complete snapshot. Recovery decisions (C, A's hand-off retry and nudge deferrals) run
        before A's report decisions, so a usage-refused nudge is deferred, not reported as a stall."""
        import claims

        self.facts = facts
        self.table = claims.process_table()
        self.outs = self.outputs(facts)
        self.states = firing_states(facts, self.now, self.table, wrapper_alive, self.data["crashed_seen"], self.outs,
                                    coordination.ledger_since(self.state))
        self.enq = {f["firing"]: f for f in facts if f.get("fact") == "enqueued" and isinstance(f.get("firing"), str)}
        self.claims_on = claims.enabled(self.home)
        self.ledger_ok = True
        self.check_fired()
        if self.claims_on:
            self.check_claims()

    # Shared by C and A: the recovery chain of an origin firing.
    def chain(self, origin):
        """Every attempt for an origin, queued or fired, once each: dicts with firing, attempt, deferral, follows. In
        the window where run-due wrote an attempt's enqueued fact and died before rewriting the queue, its id is in
        both; the fact wins (Ember's B3), so nothing is counted twice."""
        out = [f for f in self.enq.values() if f.get("retry_of") == origin]
        fired = {a["firing"] for a in out}
        out += [dict(w, firing=w["id"]) for w in read_queue(self.state)
                if w.get("retry_of") == origin and w.get("id") not in fired]
        return out

    def answered(self, firing):
        """An attempt is handled exactly when some item follows it or its report key exists."""
        if any(a.get("follows") == firing for chain in [self.enq.values(), read_queue(self.state)] for a in chain):
            return True
        return any(k.startswith(f"wake:{firing}:") for k in list(self.data["reported"]) + list(self.data["outbox"]))

    def busy(self, origin):
        """An attempt for this origin is queued, or a fired one is running."""
        if any(w.get("id") == origin or w.get("retry_of") == origin for w in read_queue(self.state)):
            return True
        return any(self.states.get(a.get("firing")) in ("running", None) and a.get("firing") in self.enq
                   for a in self.chain(origin))

    def finished_output(self, firing):
        f = next((x for x in self.facts if x.get("firing") == firing and x.get("fact") == "finished"), None)
        return (self.outs.get(f.get("output")) if f else None), (_time(f.get("at")) if f else None)

    def recover(self, firing, e, claim=None, cwd=None, nudge=False):
        """C's rule for one unanswered failed attempt: a usage limit is deferred (up to 3 per origin); anything else
        gets the origin's one ordinary retry, else is reported. A nudge has no ordinary retry. Returns the report
        condition when it reports, else None."""
        import claims

        origin = e.get("retry_of") or firing
        out, finished = self.finished_output(firing)
        chain = self.chain(origin)
        if usage_limited(out):
            used = sum(1 for a in chain if a.get("attempt") == "deferral")
            if used >= 3:
                return "usage limit, deferrals used up"
            end = finished or self.now
            reset = reset_time(out.get("result"), end)
            at = reset + timedelta(minutes=5) if reset else end + UNKNOWN_RECHECK
            if reset is None:
                self.note(f"heartbeat: usage limit on wake {firing}, reset unknown; deferral {used + 1} tries at {hhmm(at)}")
            self.add(origin, firing, e, "usage-limit", "deferral", used + 1, at, claim, cwd)
            return None
        if nudge or any(a.get("attempt") == "retry" for a in chain):
            return {"never-started": "never started"}.get(self.states[firing], self.states[firing])
        self.add(origin, firing, e, self.states[firing], "retry", None, self.now, claim, cwd)
        return None

    def add(self, origin, follows, e, kind, attempt, deferral, at, claim, cwd):
        """One recovery item through add_heartbeat_wake, against the hourly cap; the same session, kept structurally."""
        import claims
        import hook

        session = e.get("session")
        cwd = cwd if cwd is not None else e.get("cwd")
        if claims.static_cwd_problem(cwd, os.path.realpath(str(self.home or Path.home()))):
            self.report_wake(follows, "no valid cwd", session)
            return
        if not self.take_cap():
            return
        launch = "resume" if transcript_exists(self.home, session) else "session-id"
        try:
            hook.add_heartbeat_wake(id=str(uuid.uuid4()), kind=kind, session=session, launch=launch, claim=claim,
                                    retry_of=origin, follows=follows, attempt=attempt, deferral=deferral,
                                    at=at.timestamp(), cwd=cwd)
        except ValueError as e:
            print(f"heartbeat: no recovery for firing {follows}: {e}", file=sys.stderr)

    def report_wake(self, firing, condition, owner):
        try:
            self.report(wake_key(firing, WAKE_CONDITIONS[condition]), wake_line(firing, condition, owner, self.now))
        except ValueError:
            pass

    def check_fired(self):
        """C: a fired wake that didn't finish well. With claims on, a firing whose enqueued fact has a claim is A's.
        A `work` firing A doesn't own is reported, never retried; with claims off that is every work firing, and the
        claim is never read or written."""
        import claims

        for firing, e in self.enq.items():
            state = self.states.get(firing)
            if state not in ("never-started", "crashed", "failed") or self.answered(firing):
                continue
            if e.get("hb"):
                continue  # a Q, R or tick firing: never retried or reported; it folds into delivery_fault (3.3)
            if self.claims_on and e.get("claim"):
                continue
            if e.get("work"):
                self.report_wake(firing, {"never-started": "never started"}.get(state, state), e.get("session"))
                continue
            origin = e.get("retry_of") or firing
            if claims.running(e.get("session"), self.table) is not False or self.busy(origin):
                continue
            condition = self.recover(firing, e)
            if condition:
                self.report_wake(firing, condition, e.get("session"))

    # A: claims section 4, row by row.
    def report_claim(self, record, spell, condition):
        key = claim_key(record["id"], spell, CLAIM_CONDITIONS[condition])
        self.report(key, claim_line(record["id"], condition, record["owner"], self.now))

    def check_claims(self):
        import claims

        c = claims.Claims(self.state, home=self.home, now=lambda: self.now)
        c.settle_pauses()
        items = read_queue(self.state)
        for record in c.all():
            spell = claims.spell_start(record)
            if record["status"] == "open":
                self.open_row(c, record, spell or 0)
            elif record["status"] == "claimed":
                self.claimed_row(c, record, spell or 0, items)

    def spawn_firing(self, record):
        """The firing that spawn-claimed this spell (by harness), if that's how the spell began."""
        start = claims_spell_entry(record)
        return start["firing"] if start and start["by"] == "harness" and start["action"] == "claim" else None

    def open_row(self, c, record, spell):
        if record["dispatch"] == "none":
            self.report_claim(record, spell, "unowned")
            return
        # open/handoff: its firing failed before the spawn claim.
        firing = next((f for f, e in self.enq.items() if e.get("claim") == record["id"] and not e.get("retry_of")), None)
        if not firing or self.states.get(firing) != "never-started":
            return
        latest = last_attempt(self.chain(firing))
        if latest is None and not self.answered(firing):
            if self.take_cap():
                import hook

                e = self.enq[firing]
                try:
                    hook.add_heartbeat_wake(id=str(uuid.uuid4()), kind="never-started", session=e["session"],
                                            launch="session-id", claim=record["id"], retry_of=firing, follows=firing,
                                            attempt="retry", at=self.now.timestamp(), cwd=record["cwd"])
                except ValueError as err:
                    print(f"heartbeat: no retry for claim {record['id']}: {err}", file=sys.stderr)
            return
        if latest is not None and self.states.get(latest.get("firing")) in ("never-started", "crashed", "failed"):
            self.report_claim(record, spell, "start failed")

    def claimed_row(self, c, record, spell, items):
        import claims
        import hook

        owner = record["owner"]
        f0 = self.spawn_firing(record)
        if f0 and any(k.startswith(f"wake:{f0}:") for k in self.data["reported"]):
            return  # C reported it while claims were off: no A action until a session's own transition (not retroactive)
        if self.table is None:
            self.report_claim(record, spell, "liveness unknown")
            return
        if claims.running(owner, self.table):
            return
        if f0 and self.enq.get(f0, {}).get("retry_of") and not transcript_exists(self.home, owner):
            self.spent_spawn(record, spell, f0)  # Q4: the spawn was itself the hand-off's one retry
            return
        nudge = nudge_entry(record)
        # Reconcile both split-nudge halves by firing id (heartbeat.md, "Split writes in a nudge").
        if nudge is None:
            half = self.unmarked_nudge(record)
            if half:
                with coordination.lock(self.state, "wakes"):
                    c.mark_nudged(record["id"], half)
                return
        elif nudge["firing"] not in self.enq and not any(w.get("id") == nudge["firing"] for w in items) \
                and not any(f.get("firing") == nudge["firing"] and f.get("fact") == "taken" for f in self.facts):
            self.nudge(c, record, nudge["firing"], readd=True)
            return
        if claims.pending_for(record, items):
            return
        if nudge is None:
            self.nudge(c, record, str(uuid.uuid4()))
            return
        # The nudge fired. A usage refusal of it is deferred (recovery before the report decision).
        f = nudge["firing"]
        if self.states.get(f) in ("never-started", "crashed", "failed") and not self.answered(f):
            out, _ = self.finished_output(f)
            if usage_limited(out):
                if self.recover(f, self.enq.get(f, {"session": owner}), claim=record["id"], cwd=record["cwd"], nudge=True) is None:
                    return
        if record["reported"] or _time(record["nudged"]) is None or self.now - _time(record["nudged"]) < BEAT:
            return
        if self.states.get(f) in (None, "running") and f in self.enq:
            return
        if f0 and not transcript_exists(self.home, owner):
            self.report_claim(record, spell, "start failed")
            return
        touched = _time(record["updated"]) and _time(record["updated"]) > _time(record["nudged"])
        self.report_claim(record, spell, "stalled after nudge, touched since" if touched else "stalled after nudge")

    def spent_spawn(self, record, spell, f0):
        """Q4 (Ember, 1570222344146780254): one recovery total per logical spawn attempt per spell. This spell's spawn
        claim was made by A's retry of a hand-off that never started, so that retry was the one recovery: an
        owner-never-started nudge would be a second. The chain's last attempt, once it has failed, is deferred if it
        hit the usage limit (deferrals keep their own budget), else reported once as `start failed`."""
        last = last_attempt(self.chain(self.enq[f0]["retry_of"]))
        firing = last and last.get("firing")
        if firing not in self.enq or self.states.get(firing) not in ("never-started", "crashed", "failed"):
            return  # still queued, young or running
        if self.answered(firing):
            return
        out, _ = self.finished_output(firing)
        if usage_limited(out) and self.recover(firing, self.enq[firing], claim=record["id"], cwd=record["cwd"],
                                               nudge=True) is None:
            return
        self.report_claim(record, spell, "start failed")

    def unmarked_nudge(self, record):
        """A heartbeat nudge item for this claim that's queued, enqueued or taken in this spell, with no mark yet."""
        import hook

        start = _time(claims_spell_entry(record)["at"]) if claims_spell_entry(record) else None
        for w in read_queue(self.state):
            if w.get("by") == "heartbeat" and w.get("claim") == record["id"] and not w.get("retry_of"):
                return w["id"]
        for f in self.facts:
            ours = f.get("fact") == "taken" or (f.get("fact") == "enqueued" and f.get("by") == "heartbeat"
                                                 and not f.get("retry_of"))
            if f.get("claim") == record["id"] and ours:
                if start is None or (_time(f.get("at")) or start) >= start:
                    return f["firing"]
        return None

    def nudge(self, c, record, firing, readd=False):
        """Nudge once: the item first (minted firing id), then the mark, under wakes.lock -> tasks/.lock. A claim with
        no valid cwd is reported instead. A re-add (the mark without its item) writes only the item, same id."""
        import claims
        import hook

        spell = claims.spell_start(record) or 0
        if claims.static_cwd_problem(record["cwd"], c.home_real):
            self.report_claim(record, spell, "no valid cwd")
            return
        if not readd and not self.take_cap():
            return
        f0 = self.spawn_firing(record)
        launch = "resume" if transcript_exists(self.home, record["owner"]) else "session-id"
        with coordination.lock(self.state, "wakes"):
            try:
                hook.add_heartbeat_wake(id=firing, kind="nudge", session=record["owner"], launch=launch,
                                        claim=record["id"], at=self.now.timestamp(), cwd=record["cwd"])
            except ValueError as e:
                print(f"heartbeat: no nudge for claim {record['id']}: {e}", file=sys.stderr)
                return
            if not readd:
                c.mark_nudged(record["id"], firing)

    def check_units(self):
        """D: `systemctl is-failed` and `show -p InvocationID` on the fixed list only; never start, restart, stop, reset."""
        for unit in units_for(self.user):
            code, out = self.systemctl(["systemctl", "is-failed", "--", unit])
            if out.strip() != "failed":
                continue
            _, shown = self.systemctl(["systemctl", "show", "-p", "InvocationID", "--", unit])
            invocation = shown.strip().partition("=")[2].strip()
            if not INVOCATION.match(invocation):
                invocation = ""
            self.report(unit_key(unit, invocation), f"heartbeat unit {unit}: failed, invocation {invocation}, at {hhmm(self.now)}")

    # ---- The stalled-work layer (docs/design/heartbeat-stalled-work.md, PR #26 at 32642cd). Only with
    # PERSISTENCE_HEARTBEAT_STALLED on, and only on a beat whose queue and ledger were read whole. ----

    def note(self, line):
        """A journal line that isn't a report key: written once, after the lock is released."""
        self.notes.append(line)

    def section(self, name):
        return self.data.setdefault(name, {})

    def raise_fault(self, kind, ident):
        """The one standing delivery fault (3.3): raised or updated in place, never a new key. `rev` only grows; after an
        acknowledgement the next fault starts `since`, counts and ids over."""
        if kind not in FAULT_KINDS:
            raise ValueError(f"{kind!r} is not a delivery fault kind")
        f = self.data.get("delivery_fault")
        stamp = self.now.isoformat(timespec="seconds")
        if not f or (isinstance(f.get("acked_rev"), int) and f["acked_rev"] >= f.get("rev", 0)):
            f = {"rev": (f or {}).get("rev", 0), "acked_rev": (f or {}).get("acked_rev"), "since": stamp,
                 "counts": {k: 0 for k in FAULT_KINDS}, "ids": []}
        f["rev"] += 1
        f["counts"][kind] += 1
        f["ids"] = (f["ids"] + [str(ident)])[-10:]
        f["last"] = stamp
        self.data["delivery_fault"] = f

    def held(self, name):
        hold = self.section("holds").get(name)
        until = _time(hold.get("until") if isinstance(hold, dict) else hold)
        return until is not None and self.now < until

    def mode(self):
        return getattr(self, "_mode", None) or {"mode": "detect"}

    def source_on(self):
        """STALLED on needs the heartbeat source. Without it: one visible state, misc.source_off (since when), one
        journal line when it begins and one when it ends, and no R, Q or ticks (no wake whose block can't arrive)."""
        misc = self.section("misc")
        if source_named(self.home):
            if "source_off" in misc:
                del misc["source_off"]
                self.note("heartbeat: source_off over: the heartbeat source is named again")
            return True
        if "source_off" not in misc:
            misc["source_off"] = self.now.isoformat(timespec="seconds")
            self.note("heartbeat: source_off: PERSISTENCE_HEARTBEAT_STALLED is on but 'heartbeat' isn't in "
                      "PERSISTENCE_OBSERVE_SOURCES; no R, Q or ticks")
        return False

    def stalled_layer(self):
        """Steps 2 to 4 of a beat (3, "order within a beat"), after B, C, A and D: settle what was queued, judge ticks,
        then R, Q and the tick, the tick last."""
        if not self.ledger_ok:
            return  # no whole ledger this beat: no decision (as for A and C)
        # One snapshot strategy (Ember's G3): the queue and the ledger are read together under wakes.lock, which run-due
        # holds across its two writes (the enqueued fact, then the queue), and the lock is held until the layer's last
        # enqueue. So no firing can fall between the two reads, and none can move while the layer decides. C and A
        # keep their own reads, as on main.
        # The lock is a BACKSTOP, not the only guard (checked for Claude's point on #28). Without it, no interleaving
        # breaks this layer, because three things hold together: (1) the queue is read BEFORE the ledger; (2) the only
        # other processes that take an item off the queue, run-due and a live session's waiter, append its fact first
        # (enqueued, taken) and rewrite the queue after, so an item read as gone from the queue is already in the ledger
        # read after it: a firing is seen in one or both, never in neither; (3) heartbeat items are added and removed
        # otherwise only by the beat itself, under heartbeat.lock (adds idempotent by id).
        # Removing only the lock survives test_never_two_live_wakes_for_one_key_at_any_point; removing it AND reading the
        # ledger first fails that test. The lock stays so the snapshot doesn't rest on (2) holding for every future
        # writer; test_the_stalled_layer_reads_the_queue_before_the_ledger pins (1).
        with coordination.lock(self.state, "wakes"):
            self.queue = read_queue(self.state)
            try:
                facts, complete = coordination.snapshot(self.state)
            except coordination.LedgerError:
                return
            if not complete:
                return
            self.facts = facts
            self.outs = self.outputs(facts)
            self.states = firing_states(facts, self.now, self.table, wrapper_alive, self.data["crashed_seen"], self.outs,
                                    coordination.ledger_since(self.state))
            self.enq = {f["firing"]: f for f in facts if f.get("fact") == "enqueued" and isinstance(f.get("firing"), str)}
            self.hb_items = {w["id"]: w for w in self.queue if isinstance(w.get("hb"), str)}
            self.hb_fired = {f: e for f, e in self.enq.items() if isinstance(e.get("hb"), str)}
            self.recheck_usage()
            self.settle_queued()
            self.judge_ticks()
            self.check_reports()
            self.check_quiet()
            self.check_tick()

    # 3.2: reconcile, both directions, before any decision.
    def reconcile(self):
        """Holding wakes.lock (so a run-due move can't fall between the two reads), then the ledger under fired.lock.
        Returns False when either can't be read whole: no stalled decision this beat."""
        import hook

        with coordination.lock(self.state, "wakes"):
            queue = read_queue(self.state)
            try:
                facts, complete = coordination.snapshot(self.state)
            except coordination.LedgerError:
                return False
            if not complete:
                return False
            fired = {f["firing"]: f for f in facts if f.get("fact") == "enqueued" and isinstance(f.get("firing"), str)}
            any_fact = {f.get("firing") for f in facts}
            queued = {w.get("id"): w for w in queue}
            known = self.section("known")
            # A crash at write A: state says queued, no wake and no fact. Back to none; not a failed attempt.
            for key, entry in self.queued_entries():
                if entry["wake"] not in queued and entry["wake"] not in any_fact:
                    self.unqueue(key, entry, failed=False)
                    entry["attempt"] = max(0, entry.get("attempt", 1) - 1)
            for tick, t in list(self.section("ticks").items()):
                if not t.get("judged") and tick not in queued and tick not in any_fact:
                    del self.section("ticks")[tick]  # never fired; the cap slot and last_tick stay spent
            # Duplicates first (never two live wakes for one key), then orphans: adopted, never enqueued beside.
            remove = []
            for w in queue:
                hb = w.get("hb")
                if not isinstance(hb, str) or w.get("id") in known:
                    continue
                live = self.live_wake_for(hb, queued)
                if live and live != w["id"]:
                    remove.append(w["id"])
            if remove:
                hook.write_queue_locked([w for w in queue if w.get("id") not in remove])
                for wid in remove:
                    self.note(f"heartbeat: removed duplicate heartbeat wake {wid}")
                queued = {k: v for k, v in queued.items() if k not in remove}
            for wid, w in list(queued.items()) + [(f, e) for f, e in fired.items() if f not in queued]:
                hb = w.get("hb")
                if not isinstance(hb, str) or wid in known:
                    continue
                self.adopt(wid, hb, w, unfired=wid in queued and wid not in fired)
        return True

    def queued_entries(self):
        """(key, entry) for every `queued` entry: the quiet spell (key "quiet") and each R report key."""
        out = []
        q = self.data.get("quiet")
        if q and q.get("state") == "queued":
            out.append(("quiet", q))
        out += [(k, e) for k, e in self.section("reports_q").items() if e.get("state") == "queued"]
        return out

    def unqueue(self, key, entry, failed):
        entry["state"] = "none"
        if failed:
            entry["failures"] = entry.get("failures", 0) + 1
            if entry["failures"] >= 3:
                self.note(f"heartbeat: report undeliverable ({'quiet' if key == 'quiet' else key})")
                self.raise_fault("undeliverable", entry.get("wake"))
                self.section("holds")[key] = (self.now + UNDELIVERABLE_WAIT).isoformat(timespec="seconds")
                entry["failures"] = 0

    def live_wake_for(self, hb, queued):
        """The wake id state already holds as live for this hb key, if any."""
        kind = hb.partition(":")[0]
        if kind == "quiet":
            q = self.data.get("quiet") or {}
            return q.get("wake") if q.get("state") == "queued" and q.get("wake") in queued else None
        if kind == "reports":
            return next((e["wake"] for e in self.section("reports_q").values()
                         if e.get("state") == "queued" and e.get("wake") in queued), None)
        gen = hb.partition(":")[2]
        return next((t for t, e in self.section("ticks").items()
                     if e.get("gen") == gen and not e.get("judged") and t in queued), None)

    def adopt(self, wid, hb, w, unfired):
        kind, _, rest = hb.partition(":")
        at = _time(w.get("set")) or _time(w.get("at")) or self.now
        stamp = at.isoformat(timespec="seconds")
        if kind == "quiet":
            q = self.data.get("quiet") or {}
            if q.get("spell") != rest:
                q = {"spell": rest, "attempt": 0, "failures": 0, "backoff": 0}
            q.update(state="queued" if unfired else "delivered", wake=wid, at=stamp, last_q=stamp,
                     attempt=q.get("attempt", 0) + 1)
            self.data["quiet"] = q
        elif kind == "reports":
            self.section("misc")["reports_at"] = stamp
            if unfired:
                for key in self.unread_keys():
                    e = self.section("reports_q").setdefault(key, {"attempt": 0, "failures": 0})
                    e.update(state="queued", wake=wid, at=stamp, attempt=e.get("attempt", 0) + 1)
        else:
            self.section("ticks")[wid] = {"gen": rest, "at": stamp}
            last = _time(self.section("misc").get("last_tick"))
            if last is None or at > last:
                self.section("misc")["last_tick"] = stamp
        self.section("known")[wid] = stamp
        self.note(f"heartbeat: adopted orphan wake {wid}")

    # 3.1: queued is not delivered; failure leaves it retryable.
    def ack_intents(self):
        """{wake: "live" | "abandoned"} for each ack intent on disk (observed.ack_intent). Live: its owner (pid AND
        /proc start time) is alive and it is younger than the ack's lock wait plus a margin, so that ack may still
        record. Anything else is abandoned: a timeout, a crash, a recycled pid, or a file that doesn't parse."""
        out, root = {}, self.state / "heartbeat-ack-intent"
        if not root.is_dir():
            return out
        for p in root.iterdir():
            try:
                i = json.loads(p.read_text(encoding="utf-8"))
                at = _time(i.get("at"))
                live = (at is not None and self.now - at <= ACK_INTENT_FRESH
                        and wrapper_alive(i.get("pid"), i.get("start")))
            except (OSError, ValueError, AttributeError):
                live = False
            out[p.name] = "live" if live else "abandoned"
        return out

    def settle_queued(self):
        intents = self.ack_intents()
        for firing, e in self.hb_fired.items():
            state = self.states.get(firing)
            if state in ("never-started", "crashed", "failed") and firing not in self.section("fault_seen"):
                # Seen once: a usage-limited one sets its hold once, so recheck_usage can end that hold for good.
                self.section("fault_seen")[firing] = self.now.isoformat(timespec="seconds")
                if not self.usage_hold(firing):
                    self.raise_fault("hb_failed", firing)
        for key, entry in self.queued_entries():
            wake = entry["wake"]
            if wake in self.hb_items:
                continue
            if wake not in self.enq:
                self.unqueue(key, entry, failed=False)  # cancelled or dropped: back to none
                continue
            state = self.states.get(wake)
            if state in (None, "running") or intents.get(wake) == "live":
                continue  # an ack in flight: no failure, and not unqueued; it records under the lock, or is abandoned
            if state in ("never-started", "crashed", "failed", "ok", "taken"):
                # ok with the entry still queued: the block never reached the session, or its ack was abandoned after
                # the block was shown (an intent says so: retryable, not a failed attempt).
                self.unqueue(key, entry, failed=wake not in intents and not self.usage_hold(wake))
        live = {e["wake"] for _, e in self.queued_entries()}
        for name, kind in intents.items():
            if kind == "abandoned" and name not in live and self.states.get(name) not in (None, "running"):
                (self.state / "heartbeat-ack-intent" / name).unlink(missing_ok=True)  # consumed: its wake has ended
        q = self.data.get("quiet")
        if q and q.get("state") == "delivered" and self.states.get(q.get("wake")) not in (None, "running"):
            q["state"] = "none"  # its session ended; if the spell is still this one, nothing happened: back off
            if not self.usage_hold(q.get("wake")):
                q["backoff"] = q.get("backoff", 0) + 1  # a usage-limited one never ran: the hold, not a backoff

    def usage_hold(self, firing):
        """A heartbeat firing that hit the usage limit is no fault and no failed attempt: nothing of the stalled layer
        wakes before the reset plus 5 minutes (section 5, usage limit). holds.usage is structured: {reset: parsed |
        unknown, since, until}. An unknown reset holds until UNKNOWN_RECHECK after the finish, with one journal line,
        and is rechecked every beat (recheck_usage)."""
        out, finished = self.finished_output(firing)
        if not usage_limited(out):
            return False
        end = finished or self.now
        reset = reset_time(out.get("result"), end)
        until = reset + timedelta(minutes=5) if reset else end + UNKNOWN_RECHECK
        holds = self.section("holds")
        old = holds.get("usage") if isinstance(holds.get("usage"), dict) else {}
        if (_time(old.get("until")) or self.now) <= until:
            holds["usage"] = {"reset": "parsed" if reset else "unknown", "since": end.isoformat(timespec="seconds"),
                              "until": until.isoformat(timespec="seconds")}
            if not reset and old.get("until") != holds["usage"]["until"]:
                self.note(f"heartbeat: usage limit, reset unknown; the stalled layer holds and rechecks every beat, "
                          f"next try {hhmm(until)}")
        return True

    def recheck_usage(self):
        """Every beat: a hold past its until is dropped; an unknown one ends early once any firing finished after it
        began without the usage limit (the provider answers again)."""
        holds = self.section("holds")
        hold = holds.get("usage")
        if hold is None:
            return
        if not isinstance(hold, dict) or not self.held("usage"):
            del holds["usage"]
            return
        since = _time(hold.get("since"))
        if hold.get("reset") != "unknown" or since is None:
            return
        for f in self.facts:
            if f.get("fact") != "finished" or (_time(f.get("at")) or EPOCH) <= since:
                continue
            out = self.outs.get(f.get("output"))
            if isinstance(out, dict) and not usage_limited(out):
                del holds["usage"]
                self.note("heartbeat: usage hold over (a firing ended without the limit)")
                return

    # 3.0 and 3.0.1: what "running" and "activity" mean.
    def running_now(self):
        return running_mine(self.states, self.table, room_session())

    def activity(self):
        return last_activity(self.facts, self.states, self.tasks(), self.queue)

    def tasks(self):
        if not hasattr(self, "_tasks"):
            self._tasks = read_tasks(self.state)
        return self._tasks

    def due_within(self, span):
        return any(isinstance(w.get("at"), (int, float)) and w["at"] < (self.now + span).timestamp() for w in self.queue)

    def next_due(self):
        ats = [w["at"] for w in self.queue if isinstance(w.get("at"), (int, float))]
        return datetime.fromtimestamp(min(ats), timezone.utc) if ats else None

    # R: reports reach a self.
    def unread_keys(self):
        """Report keys emitted at least an hour ago (a paused-claim key at once: its 24 h is its wait), neither queued
        nor delivered, not held, and not caused by the heartbeat itself (3.3)."""
        hb_ids = set(self.hb_items) | set(self.hb_fired)
        out = []
        for key, at in sorted(self.data["reported"].items()):
            when = _time(at)
            if when is None or not r_eligible(key, hb_ids):
                continue
            if not key.startswith("paused:") and self.now - when < timedelta(hours=1):
                continue
            if self.section("reports_q").get(key, {}).get("state") in ("queued", "delivered") or self.held(key):
                continue
            out.append(key)
        return out

    def paused_claims(self):
        """A claim paused with no `until` for more than 24 h becomes a report key (R delivers it)."""
        for record in self.tasks():
            if record.get("status") != "paused" or record.get("until") or record.get("pause_until"):
                continue
            hist = record.get("history") or []
            idx = max((i for i, h in enumerate(hist) if isinstance(h, dict) and h.get("action") == "pause"), default=None)
            since = _time(hist[idx].get("at")) if idx is not None else _time(record.get("updated"))
            if since is None or self.now - since <= timedelta(hours=24):
                continue
            try:
                key = f"paused:{_uuid(record.get('id'))}:{idx if idx is not None else 0}"
            except ValueError:
                continue
            self.report(key, f"heartbeat claim {record['id']}: paused with no until since {hhmm(since)}, at {hhmm(self.now)}")

    def check_reports(self):
        self.paused_claims()
        if self.held("usage"):
            return
        last = _time(self.section("misc").get("reports_at"))
        if last is not None and self.now - last < REPORTS_EVERY:
            return
        if any(e.get("state") == "queued" for e in self.section("reports_q").values()):
            return
        keys = self.unread_keys()
        if not keys or not self.take_cap(required=True):
            return
        wid = str(uuid.uuid4())
        stamp = self.now.isoformat(timespec="seconds")
        for key in keys:
            e = self.section("reports_q").setdefault(key, {"attempt": 0, "failures": 0})
            e.update(state="queued", wake=wid, at=stamp, attempt=e.get("attempt", 0) + 1)
        self.section("misc")["reports_at"] = stamp
        self.enqueue(wid, "reports", "reports", {"user": self.user})

    # Q: quiet.
    def check_quiet(self):
        if self.mode().get("mode") == "working":
            return  # ticks supersede Q
        if self.held("usage") or self.held("quiet"):
            return
        running = self.running_now()
        if running is not False:
            return  # running, or unknown liveness: no decision
        H = quiet_after(self.home)
        if self.due_within(H):
            return
        act = self.activity()
        spell = (act or EPOCH).isoformat(timespec="seconds")
        q = self.data.get("quiet")
        if q and q.get("state") == "queued":
            return
        if not q or q.get("spell") != spell:
            q = {"spell": spell, "state": "none", "attempt": 0, "failures": 0, "backoff": 0,
                 "last_q": (q or {}).get("last_q")}
            self.data["quiet"] = q
        if q.get("state") != "none":
            return
        need = min(H * (2 ** q.get("backoff", 0)), BACKOFF_MAX) if q.get("backoff") else H
        quiet_since = max(act or EPOCH, (_time(q.get("last_q")) if q.get("backoff") else None) or EPOCH)
        if self.now - quiet_since < need:
            return
        if not self.cap_has_room():
            self.defer("quiet")
            return
        self.take_cap(required=False)
        wid = str(uuid.uuid4())
        stamp = self.now.isoformat(timespec="seconds")
        q.update(state="queued", wake=wid, at=stamp, last_q=stamp, attempt=q.get("attempt", 0) + 1)
        nxt = self.next_due()
        open_or_paused = sum(1 for r in self.tasks() if r.get("status") in ("open", "paused"))
        self.enqueue(wid, "quiet", f"quiet:{spell}", {"since": hhmm(act) if act else "never",
                                                      "before": hhmm(nxt) if nxt else "none",
                                                      "claims": open_or_paused, "reports": len(self.unread_keys()),
                                                      "user": self.user})

    def defer(self, what):
        """A full cap deferring Q or the tick: one journal line per cap window, nothing else (no fault, no key)."""
        start, _ = self._cap_window()
        window = start.isoformat()
        seen = self.section("deferred")
        if what not in seen.get(window, []):
            seen.clear()  # only the current window is kept
            seen.setdefault(window, []).append(what)
            self.note(f"heartbeat: {what} deferred, cap full")
        else:
            seen.setdefault(window, [])

    # Section 5: the working-mode tick, always the beat's last decision.
    def check_tick(self):
        m = self.mode()
        if m.get("mode") != "working" or self.held("usage"):
            return
        gen = m["gen"]
        if self.running_now() is not False or self.due_within(BEAT):
            return
        gap, _ = tick_gap(self.home)
        last = _time(self.section("misc").get("last_tick"))
        if last is not None and self.now - last < gap:
            return
        if any(t.get("gen") == gen and not t.get("judged") for t in self.section("ticks").values()):
            return
        if not self.cap_has_room():
            self.defer("tick")
            return
        self.take_cap(required=False)
        wid = str(uuid.uuid4())
        stamp = self.now.isoformat(timespec="seconds")
        self.section("ticks")[wid] = {"gen": gen, "at": stamp}
        self.section("misc")["last_tick"] = stamp
        self.enqueue(wid, "tick", f"tick:{gen}", {"tick": wid})

    def judge_ticks(self):
        """Each tick of the current gen judged once, when its firing is final, in queue order (section 5)."""
        m = self.mode()
        if m.get("mode") != "working":
            return
        gen = m["gen"]
        c = self.section("counters")
        if c.get("gen") != gen:
            c.clear()
            c.update(gen=gen, empty=0, failed=0, since=m.get("set_at"), empty_ids=[], failed_ids=[])
        for tick, t in sorted(self.section("ticks").items(), key=lambda kv: kv[1].get("at", "")):
            if t.get("gen") != gen or t.get("judged"):
                continue
            if tick in self.hb_items:
                break  # not fired yet; later ticks wait for it
            if tick not in self.enq:
                t["judged"] = "neither"  # cancelled before it fired
                continue
            state = self.states.get(tick)
            if state in (None, "running"):
                break
            out, finished = self.finished_output(tick)
            end = finished or self.now
            if state == "taken" or usage_limited(out):
                t["judged"] = "neither"
                if usage_limited(out):
                    self.usage_hold(tick)
                continue
            if state == "ok":
                verdict = "progress" if t.get("outcome") == "continue" or self.progress(_time(c.get("since")), end) \
                    else "empty"
            else:
                verdict = "failed"
            t["judged"] = verdict
            c["since"] = end.isoformat(timespec="seconds")
            if verdict == "progress":
                c.update(empty=0, failed=0, empty_ids=[], failed_ids=[])
            elif verdict == "empty":
                c.update(empty=c["empty"] + 1, failed=0, failed_ids=[], empty_ids=(c["empty_ids"] + [tick])[-3:])
            else:
                c.update(failed=c["failed"] + 1, failed_ids=(c["failed_ids"] + [tick])[-3:])
            if c["empty"] >= 3:
                self.end_mode(gen, {"mode": "detect", "set_at": self.now.isoformat(timespec="seconds"),
                                    "ended": "3 ticks without progress", "gen": gen, "ticks": c["empty_ids"]},
                              f"working:ended:{gen}", "ended, 3 ticks without progress")
                return
            if c["failed"] >= 3:
                self.end_mode(gen, {"mode": "suspended", "gen": gen, "since": self.now.isoformat(timespec="seconds"),
                                    "ticks": c["failed_ids"]},
                              f"working:suspended:{gen}", "SUSPENDED, 3 failed ticks")
                return

    def progress(self, since, end):
        """Structural progress in (since, end]: a claim change not by the heartbeat or the harness, or a wake added
        that isn't the heartbeat's. Never text."""
        def inside(at):
            return at is not None and (since is None or at > since) and at <= end
        for record in self.tasks():
            for h in record.get("history") or []:
                if isinstance(h, dict) and h.get("by") not in ("heartbeat", "harness") and inside(_time(h.get("at"))):
                    return True
        for w in list(self.queue) + list(self.enq.values()):
            if w.get("by") != "heartbeat" and not w.get("hb") and inside(_time(w.get("set"))):
                return True
        return False

    def end_mode(self, gen, record, key, words):
        """A breaker: write the mode file only if it still holds this gen (compare and set), then one R report key."""
        current = read_mode(self.state)
        if current.get("gen") != gen or current.get("mode") != "working":
            return
        write_atomic(mode_path(self.state), record)
        self._mode = record
        history = self.section("misc").setdefault("mode_history", [])
        history.append({k: record.get(k) for k in ("mode", "gen", "since", "ended", "ticks")} | {"at": self.now.isoformat(timespec="seconds")})
        del history[:-10]
        self.report(key, f"heartbeat working mode: {words}, gen {gen}, at {hhmm(self.now)}")

    # 3.2: the enqueue order. Intent first (write A), then the wake (write B).
    def enqueue(self, wid, kind, hb, fill):
        import hook

        self.section("known")[wid] = self.now.isoformat(timespec="seconds")
        save(self.state, self.data)  # fault point A
        try:
            item = hook.add_heartbeat_wake(id=wid, kind=kind, session=str(uuid.uuid4()), launch="session-id",
                                           at=self.now.timestamp(), cwd=str(Path(self.home or Path.home()) / "work"),
                                           hb=hb, fill=fill, set_at=self.now)  # fault point B
        except ValueError as e:
            print(f"heartbeat: no {kind} wake: {e}", file=sys.stderr)
            return
        if item:
            self.hb_items[wid] = item


START_GRACE = timedelta(minutes=5)
BEAT = timedelta(minutes=10)
FAULT_KINDS = ("cap", "hb_failed", "undeliverable")
UNDELIVERABLE_WAIT = timedelta(hours=6)
REPORTS_EVERY = timedelta(hours=6)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def r_eligible(key, hb_ids):
    """Keys the heartbeat causes itself are never R keys (3.3): cap notices, and reports about its own Q, R or tick
    wakes. Everything else it reports is."""
    if key.startswith("cap:"):
        return False
    if key.startswith("wake:"):
        return key.split(":")[1] not in hb_ids
    return True


def room_session():
    """The room session's id from its session.json: "" when there is no room file, None when it can't be read."""
    import hook

    try:
        d = json.loads(hook.ROOM_SESSION_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        return None
    sid = d.get("id") if isinstance(d, dict) else None
    return sid if isinstance(sid, str) else None


def running_mine(states, table, room):
    """Section 3.0: True when a ledger firing is running (or about to: enqueued, not yet decided), or the room session
    is held by a live process; None when liveness can't be read; else False. Remote Control and other Claude processes
    never count: only a firing's own record or the room's session id is looked for."""
    import claims

    if table is None or room is None:
        return None
    if any(state in ("running", None) for state in states.values()):
        return True
    return bool(room) and bool(claims.running(room, table))


def last_activity(facts, states, tasks, queue):
    """Section 3.0.1: the latest change to the work itself, or None. A work firing ending (not the heartbeat's own Q, R
    or tick), a claim history entry not by the heartbeat or the harness, or a wake added that isn't the heartbeat's."""
    enq = {f["firing"]: f for f in facts if f.get("fact") == "enqueued" and isinstance(f.get("firing"), str)}
    times = []
    last_fact = {}
    for f in facts:
        firing = f.get("firing")
        if not isinstance(firing, str) or enq.get(firing, {}).get("hb"):
            continue
        last_fact[firing] = f
        if f.get("fact") == "finished":
            times.append(_time(f.get("at")))
    for firing, state in states.items():
        if state in ("crashed", "never-started") and firing in last_fact:
            times.append(_time(last_fact[firing].get("at")))
    for record in tasks:
        for h in record.get("history") or []:
            if isinstance(h, dict) and h.get("by") not in ("heartbeat", "harness"):
                times.append(_time(h.get("at")))
    for w in list(queue) + list(enq.values()):
        if w.get("by") != "heartbeat" and not w.get("hb"):
            times.append(_time(w.get("set")))
    times = [t for t in times if t is not None]
    return max(times) if times else None


def read_tasks(state):
    out = []
    for path in sorted((Path(state) / "tasks").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


# Section 5: the mode file. Missing or malformed means detection; the beat never repairs it.
MODES = ("detect", "working", "suspended")


def mode_path(state):
    return Path(state) / "heartbeat-mode.json"


def read_mode(state):
    """{mode, ...}. Missing: detect. Bad JSON, an unknown mode, or working/suspended without a full gen: detect with
    `malformed` set (the beat writes one journal line and leaves the file as it is)."""
    try:
        m = json.loads(mode_path(state).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"mode": "detect"}
    except (OSError, ValueError):
        return {"mode": "detect", "malformed": True}
    if not isinstance(m, dict) or m.get("mode") not in MODES:
        return {"mode": "detect", "malformed": True}
    if m["mode"] != "detect":
        try:
            _uuid(m.get("gen"))
        except ValueError:
            return {"mode": "detect", "malformed": True}
    return m


def write_atomic(path, obj):
    """A temp file in the same directory, fsync, os.replace, fsync of the directory."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=1, sort_keys=True))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def wrapper_alive(pid, start):
    """The run line's pid is alive with the same /proc start time (a recycled pid has another)."""
    import run_line

    return pid is not None and start is not None and run_line.start_time(pid) == start


def firing_states(facts, now, table, alive, seen, outputs, since=None):
    """Per firing id, its derived state (heartbeat.md, "States"), or None while no decision can be made yet: running,
    never-started (enqueued, no start, past the start grace), pre-ledger (enqueued only, before `since`, the ledger's
    coverage: whether it started is unknown, so nothing is reported or retried), crashed (started, nothing alive, for a full beat after
    first seen so), failed (finished with is_error, a missing or unparseable output, or a non-zero exit), ok, taken.
    `seen` is heartbeat.json's crashed_seen, updated here; `outputs` maps an output file name to its parsed JSON or None."""
    import claims

    by = {}
    for f in facts:
        if isinstance(f.get("firing"), str):
            by.setdefault(f["firing"], {}).setdefault(f.get("fact"), f)
    states = {}
    for firing, got in by.items():
        state = None
        if "taken" in got:
            state = "taken"
        elif "finished" in got:
            out = outputs.get(got["finished"].get("output"))
            bad = got["finished"].get("exit") != 0 or not isinstance(out, dict) or out.get("is_error") is not False
            state = "failed" if bad else "ok"
        elif "started" in got:
            s = got["started"]
            live = claims.running(s.get("session"), table) if claims.full_id(s.get("session")) else False
            if live or live is None or alive(s.get("pid"), s.get("start_time")):
                seen.pop(firing, None)
                state = "running" if live or alive(s.get("pid"), s.get("start_time")) else None
            else:
                first = _time(seen.get(firing))
                if first is None:
                    seen[firing] = now.isoformat(timespec="seconds")
                elif now - first >= BEAT:
                    state = "crashed"
        elif "enqueued" in got:
            at = _time(got["enqueued"].get("at"))
            if at is not None and since is not None and at < since:
                state = "pre-ledger"
            elif at is not None and now - at >= START_GRACE:
                state = "never-started"
        states[firing] = state
    return states


def last_attempt(attempts):
    """The attempt no other attempt in the chain follows: chosen by structure, never by comparing `at`s, since an
    enqueued fact's is ISO text and a queued item's a number (Ember's B3). None for an empty chain."""
    followed = {a.get("follows") for a in attempts}
    ends = [a for a in attempts if a.get("firing") not in followed]
    return ends[-1] if ends else None


def transcript_exists(home, session):
    """A stat of the session's transcript path, never opened."""
    return bool(session) and any((Path(home or Path.home()) / ".claude" / "projects").glob(f"*/{session}.jsonl"))


def claims_spell_entry(record):
    import claims

    i = claims.spell_start(record)
    return record["history"][i] if i is not None else None


def nudge_entry(record):
    """This spell's nudge mark, if any."""
    import claims

    start = claims.spell_start(record)
    return next((h for h in reversed(record["history"][start or 0:]) if h["action"] == "nudge"), None)


def _time(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else None


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


EMPTY = {"reported": {}, "outbox": {}, "crashed_seen": {}, "cap": {}}
# The stalled layer's sections: absent until used, so with it off heartbeat.json keeps exactly today's shape.
STALLED_SECTIONS = ("quiet", "reports_q", "ticks", "counters", "delivery_fault", "fault_seen", "holds", "deferred",
                    "misc", "known")


def load(state):
    """heartbeat.json. Missing initializes; malformed, unreadable or the wrong shape raises StateUnreadable (Ember's
    B2): starting clean would drop the hourly cap, the outbox and the reported keys and then go on deciding. Only an
    absent file or an absent section is missing; a section that is present must be an object, so an explicit null is
    the wrong shape, not an empty section. Inside: `cap` is {} or whole (an aware ISO window and an int count >= 0,
    not a bool, both there), since anything else would reset or weaken the hourly limit; every outbox line is a str.
    crashed_seen's timestamps aren't checked here (a follow-up in the issue drafts)."""
    path = Path(state) / "heartbeat.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise StateUnreadable(f"{path} can't be read ({type(e).__name__})") from None
    if not isinstance(data, dict) or any(k in data and not isinstance(data[k], dict) for k in (*EMPTY, *STALLED_SECTIONS)):
        raise StateUnreadable(f"{path} isn't the heartbeat's state")
    fault = data.get("delivery_fault")
    if fault and not (_count(fault.get("rev")) and isinstance(fault.get("counts"), dict)
                      and (fault.get("acked_rev") is None or _count(fault.get("acked_rev")))):
        raise StateUnreadable(f"{path} has a delivery_fault that isn't whole")  # a bad rev could hide a fault
    cap = data.get("cap", {})
    if cap and not ("window" in cap and "count" in cap and _time(cap["window"]) and _count(cap["count"])):
        raise StateUnreadable(f"{path} has a cap that isn't empty or whole")
    if not all(isinstance(line, str) for line in data.get("outbox", {}).values()):
        raise StateUnreadable(f"{path} has an outbox line that isn't text")
    out = {k: dict(data[k]) if k in data else {} for k in EMPTY}
    out.update({k: data[k] for k in STALLED_SECTIONS if k in data})
    return out


def save(state, data):
    write_atomic(Path(state) / "heartbeat.json", data)


def journal(line):
    print(line, flush=True)


def emit(state, sink):
    """Phase 2, holding no lock: every outbox line, including ones an earlier beat left. Returns the confirmed keys."""
    confirmed = []
    for key, line in sorted(load(state)["outbox"].items()):
        try:
            sink(line)
        except OSError:
            break  # the journal refused: nothing after this is marked; the next beat emits again
        confirmed.append(key)
    return confirmed


def mark(state, keys, now, home=None):
    """Phase 3: retake heartbeat.lock (30 s), mark each confirmed key and drop it from the outbox. A claimed claim's key
    sets its `reported` through the claims module (under tasks/.lock) only while the claim is still in the key's
    spell; otherwise the entry is just dropped."""
    if not keys:
        return
    try:
        with coordination.lock(state, "heartbeat", wait=MARK_WAIT):
            data = load(state)
            for key in keys:
                if key not in data["outbox"]:
                    continue
                del data["outbox"][key]
                if key.startswith("claim:"):
                    import claims

                    _, claim, spell, _kind = key.split(":")
                    c = claims.Claims(state, home=home, now=lambda: now)
                    try:
                        record = c.get(claim)
                    except claims.Refused:
                        continue
                    if record["status"] == "claimed":
                        if not c.mark_reported(claim, int(spell)):
                            continue  # the spell ended between emission and marking: dropped
                    elif record["status"] != "open":
                        continue
                data["reported"][key] = now.isoformat(timespec="seconds")
            save(state, data)
    except coordination.LockBusy:
        return  # nothing marked; the next beat emits again


def diagnose(error):
    print(f"heartbeat: {error}; no decisions this beat, and the file is left as it is", file=sys.stderr, flush=True)


def beat(state=None, home=None, now=None, sink=journal, systemctl=systemctl_run, user=None):
    """One beat. Returns "off", "busy" (another beat holds heartbeat.lock), "ok", "queue-unreadable" (no wake decision
    this beat; D and earlier reports still go) or "state-unreadable" (nothing decided, emitted or marked)."""
    if not enabled(home):
        return "off"
    state = Path(state or STATE)
    now = now or (lambda: datetime.now(timezone.utc))
    if user is None:
        import pwd

        user = pwd.getpwuid(os.geteuid()).pw_name
    status = "ok"
    notes = []
    try:
        with Beat(state, home, now, sink, systemctl, user) as b:
            notes = b.notes
            whole = False
            try:
                if b.stalled:
                    b._mode = read_mode(state)
                    if b._mode.get("malformed"):
                        b.note("heartbeat: heartbeat-mode.json is malformed; detection this beat, the file left as it is")
                    if tick_gap(home)[1]:
                        b.note(f"heartbeat: PERSISTENCE_HEARTBEAT_TICK_GAP is under {TICK_GAP} minutes; {TICK_GAP} is used")
                    whole = b.reconcile()  # 3.2: before any decision
                b.check_overdue()  # reads the queue first, so a bad one stops the beat before any decision
                b.check_ledger()  # step 1: B above, then C and A
            except QueueUnreadable as e:
                diagnose(e)
                status = "queue-unreadable"
                whole = False
            b.check_units()  # D
            if b.stalled and whole and b.source_on():
                b.stalled_layer()  # R, Q, then the tick last
        for line in notes:
            try:
                sink(line)
            except OSError:
                break
        mark(state, emit(state, sink), now(), home=home)
    except coordination.LockBusy:
        return "busy"
    except StateUnreadable as e:
        diagnose(e)
        return "state-unreadable"
    return status


# The commands (`pheartbeat <cmd>`, a one-line wrapper around this file). Each takes heartbeat.lock, waiting briefly.
CMD_WAIT = 10


class Refused(Exception):
    pass


def hold_line(hold):
    """holds.usage for status: parsed or unknown, never a guess shown as a reset."""
    until = _time(hold.get("until")) if isinstance(hold, dict) else None
    if until is None:
        return None
    if hold.get("reset") == "parsed":
        return f"usage hold until {hhmm(until)} (reset read from the output)"
    return f"usage hold: reset unknown (no reset time in the output); rechecked every beat, next try {hhmm(until)}"


def command(args, state=None, now=None, out=print, home=None):
    """status | working | detect | continue <tick id> | ack-fault <rev>. Returns 0, or 1 when refused."""
    import observed

    state = Path(state or STATE)
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    try:
        with coordination.lock(state, "heartbeat", wait=CMD_WAIT):
            data = load(state)
            mode = read_mode(state)
            verb = args[0] if args else "status"
            if verb == "status" and len(args) == 1:
                out(f"mode {mode['mode']}" + (f", gen {mode['gen']}" if mode.get("gen") else "")
                    + (" (the file is malformed: detection)" if mode.get("malformed") else ""))
                misc = data.get("misc", {})
                out(f"settings: {settings_path(home)} (its env block only; the environment is never read)")
                if misc.get("source_off"):
                    out(f"stalled layer: source_off since {hhmm(_time(misc['source_off']) or now)} ('heartbeat' isn't "
                        f"in PERSISTENCE_OBSERVE_SOURCES): no R, Q or ticks")
                usage = data.get("holds", {}).get("usage")
                if usage and hold_line(usage) and (_time(usage.get("until")) or now) > now:
                    out(hold_line(usage))
                if misc.get("last_tick"):
                    out(f"last tick {misc['last_tick']}")
                if mode["mode"] == "suspended":
                    out(observed.suspended_line(mode))
                if observed.fault_showing(data.get("delivery_fault")):
                    out(observed.fault_line(data["delivery_fault"]))
                for entry in misc.get("mode_history", [])[-5:]:
                    out(f"earlier: {json.dumps(entry, sort_keys=True)}")
                return 0
            if verb in ("working", "detect") and len(args) == 1:
                if mode["mode"] == "suspended":
                    data.setdefault("misc", {}).setdefault("mode_history", []).append(
                        {k: mode.get(k) for k in ("mode", "gen", "since", "ticks")} | {"acknowledged": stamp})
                    data["misc"]["mode_history"] = data["misc"]["mode_history"][-10:]
                    save(state, data)
                record = {"mode": "working", "gen": str(uuid.uuid4()), "set_at": stamp} if verb == "working" \
                    else {"mode": "detect", "set_at": stamp}
                write_atomic(mode_path(state), record)
                out(f"mode {verb}" + (f", gen {record['gen']}" if verb == "working" else ""))
                return 0
            if verb == "continue" and len(args) == 2:
                tick = data.get("ticks", {}).get(args[1])
                if not tick or mode["mode"] != "working" or tick.get("gen") != mode.get("gen"):
                    raise Refused(f"{args[1]} is not a tick of the current working gen")
                if tick.get("judged"):
                    raise Refused(f"tick {args[1]} is already judged ({tick['judged']})")
                facts, _ = coordination.snapshot(state)
                if not any(f.get("firing") == args[1] and f.get("fact") == "started" for f in facts):
                    raise Refused(f"tick {args[1]} hasn't started")
                tick["outcome"] = "continue"
                save(state, data)
                out(f"tick {args[1]}: continue")
                return 0
            if verb == "ack-fault" and len(args) == 2:
                f = data.get("delivery_fault")
                try:
                    rev = int(args[1])
                except ValueError:
                    raise Refused("ack-fault takes the fault's rev") from None
                if not observed.fault_showing(f):
                    raise Refused("no delivery fault is showing")
                if f["rev"] != rev:
                    raise Refused(f"the fault is at rev {f['rev']} now, not {rev}: read it again")
                f["acked_rev"] = rev
                save(state, data)
                out(f"delivery fault rev {rev} acknowledged")
                return 0
            raise Refused("usage: pheartbeat status | working | detect | continue <tick id> | ack-fault <rev>")
    except Refused as e:
        out(f"refused: {e}")
        return 1
    except coordination.LockBusy:
        out("refused: a beat holds heartbeat.lock; try again")
        return 1
    except StateUnreadable as e:
        out(f"refused: {e}")
        return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        return command(argv)
    return 0 if beat() in ("off", "ok", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
