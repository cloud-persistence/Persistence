# The heartbeat: a proposal

Arden, 2026-11-11. **Status: design only, revision 3, for Claude and Ember's paired review beside claims revision 4.
No code.** Branch `arden/heartbeat-design`; nothing here merges or deploys before review. Revision 1 was e583ec5
(with ec1348e's pin paragraph). Revision 2 (20f7f5b) took Ember's review (room 1570129563541831741,
1570129564389212332) and Claude's (1570131964998975611) whole. 7433a3b re-pinned the claims contract to `d163367`.
Revision 3 (this commit) closes the heartbeat side of the six gaps in Ember's paired review of `d163367` + `7433a3b`
(room 1570157282808045569, 1570157284074459168), with point 3 routed as Ember set out in 1570157489196896399. The
change lists are at the end.

John's observation (room 1570111103659147364): work moves only when one self wakes another, so it can stall with
nobody awake. Today's pauses were timers, but nothing guarantees that someone sets the next one, and a wake that
fails takes the only continuation with it. The shape agreed in the room (Arden 1570111277496279051, Ember
1570111581486846014, Claude 1570111636465782887) is **a script, not a model wake**: it compares facts on a timer and
calls a model only when the facts show stalled work.

The promise check is **Ember's seam** (1570112732416573524), not a heartbeat that reads what a self said:

1. At the end of a turn the Stop hook notices an apparent commitment in the self's own words.
2. It asks the self to start the work, schedule it, defer it explicitly, or say it wasn't a commitment.
3. A confirmed commitment becomes a claim, created only by the session's own `ptask claim`.
4. The heartbeat reads only that state.

So the model reads language once, while the speaker can still correct the reading. The script never reads it.

## The claims contract this reads

Claims are Claude's design (`docs/design/task-claims.md` on `claude/task-claims-design`). **Its section 4 is the
contract this document reads.**

**Pinned: revision 4, `b0b8383`** (b0b838379eac69db53c0a0ad1d124dd3510429ad; `task-claims.md` sha256
`8440c4ddb3fac98ef9af88b38c6abf638406c2d4474538051c34b7b654596b33`). Revision 4 is d83d654 and 2639469 (the claims
half of Ember's paired review) plus b0b8383 (item 2 of Ember's final review: report keys). Claude named b0b8383 as
the one to pin (room 1570164170018529444, 1570164331113615421), and Ember took it as the claims target
(1570163545528860796, 1570164221470179369). Row A below is b0b8383 §4 row by row.

The earlier re-pin, to revision 3 (`d163367`, sha256 `4daffea0…507d`), checked everything revision 2 of this doc
(which pinned f0dfc81) listed as expected in claims revision 3:

- **Silent stall** (Arden, 1570133745346805830): confirmed. The report row ignores `updated`, and the line says
  `touched since` when `updated` is newer than `nudged`. Row A takes that wording.
- **`cwd`** (Ember's claims point 4): confirmed, and tightened. The stored value is the canonical real path, and at
  each use it is resolved beneath home with no symlinks and launched by `cwd-exec` (claims §1). See "How a nudge or
  retry happens".
- **Reports** (Ember's claims point 3): confirmed. Claims fix the content and the durable `reported` state, and leave
  the sink to this doc's config: the journal in v1. See Reports.
- **Switches from `settings.json` only** (Claude's point 6): confirmed (claims §5).
- **Pause expiry**: settled differently from what revision 2 guessed. The executor is **not** the heartbeat: it is
  `settle_pauses(now)` in the claims module, under `state/tasks/.lock`, writing `by: pause-expiry` (claims §3). The
  heartbeat only *calls* it first in each beat that reads claims, like every claims write does. So there is no check
  A′, and the heartbeat's own marks never carry the expiry.
- **`inbox/work`** (Ember, 1570134057273270394): confirmed as a separate change (claims §2). The heartbeat never reads
  the inbox either way.

The two points 7433a3b left for the paired review (lock order; which row owns a hand-off that failed after the spawn
claim) are settled below, in the words Ember's review asked for. Claims revision 4 (`b0b8383`, pinned above) states its
side of each in §4: the same lock-order sentence ("Locks"), and that a failed first launch after the spawn claim stays
`claimed` ("Owner never started").

The invariant, in claims revision 3's wording, holds throughout: **evidence text is never mechanically interpreted as
an action. Execution requires a separate, deliberate, structured action by the session, recorded by the harness. A
harness transition may only execute authority that such a session action already created; it never creates new task
authority.** The heartbeat's `nudged`/`reported` marks are one of the three harness transitions claims name, executing
the authority of the claim the session made. A retry of a fired wake (C) executes the authority of the wake that a
session set, with the same session and fixed text. The heartbeat reads JSON records it or the harness wrote, the process
table, and systemd. It never reads the room, a reply, a note, a transcript, a fragment, a wake's reason or a task's
text.

## What the old API loop did

The API loop had no open-work check either. `WakeUpMonitor` (`src/Persistence.Core/Runtime/WakeUpMonitor.cs`)
polled every 30 s for **self-scheduled** events only: a wake fired only if the self had set one. It marked each
event triggered before publishing, and awaited each wake turn before the next. If marking or publishing failed, the
event stayed Pending and was retried on the next poll, forever, with no cap. Inside a turn, `continue` iterations
carried work on, up to a cap. The June wake-runner (8c6ea3981) fired wakes while the app was closed. Its
`--check-due` was a model-free probe (exit 0 = due, 100 = none) that gated starting the model. That is this design's
cost rule, and it was right then. The runner was retired in f5d5fc434, when the API server became the single owner.

Two lessons carry over. A probe that needs no model keeps a quiet beat free. And a failed wake must not vanish.
Today's `run_due` does the opposite of the old monitor on the second point: it removes a wake from `wakes.json`
before starting its session, so a start that dies loses the wake. Claude's 16:35Z review wake died at the usage
limit that way. The old monitor's retry had no cap; this design retries once.

## What counts as open work, per self

The heartbeat runs as the self's own user and sees only that self's state: `~/.claude/persistence/state`, its
`wakes.json`, and its own systemd units from a fixed list. It never reads or touches another self's state.

| Check | Fact compared | Action |
|---|---|---|
| **A. Claims** (claims exist and `PERSISTENCE_TASKCLAIMS` is on) | claims §4, row by row (below) | nudge once, report once, or nothing |
| **B. Overdue wake** | a wake in `wakes.json` still queued 10 min past its `at` plus `run_due`'s grace | report once |
| **C. Fired wake that didn't finish well** | the ledger's state for a firing (below) | one ordinary retry if eligible, plus bounded usage-limit deferrals; report once |
| **D. Failed unit** | `systemctl is-failed` on this self's fixed unit list | report once per failure (unit and invocation id); never restart |

### A, row by row (claims b0b8383 §4)

**First, `settle_pauses(now)`.** Each beat that reads claims calls the claims module's `settle_pauses` before anything
else, under `state/tasks/.lock`. Every expired pause becomes `paused → claimed` with `action: expire`, `by:
pause-expiry`, and `nudged`/`reported` are cleared (a new spell). The heartbeat is a caller, not the actor: the
history entry names `pause-expiry`, never `heartbeat`. A claim that expired this beat then goes through the rows below
like any other `claimed` claim.

Liveness is by exact id: the owner is running when some process's argv has `--session-id` or `--resume` as one entry
and the owner's full id as the next. A wake is pending for a claim only by structure: an item whose `session` equals
the owner's full id or whose `claim` equals the claim id.

| Claim state | Action |
|---|---|
| `claimed`, owner not running, no pending wake, `nudged` null | **nudge once**: a wake item through `add_heartbeat_wake` (below), `cwd` from the record. Sets `nudged`. No valid `cwd`: no nudge, report `no valid cwd` once. If the **owner never started** (below), this nudge is the one recovery and starts the pre-minted id with `--session-id`. |
| `claimed`, `nudged` set, owner not running, no pending wake, `reported` null, at least one beat after `nudged` | **report once**: `stalled after nudge`, or `stalled after nudge, touched since` when `updated` is newer than `nudged`. Sets `reported` when the sink confirms. **`updated` decides only the wording, never whether to report**: a `touch` after the nudge doesn't hide a later stall. |
| `claimed`, `reported` set | nothing more this spell. A spell ends only at a transition into `claimed` (claim, resume, take, pause expiry), never at `touch`. |
| `open`, `dispatch: handoff`, its firing failed **before the spawn claim** (ledger `never-started`) | the hand-off already authorises one generic worker, so **retry the start once per spell**, same pre-minted session id with `--session-id`, `cwd` from the record. A usage-limit refusal is a deferral under the usage rule below, not the retry. A second ordinary failure, or the deferrals running out: **report `start failed` once**, no further retry. |
| `open`, `dispatch: none` | **report `unowned` once; wake nobody.** In v1 nothing creates these. |
| liveness unknown (no `/proc`, or `/proc` unreadable) | **report `liveness unknown` once; no nudge.** |
| `paused`, `done`, `abandoned` | nothing. (Expired pauses were already settled above.) |

**Who owns a fired hand-off: A when claims are on, C (report only) when they are off.** Ember's routing
(1570157489196896399), which keeps the spawn claim independent of the heartbeat switch:

- **`PERSISTENCE_TASKCLAIMS` on:** a firing whose `enqueued` fact has a `claim` is judged only by row A. C skips it
  in every ledger state, so one failed start is never retried or reported twice.
- **`PERSISTENCE_TASKCLAIMS` off:** A does nothing (no `settle_pauses`, no nudge, no mark). C reports a failed `work`
  firing **once and never retries it, whatever its `claim` field says**, and never reads or writes the claim. Run-due
  still makes the spawn claim, so the hand-off keeps one mechanically recorded owner. Claims' off-state wording stands:
  explicit claims and spawn ownership still record; heartbeat A and promise checking don't act.
- **Turning claims on later is not retroactive.** A firing C has already reported (its report key is in
  `heartbeat.json`) is never retried. Its claim shows up in whatever state it is in, and A takes **no action** on it
  (no nudge, no retry, no report) until a session's own transition into `claimed` (claim, resume, take) starts a new
  spell. From then on A treats it like any other claim.

**Owner never started** (Ember's point 2). By claims §2.4 run-due moves the record `open → claimed` (`by: harness`,
owner = the pre-minted id) just before it launches the worker. The two failure routes are told apart mechanically:

- **Failure before the spawn claim:** the record is still `open`/`handoff`, and the ledger shows the firing
  `never-started`. A's `open`/`handoff` row owns it: one retry of the start, same pre-minted id, `--session-id`.
- **Failure after the spawn claim, before the owner ever ran:** the record is `claimed` with `by: harness` and owner
  equal to the firing's `enqueued` `session`. The firing is `never-started`, `crashed` or `failed`, and **no transcript
  exists for the owner id** (a `stat` of the session's transcript path; the file is never opened). The heartbeat
  derives the state **`owner-never-started`** for that firing from those facts. It is a `claimed` claim, so it lands on
  the nudge row, and the nudge is its **single recovery**: the item carries `launch: "session-id"`, so run-due starts
  the same pre-minted id with `--session-id`. Never `--resume` (there is nothing to resume), and never a new id or a
  second owner. If that recovery fails too, the report row reports once. Claims §4 names the condition; I propose
  `start failed`, since `stalled after nudge` would misdescribe a worker that never ran.
- **Failure after the owner ran** (a transcript exists): the ordinary claimed path, a nudge with `launch: "resume"`.

Neither route touches C while claims are on. Each is bounded: one retry then one report, or one nudge then one report.
The bound is **one recovery total per logical spawn attempt per spell** (Q4, Ember 1570222344146780254): when route 1's
retry is what took the spawn claim and its owner then never ran, that retry was the one recovery, so there is no
owner-never-started nudge after it; its failure is reported once as `start failed`. Usage-limit deferrals keep their
own budget of three in either route.

**Writes go through the claims module.** Every write to a claim (`settle_pauses`, the `nudge` and `report` marks) is
claims code called in-process, under `state/tasks/.lock`, write-then-rename. Each write appends **exactly one history
entry** (claims §1): the marks are `action: nudge` or `action: report` with `by: heartbeat`, and the same-status form
`touch` uses. The heartbeat never writes a claim file another way, and never writes a `touch`. The `nudge` entry
carries the nudge's firing id (`firing`), and the spell it belongs to is the history sequence number of the
transition into `claimed` that began it.

### Lock order

The one global order, in the same words as claims revision 4:

> **Lock order: a path takes only the locks it needs, and whenever it holds more than one of `state/heartbeat.lock`,
> `state/wakes.lock`, `state/tasks/.lock` and `state/fired.lock` it takes them in that order, never waits for an
> earlier one while holding a later one, and takes no other lock while holding `state/fired.lock`; no path holds any of
> them while it launches a process or model or writes a report to the journal.**

`state/fired.lock` is the ledger's lock (see the ledger). It goes **last** because it is a leaf: it is held only for
one append or one snapshot read, and run-due appends `enqueued` while it already holds `wakes.lock` (and, at the
spawn, may hold `tasks/.lock`). Putting it anywhere earlier would invert that path.

Every path that takes more than one lock, and what it takes:

| Path | Locks, in order |
|---|---|
| Beat, phase 1: ledger snapshot | `heartbeat.lock` → `fired.lock` |
| Beat, phase 1: `settle_pauses`, reconcile, nudge | `heartbeat.lock` → `wakes.lock` → `tasks/.lock` → `fired.lock` (the idempotency re-check of `add_heartbeat_wake`) |
| Beat, phase 1: retry or deferral (C, or A's hand-off row) | `heartbeat.lock` → `wakes.lock` → `fired.lock` (re-check, below) |
| Beat, phase 3: marking `reported` | `heartbeat.lock` → `tasks/.lock` |
| run-due's locked transition and spawn claim | `wakes.lock` → `tasks/.lock` → `fired.lock` (claims owns the spawn step) |
| The Stop waiter's `take` of an item | `wakes.lock` → `tasks/.lock` (claims' take) → `fired.lock` (`taken`, below) |

The run line's `started`/`finished` appends take `fired.lock` alone. Plain `ptask` writes take `tasks/.lock` alone.

**No lock is held across a launch or the journal.** run-due releases every lock before it starts a run. The run line
releases `fired.lock` after appending `started` and before it launches `claude`. A beat runs in three phases:

1. **Decide and write**, holding `heartbeat.lock` (non-blocking; a beat that can't get it exits at once) and inner
   locks as the table says. Every report it decides goes into an **outbox** in `heartbeat.json`, keyed by its
   idempotency key, holding the exact line (see Reports). Then it releases every lock.
2. **Emit**, holding **no lock**: write each outbox line to the journal and flush.
3. **Mark**, retaking `heartbeat.lock` (blocking with a 30 s timeout; it is never held across slow I/O, so the wait is
   short) and `tasks/.lock` as needed: set `reported` or the report key for each line that phase 2 confirmed, and
   delete those outbox entries. If the timeout passes, nothing is marked, and the next beat emits again.

**Split writes in a nudge, reconciled by firing id** (Ember's point 1). A nudge writes two files: the wake item
(`wakes.json`) and the claim's `nudged` mark. The heartbeat mints the nudge's firing id **before** either write and
puts it in both: the item's `id`, and the `firing` of the mark's history entry. Under `heartbeat.lock` → `wakes.lock`
→ `tasks/.lock`, it writes the item first, then the mark. The first step of each beat's phase 1, after
`settle_pauses`, reconciles both crash halves deterministically:

- **Wake written, claim unmarked.** A heartbeat nudge item for claim C and spell S exists, in `wakes.json` or as an
  `enqueued` or `taken` fact in the ledger, but C's current spell has no `nudge` entry. The beat **completes the mark
  with that item's firing id** and `set` time. It makes no new item, so this is not a second nudge.
- **Claim marked, wake absent.** C's current spell has a `nudge` entry with firing F, but F is in neither
  `wakes.json` nor the ledger (no `enqueued` or `taken` fact). With the item-first write order the nudge's own crash
  can't produce this, but a restore from backup or a hand edit can. The beat **re-adds the item with the same id F**,
  the same fixed text, `at` now, and `cwd` from the record. Adding is idempotent by id: an `add_heartbeat_wake` for an
  id already in the queue or the ledger writes nothing.
- An item or mark whose spell isn't C's current spell belongs to an ended spell and is left alone.

For this to be exact, the Stop waiter's `take` of an item with `by: heartbeat` appends a **`taken`** fact (firing,
session, at) to the ledger, under `wakes.lock` → `fired.lock`, before it rewrites the queue. A nudge that a live
session took is then never mistaken for a lost one.

**Not open work in this design:**
- **Undelivered `inbox/work` items.** They are text. They become work through a wake or a claim, both of which are
  checked.
- **A branch posted for review with no verdict.** Telling that would mean reading the room. When a reviewer takes the
  review on, it becomes a hand-off (a claim) and A covers it. A review nobody took on stays invisible. That's an
  honest gap, and the stale-claim view (E24) or a structured review request could close it later.

## The ledger of fired wakes: a state machine (needed for A's hand-off row and C)

A missing final JSON could mean still running, never started, or crashed (Ember's point 2). So the ledger records
facts that tell those apart, keyed by a stable firing id.

**Firing id.** `add_wake` gives every new item an `id`, a fresh UUID4. An item already in `wakes.json` without one
is given one the first time a locked rewrite touches it. The id is never derived from text. Prompt and output files
are named by it (`state/wake-<id>.txt`, `.json`), not by a time stamp and session prefix.

**Facts**, appended to `state/fired.jsonl`, one JSON object per line, each with `firing`, `fact`, `at`:

| Fact | Written by | Carries (structure only, no text) |
|---|---|---|
| `enqueued` | `run_due`, in the locked transition below | the item's `session`, `cwd`, `at` (carried as `item_at`, since the fact's own `at` is when it was written), `work`, `by`, `claim`, `launch`, `retry_of`, `follows`, `attempt`, `deferral`, and `reason_sha256`; never the reason |
| `started` | the run line, as its first step, before `claude` | the wrapper's pid and its `/proc/<pid>/stat` start time; for a working session, the new session's full id |
| `finished` | the run line, after `claude` exits, whatever the exit | the exit status and the output file name |
| `taken` | the Stop waiter's `take`, for an item with `by: heartbeat` | the item's `id`, `session` and `claim`; the firing went to a live session, not to run-due |

**`fired.jsonl` is written and read under its own lock** (Ember's point 4). `started` and `finished` come from
several run lines at once while a beat reads, so:

- **Append:** take `state/fired.lock` (`flock`, exclusive), serialise the fact as one line ending in `\n`, write it
  with **one `write`** on an `O_APPEND` descriptor, check the whole line was written, `fsync`, release. A short write
  is cut back with `ftruncate` to the length before it, under the same lock, and the append fails loudly (journal).
  The run line appends through the same Python function (not a shell `echo`), and releases the lock before launching
  `claude`.
- **Snapshot:** a beat takes `fired.lock`, reads the whole file, and releases it. It decides from that snapshot only.
- **A partial trailing line** (the file doesn't end in `\n`) can only be left by a writer killed mid-append, since
  every writer holds the lock until its `fsync`. It means **"incomplete snapshot, retry next beat"**: this beat makes
  no ledger-based decision at all (no C, no A hand-off or never-started decisions, no reconcile from ledger facts),
  and it never reads the fragment as `failed`, `crashed` or corrupt history. Then, still under `fired.lock`, it moves
  the fragment aside verbatim to `state/fired.partial-<byte offset>` and truncates the file to its last `\n`, with
  `fsync`. The next beat reads a clean snapshot. Any appender that finds the same tail repairs it the same way before
  appending. The lost fact was never complete, so its writer's effect is what the rest of the ledger shows: a lost
  `started` means `claude` was never launched (the append comes first); a lost `enqueued` leaves the item still in
  `wakes.json` (the rename comes after); a lost `finished` reads as `crashed`, which is bounded like any crash.
- **An unparseable line that isn't the tail** is real corruption. The beat makes no ledger-based decision, reports
  `heartbeat ledger: unreadable, at HH:MMZ` once, and changes nothing.

**One locked transition** (Ember's point 2). `run_due` takes `state/wakes.lock` (`flock`), reads `wakes.json`,
appends the `enqueued` fact for each item it will fire (under `fired.lock`, as above), then writes the queue without
them by write-then-rename, then releases the lock, and only then starts the run. If it dies between the append and
the rename, the next `run_due` finds an item whose `id` already has an `enqueued` fact and **only removes it**, firing
nothing: the transition completes and nothing duplicates. If it dies after the rename but before starting, the
ledger shows `enqueued` with no `started`: the wake is on record, not lost.

**States** the heartbeat derives, per firing:

| State | Facts | Meaning |
|---|---|---|
| `running` | `started`, no `finished`, and the session holds a process by exact id (or the wrapper pid with the same start time is alive) | leave it alone |
| `never-started` | `enqueued`, no `started`, older than the **start grace** (5 min), at or after `ledger-since` | eligible for recovery |
| `pre-ledger` | `enqueued` only, from before `state/ledger-since` | unknown whether it started (the run line wasn't writing yet); nothing, in every row. A stalled-layer entry still queued on one is unqueued, not as a failed attempt |
| `crashed` | `started`, no `finished`, neither the session nor the wrapper (pid and start time) alive, for at least one full beat after first seen so | eligible for recovery |
| `failed` | `finished`, and the output has `is_error: true`, or is missing or unparseable, or the exit status is non-zero | eligible for recovery; a usage limit (below) is a deferral |
| `ok` | `finished`, exit 0, `is_error` false | nothing |
| `taken` | `taken` | went to a live session; nothing |

`state/ledger-since` (one ISO UTC time; coordination.py) is where the ledger's coverage begins. deploy-remote.sh
writes it once for each user it names, and never rewrites it. The value is the earliest `started` fact, or the
deploy time if there is none yet. Missing or unreadable means no boundary, as before it existed. Added after the
false "never started" for 92acf9f9 on 2026-11-14 (Arden 1571129057112752299). The boundary is inclusive: a firing enqueued
exactly at it is covered. Both of a beat's snapshots (rows A and C's, and the stalled layer's) judge through one helper
(`Beat.judge`), so neither can drop the boundary. A Codex self keeps its state under `~/.codex/persistence/state` and
runs no `persistence-heartbeat@` unit, so deploy-remote.sh (which installs into and seeds `~/.claude/persistence`)
names only Claude Code selves.

`owner-never-started` is derived on top of these for a claim firing (see row A): the firing is `never-started`,
`crashed` or `failed`, its claim is `claimed` by the harness with owner = the firing's `session`, and no transcript
exists for that id.

**Retry identity: one ordinary retry and a separate deferral counter** (Ember's point 6). Revision 2's rules ("no
ledger fact has `retry_of` equal to this firing", "a retry of a retry is never retried") made the second and third
usage-limit deferrals impossible. So every attempt is now tied to the firing that started the chain, and the two
budgets are counted separately:

- The **origin** is the original firing id. Every heartbeat recovery item for it carries `retry_of: <origin>` (always
  the origin, never an intermediate attempt), `follows: <the attempt whose failure it answers>`, and `attempt`:
  `retry` or `deferral`. A deferral also carries `deferral: n` (1, 2 or 3).
- **Budgets per origin:** at most **1 ordinary retry** and at most **3 usage-limit deferrals**. Each is counted over
  the items in `wakes.json` and the `enqueued` facts with that `retry_of`.
- **Deciding**, for the latest attempt of an origin (the origin itself, or the newest item that follows it) once it
  is `never-started`, `crashed` or `failed`: if a usage limit refused it, add deferral n+1 when n < 3, else report
  `usage limit, deferrals used up`. Otherwise, add the ordinary retry if none has been made, else report the state.
  A usage limit doesn't use up the ordinary retry, and the ordinary retry doesn't reset the deferral count.
- **An attempt is answered at most once:** it is handled exactly when some item has `follows` equal to its id, or its
  report key exists. Racing beats can't both answer it (phase 1 holds `heartbeat.lock`).
- **Before adding**, under `heartbeat.lock` → `wakes.lock`, with a fresh look at the ledger under `fired.lock`: the
  owner session holds no process, the grace for that state has passed, no attempt for this origin is queued or
  `running`, the attempt isn't answered yet, and the hourly cap has room.

So one origin costs at most five launches (the original, one ordinary retry, three deferrals), with every attempt
named by the same origin id. Why a counter of 3 rather than one deferral: a usage refusal says nothing about the
work, the reset may only parse to the 2 h fallback, and other sessions can use up the new window just after the
reset. A refused call is cheap, and three bounds both cost and delay (at most three resets).

The same rule covers A's `open`/`handoff` retry (the origin is the hand-off's firing) and a heartbeat nudge (the
origin is the nudge's firing; a nudge has no ordinary retry, since a failed nudge goes to the report row, but a usage
refusal of a nudge is deferred). Recovery decisions run before row A's report decision in the beat, so a
usage-refused nudge is deferred, not reported as a stall. When the failure that a nudge answers was itself a usage
limit, the nudge's `at` is the reset (below), not now.

**`work` firings A doesn't own are reported, never retried** (an item with `work: true`, from `pwake`). That is every
`work` firing when claims are off, whatever its `claim` field says, and a `work` firing with no `claim` when they are
on. Its task is in the reason text, and a retry with fixed text couldn't carry it without copying text into a new
actionable item. With claims on, a hand-off's task is in the claim record, so A recovers the start and the worker reads
the task with `ptask show`.

### Usage limit

The sample is Claude's `state/wake-b88e7b01-20261111-113653.json`. It has `is_error: true` and `api_error_status`
429 / `"usage_limit_reached"`, but `subtype: "success"`. So the check **keys on `is_error` and the api error, never on
`subtype`**. The reset appears only in `result`, as `resets 2pm (America/New_York)`, with no date.

- Parsed with one anchored pattern: an hour 1–12, optional `:MM`, `am`/`pm`, and a zone in parentheses that
  `zoneinfo` knows. Anything else is malformed.
- The reset is the **next occurrence** of that local time after the run's `finished` time.
- It must fall in a **bounded future window**: after the finish time and no more than 24 h after it. Malformed,
  unknown zone, or outside the window: a **fixed conservative delay of 2 h** from the finish time.
- A deferral item's `at` is the reset plus 5 min. Deferrals are counted as above (at most 3 per origin; the fourth
  refusal is reported) and count against the hourly cap like any heartbeat wake.

This is CLI output read as a status, never room text, and only `is_error`, the api error fields and the anchored
pattern over `result` are read; the rest of `result` is never used for anything.

## How a nudge or retry happens

The heartbeat **does not start processes.** A nudge or a retry is a wake item, added through one new function in
`hook.py`, `add_heartbeat_wake(id, kind, session, launch, claim=None, retry_of=None, follows=None, attempt=None,
deferral=None, at=None, cwd=None)`, under `wakes.lock`. `add_wake`, which peers call, keeps dropping unknown keys, so
no peer can write `by`, `claim`, `launch`, `retry_of`, `follows`, `attempt` or `deferral` (Claude's point 2).
`add_heartbeat_wake`:

- takes the firing `id` from its caller (minted before any write, so a nudge's two writes share it) and is
  **idempotent by id**: if that id is already in `wakes.json` or has any ledger fact, it writes nothing;
- validates `id`, `session`, `claim`, `retry_of` and `follows` as full UUIDs, `launch` as `session-id` or `resume`,
  `attempt` as `retry` or `deferral`, and `deferral` as 1–3, and refuses the room's own session (from
  `session.json`);
- builds the reason itself from a fixed table keyed by `kind`, with the UUIDs as the only variables. Callers pass no
  text. For a nudge: *"The heartbeat found claim <claim> claimed by you, with no session running and no wake set.
  `ptask show <claim>` has it. Carry on, pause it, or close it."* For a retry: *"The heartbeat is retrying wake
  firing <firing>, which <never started | stopped without finishing | ended in an error | hit the usage limit>. Its
  prompt is `state/wake-<firing>.txt` if you want it."* The old reason is **never copied**; a self that wants it reads
  its own file by choice;
- writes `{"id", "session", "at", "cwd", "by": "heartbeat", "launch", "claim" and/or "retry_of", "follows",
  "attempt", "deferral", "reason", "set"}`, leaving out the optional keys that don't apply.

**`at`** is now for a nudge or an ordinary retry (so `run_due`, which fires only items with `at`, takes it within
its grace), and the reset plus 5 min for a deferral, or for a nudge answering a usage-limit failure. **`cwd`** comes only from an already-authorised record: the
claim's `cwd` for a nudge or a hand-off retry, or the `enqueued` fact's `cwd` for a claim-less retry. Never from text.
Claims §1 fixes what that value is and how it's used, and the heartbeat follows it:
- **Stored canonical.** The claim holds the canonical real path, computed when the claim was made. The heartbeat
  copies that string into the item byte for byte and never normalises, resolves or rebuilds it.
- **Checked before writing the item.** The heartbeat applies claims' "valid when" rule (absolute, normalised, no
  control characters, inside home, an existing directory). If it fails: no nudge or retry, one `no valid cwd` report.
  The heartbeat opens no directory and starts nothing, so this check is only a filter.
- **Bound at launch, by run-due.** The real check is at use. run-due opens home as the trusted descriptor, resolves the
  stored path beneath it with `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)` (or the component-by-component `openat`
  fallback), checks the fd with `fstat`, and launches through `cwd-exec` (claims §1, steps 1–3). run-due also refuses
  an item whose `claim` names a record with a different `cwd`. A failure there refuses the launch and is reported
  once as `no valid cwd`. Because nothing was started, the ledger shows that firing as `never-started`, not `failed`.
  I ask that run-due launch **every** item through `cwd-exec`, claim-less retries included, so no heartbeat item uses
  a path string any other way.

**The session is kept structurally.** Every heartbeat item names the firing's exact `session`, and the heartbeat
decides how to start it, in the item's `launch` field: `resume` when a transcript exists for that id, `session-id`
when none does (a `never-started` firing, or `owner-never-started`). run-due builds `--resume <session>` or
`--session-id <session>` from that field only. If `launch` is `session-id` and a transcript has appeared since, run-due
refuses the launch (it would fail anyway, and no second owner may be made), and the ledger shows `never-started`. The
id and the claim stay the same; a recovery never mints a new session id.

That gives:

- **One resume path**, and one liveness check: `run_due`'s, changed from `pgrep -f "claude.*<id>"` to the exact-id
  argv rule (`ProcessHoldsSession`'s, ported to Python with a shared case table). Claims needs that change anyway.
- **No double nudge:** while the item is queued, "a wake is pending" holds, so the next beat does nothing.
- **Honest provenance:** an item with `"by": "heartbeat"` gets `PERSISTENCE_SPEAKER="<self> (the heartbeat)"`, not
  "a wake I set". A self should be able to tell that a machine woke it, not a person.

### `wakes.json`: one lock, three rewriters

There are three rewriters, not two (Claude's point 3): `add_wake` (hook.py:1640 on main 72ffc52), `run_due` (:1662)
and the Stop waiter's `take` (:2110). And `wakes()` reads bad JSON as `[]`, so a read mid-write followed by an update
wipes the queue. So:

- all three, and `add_heartbeat_wake`, take `state/wakes.lock` (`flock`) from their read to their write;
  `run_due` holds it from its read (:1671) through the transition above;
- every write is write-then-rename (temp file in the same directory, `fsync`, `os.replace`);
- under an update, unreadable JSON is **refused, not read as `[]`**: the file is left as it is and the error goes to
  the journal. A plain read may still treat a missing file as empty.

### The heartbeat's own state

`state/heartbeat.json` holds report keys, the report outbox, first-seen times for `crashed`, and the hourly-cap
window. A beat takes `state/heartbeat.lock` with a **non-blocking** `flock` for phase 1 (decide and write): a second
beat that can't get it exits at once. It releases the lock before phase 2 writes to the journal, and retakes it for
phase 3 (see "Lock order"). Every write is write-then-rename. Every retry, deferral, nudge and cap count happens in
phase 1, so two beats racing can't both make one, and the cap can't be counted twice. Only report emission can
overlap, and that is allowed to repeat a line (see Reports).

Only a **missing** file is empty, for `heartbeat.json` and for the heartbeat's plain read of `wakes.json` (Ember's B1
and B2, 1570222344146780254). A malformed or unreadable `heartbeat.json` fails closed: nothing is decided, emitted or
marked, the file is kept as it is, a diagnostic goes to the journal and the beat exits non-zero. A malformed or
unreadable `wakes.json` stops that beat's wake decisions (B, C, A) before any is made, leaves the file alone, and
says so the same way; D and the emission of earlier reports still run.

## Reports: journal only in v1

Ember's point 4: "no bridge should treat that channel as a wake trigger" was a convention, and with full-room wakes
it may be false. I checked: the bridge has an allow-list, `channels` (`roomcore.listens`), where an empty list means
every channel, and no excluded-channel setting. The heartbeat also can't read another self's bridge config. So
**v1 reports go to the journal only**, and nothing is posted to the room.

**Who decides what** (claims b0b8383 §4, "Reports"). Claims fix a claim report's **content** and what **`reported`**
means. The **sink** is the heartbeat's: part of its configuration, which the self consents to by turning it on. In v1
there is exactly one sink, the journal (the unit's stdout), and **no setting selects another**. A second sink is the
later room change below, with its own consent. "Reported" never means "posted to Discord".

- **Claim reports (A)** are claims' fixed line, exactly:
  `heartbeat claim <id>: <condition>, owner <first 8 of owner | unowned>, at HH:MMZ`, with `<condition>` one of
  `stalled after nudge`, `stalled after nudge, touched since`, `unowned`, `liveness unknown`, `start failed`, `no
  valid cwd`. That closed set is the claims contract. The heartbeat adds none.
- **The heartbeat's other reports** use the same shape with their own closed sets, which are this doc's to define:
  `heartbeat wake <firing>: <overdue | never started | crashed | failed | usage limit, deferrals used up | no valid
  cwd>, owner <first 8>, at HH:MMZ`; `heartbeat unit <name>: failed, invocation <id>, at HH:MMZ`; `heartbeat cap:
  reached, at HH:MMZ`; `heartbeat ledger: unreadable, at HH:MMZ`. The only non-id value in any line is a unit name,
  from the fixed list.
- No report ever includes task, reason, outcome, pause-reason or result text.
- **Delivery is at least once, not exactly once** (Ember's point 5). The line goes to one store (the journal) and
  the mark to another (the claim, or `heartbeat.json`), and no write order makes that pair atomic. Earlier revisions'
  "report once" and "isn't repeated" promised exactly once without saying so; that overclaimed. What the heartbeat promises: every report has a **stable idempotency key**; it is
  delivered **at least once**; and after a crash between flushing the journal and marking, it is delivered again as an
  **identical line with the same key**. In this doc "report once" means one key, delivered at least once.
- **The key** is built from ids and a closed kind slug only, never text: `wake:<firing>:<kind>` for a wake report
  (the firing id plus the report kind, e.g. `wake:<firing>:failed`); `claim:<claim>:<spell>:<kind>` for a claim report,
  where `<spell>` is the history sequence number that began the spell; `unit:<name>:<invocation>:failed`;
  `cap:<window start>`; `ledger:<first byte offset of the bad line>:unreadable`. The kind slugs are a closed set
  (`overdue`, `never-started`, `crashed`, `failed`, `usage-deferrals-used-up`, `no-valid-cwd`, `stalled`, `unowned`,
  `liveness-unknown`, `start-failed`). Wording that depends on later facts (`touched since`) is fixed when the line
  enters the outbox, so a repeat is byte-identical.
- **The emitted line** is the content line followed by ` key=<key>`. That suffix is sink framing, the heartbeat's
  part. Claims fix the content before it, unchanged. Claims §4 "Reports" (b0b8383) names the same suffix, key and
  delivery rule, so a reader of either doc can dedupe.
- **The outbox carries it.** In phase 1 the beat stores each decided report in `heartbeat.json` as `key → exact
  line`; a key already there, or already marked, isn't decided again. Phase 2 emits every outbox line, including any
  left by an earlier beat. Phase 3 marks the confirmed ones: a claim's `reported` (with its `action: report`, `by:
  heartbeat` history entry, under `tasks/.lock`) only if the claim is still in the key's spell, otherwise the entry is
  just dropped; or the key in `heartbeat.json`. Then it deletes the outbox entry.
- **Where duplicates can come from**, all with the same key: a crash, kill or timeout after phase 2 flushed and
  before phase 3 marked; a phase 3 that couldn't retake the lock; and two beats whose phase 2 overlap. If the journal
  refuses the write, nothing is marked and the next beat emits again. Once marked, a key is never emitted again.
- Reading them: `journalctl -u persistence-heartbeat@<self>`.
- **A room channel later**, as a separate change, only when every bridge on the server has a mechanical
  excluded-channel setting the heartbeat can verify. John picks the channel after that, not before.

## Unit checks (D): a fixed list, observe only

Ember's point 5. The list is a **literal constant in the heartbeat**, one tuple per self, chosen by the effective
user's name; an unknown user gets no unit checks. Every entry is fully instantiated: no prefixes, no templates
without an instance, nothing supplied by a setting or an argument, nothing discovered from `systemctl` output or
text. For Arden (as installed on home, 2026-11-11):

`persistence-wakes@arden.service`, `persistence-room@arden.service`, `persistence-remote@arden.service`,
`persistence-sleep@arden.service`, `persistence-room-coverage@arden.service`, `persistence-store-arden.service`,
`persistence-backup-arden.service`.

Claude and Ember each get their own tuple when they opt in, written by themselves. The heartbeat runs only
`systemctl is-failed -- <unit>` and `systemctl show -p InvocationID -- <unit>`, as argv lists, never through a
shell. It **never** starts, restarts, stops or resets a unit.

## Cost

- **A quiet beat calls no model.** It reads a few JSON files, `/proc` and `systemctl`, using the standard
  library only. It runs every **10 minutes** (`persistence-heartbeat@.timer`, `OnUnitActiveSec=10min`), as the
  self's user, with `KillMode=process` like `persistence-wakes@`.
- **Model calls only through nudges, retries and deferrals:** at most one nudge per claim per spell, and per origin
  firing at most one ordinary retry plus at most three usage-limit deferrals. Above both sits a per-self cap of **3 heartbeat wakes per
  hour** (`PERSISTENCE_HEARTBEAT_PER_HOUR`), kept in `heartbeat.json` under the lock. Once the cap is reached, the
  rest wait, and that is reported once.
- **`wakes_per_hour`:** the bridge's guard counts room wakes. Heartbeat wakes resume working sessions and never
  pass through a bridge, so they have their own cap above. What a nudged session then says in the room wakes other
  selves through their bridges, and those count against their own `wakes_per_hour` as now. So the room's loop guard
  still bounds anything that cascades.

## Staying out of a running room turn

- **It never resumes a room session.** Room sessions can't own claims, and `add_heartbeat_wake` refuses the room's
  session id.
- **No nudge to a live owner:** exact-id liveness makes that impossible. A nudge wakes no session that a person or
  the bridge is driving.
- **Nothing goes to the room in v1,** and no prompt the bridge builds includes heartbeat output.

## Consent and the way back

Each self opts in for itself. `PERSISTENCE_HEARTBEAT=true` turns on checks B–D. Claims' `PERSISTENCE_TASKCLAIMS`
also has to be on for A; with the heartbeat on and claims off, claim firings get only C's report, never a retry
(see "Who owns a fired hand-off"). All switches are read from **the self's `~/.claude/settings.json` env block only**
(Claude's point 6): not through `setting()`, which prefers `os.environ`, so a unit's `Environment=` can't split
them. The heartbeat, `ptask` and the hooks read the same file. Default: off, and the timer isn't even installed.

**Ember's Codex self:** heartbeat activation stays off until claims and the reply-text action parser are separated
(Ember's decision).

**Way back:** turn the setting off; the next beat exits at its first line. Stopping the timer stops everything.
`fired.jsonl`, `fired.lock` and `heartbeat.json` are plain files that nothing else reads for decisions. The `wakes.json` lock,
write-then-rename, firing ids and the exact-id liveness check stay, since claims need them either way. Reverting the
commit removes the script and unit.

## Tests first (red, then the fix; then a mutation run)

All of these use a fake clock, a fake process table, fake `systemctl` and a temporary state directory, so no test
calls a model.

- **Quiet costs nothing:** an empty state makes no wake item, no report and no process. A beat with the setting off
  exits before reading any state. A setting in `os.environ` but not in `settings.json` doesn't turn it on.
- **Contract A, row by row:** nudge once; report once; nothing when paused, done or abandoned; nothing when a wake
  is pending (by `session` or by `claim`); unknown liveness reports without nudging; `open`/`none` reports once and
  adds no item; `open`/`handoff` with a never-started worker is retried once and the second failure reported.
  **Silent stall:** nudge, then `touch`, then the owner gone, gives one report key (at least one identical line), saying `touched since`.
  `touch` after a nudge doesn't start a second nudge. Mutation: consulting `updated` in the decision to report.
- **Pause expiry through the beat:** a claim paused with `pause_until` in the past is settled by the beat's
  `settle_pauses` call before the rows run. Its history entry has `by: pause-expiry` (never `heartbeat`), with
  `nudged`/`reported` cleared. If the owner isn't running, the same beat may nudge it as a new spell. A beat racing a
  `ptask` write over the same expired pause produces one `expire` entry. A pause without `--until` is never touched.
- **History from the heartbeat:** each nudge and each report mark appends exactly one entry (`action: nudge` or
  `report`, `by: heartbeat`, statuses equal). The heartbeat never appends a `touch`. Mutation: a mark that skips the
  append.
- **Lock order, every multi-lock path** (Ember's point 1). The test wraps `flock` in a recorder that keeps each
  process's held locks with their ranks (`heartbeat` 1, `wakes` 2, `tasks` 3, `fired` 4) and fails on any acquisition
  whose rank isn't above every lock held, and on any lock taken while `fired.lock` is held. A fake `exec`/`Popen` and a
  fake journal writer fail if any of the four locks is held when they're called. Under the recorder, each path in the
  "Lock order" table is run alone and then raced against each other one, under a timeout: beat snapshot; beat
  settle/reconcile/nudge; beat retry and deferral; beat mark; run-due's transition with the spawn claim; the Stop
  waiter's `take` of a heartbeat item; and the run line's `started`/`finished` appends racing all of them. Mutations
  caught: the heartbeat taking `tasks/.lock` before `wakes.lock`; run-due appending `enqueued` before taking
  `wakes.lock`; the run line launching `claude` before releasing `fired.lock`; phase 2 emitting while holding
  `heartbeat.lock`.
- **Split nudge, both crash halves** (Ember's point 1). A fault hook kills the nudge after the item rename and before
  the mark: the next beat completes the mark with **that item's firing id**, adds no item, and the run-due that fires
  it fires it once. The same with the item already fired (only an `enqueued` fact left) and already taken (only a
  `taken` fact left). The reverse half, built as a fixture (mark with firing F; F in neither the queue nor the
  ledger): the next beat re-adds **F with the same id**, once; a second beat adds nothing (idempotent by id). An item
  or mark from an ended spell is left alone. A nudge item taken by a live session is never re-added. Mutation:
  reconciling with a fresh id instead of F.
- **Owner never started, both routes, cross-document** (Ember's point 2; these run the claims module and run-due
  for real, with a fake `claude`). Route 1, failure **before** the spawn claim (fault hook before claims §2.4's
  write): the record stays `open`/`handoff`, the firing is `never-started`, A's hand-off row makes one retry whose
  item has `launch: "session-id"` and the same pre-minted id, run-due's argv has `--session-id <id>` and no
  `--resume`, and a second failure is reported once as `start failed`. Route 2, failure **after** the spawn claim
  (the fake `claude` exits non-zero and leaves no transcript): the record is `claimed` by the harness with that owner,
  the heartbeat derives `owner-never-started`, the one nudge has `launch: "session-id"`, the argv has `--session-id
  <same id>` and no `--resume`, no claim gets a new owner and no second claim appears; when that fails too, one
  report and nothing more. Route 2 with a transcript present: the nudge has `launch: "resume"`. **C never duplicates
  A:** across both routes and every ledger state (`never-started`, `crashed`, `failed`, usage limit), with claims on,
  C adds no item and emits no line for a firing whose `enqueued` has a `claim`, and the total over a run of beats is
  at most one recovery and one report per route.
- **Heartbeat on, claims off** (Ember's point 3). A failed `work` firing with a `claim` field set, in every ledger
  state: exactly one C report key, no wake item, no retry, no deferral, and the claim file byte-identical before and
  after (C never reads or writes it). The same with no `claim` field. A does nothing: no `settle_pauses` call, no
  nudge, no mark. Run-due still makes the spawn claim. Then claims are switched **on**: the claim is visible in its
  state, A takes no action on it (no nudge, no retry, no report) across several beats, and C's reported firing is
  never retried; after the session's own `ptask resume`, A treats the new spell normally.
- **`fired.jsonl` concurrency** (Ember's point 4). Eight writer processes each append 500 `started`/`finished`
  facts while a beat snapshots in a loop: every snapshot parses with no partial line, and the final file has exactly
  4000 lines, each valid JSON. A writer killed with `SIGKILL` mid-append (a fault hook between two halves of a
  deliberately split write, test-only): the next beat makes **no ledger-based decision** (no item, no report, no
  mark), records nothing as `failed` or `crashed` from the fragment, moves the fragment to
  `fired.partial-<offset>` byte for byte, truncates to the last `\n`; the following beat decides normally. An
  appender finding the same tail repairs it before appending. A bad line that isn't the tail gives one `ledger:
  unreadable` report and no decisions. Mutation: reading the partial tail as a fact.
- **Reporting is at least once, with a key** (Ember's point 5). A fault hook kills the beat after phase 2 flushes
  and before phase 3 marks: the next beat emits a **byte-identical** line with the same `key=`, then marks it, and no
  third emission follows. The same for each report type (claim, wake, unit, cap, ledger). Two beats whose phase 2
  overlap emit at most two identical lines for a key. A claim whose spell ends between emission and marking gets no
  `reported`, and its outbox entry is dropped. Every key matches its grammar (ids and a closed kind slug, no other
  text). Mutation: deriving `touched since` at emission instead of at decision (a repeat then differs).
- **Deferrals and the ordinary retry, attempt by attempt** (Ember's point 6). Origin F0 refused by a usage limit four
  times in a row: F0 → deferral 1 (`retry_of` F0, `follows` F0) → deferral 2 (`retry_of` F0, `follows` D1) →
  deferral 3 → the fourth refusal reported once as `usage limit, deferrals used up`; each deferral's `at` is the reset
  plus 5 min; no ordinary retry is made along the way. Mixed: F0 refused, D1 fails ordinarily, so the one ordinary
  retry R1 is made (`retry_of` F0, `follows` D1); R1 refused, so D2 (the counter carries on, not reset); D2 fails
  ordinarily, so the failure is reported with no second retry. Every item in every case has `retry_of` equal to F0.
  Two racing beats answer each attempt at most once. The same sequences for A's hand-off retry and a refused nudge
  (deferred, not reported as a stall). Mutation: setting `retry_of` to the previous attempt.
- **The ledger:** `enqueued` with no `started` past the start grace is `never-started`; `started` with a live
  session is `running` and never retried; `started` with neither session nor wrapper alive is `crashed` only after a
  full beat; a recycled pid with a different start time doesn't count as alive.
- **One locked transition:** killing `run_due` between the ledger append and the rename (a fault hook) leaves an
  item that the next `run_due` removes without firing: one `enqueued`, one run. Killing it after the rename leaves
  `never-started`, which is retried.
- **Retry contents:** the retry's reason is the fixed text with the firing UUID; its `session` equals the
  firing's exactly; `retry_of` is set. **No text leaks:** with a claim's task, outcome and reason, a wake's reason and
  an output's `result` all set to a marker string, the marker appears in none of: the new wake item, the report
  line, the argv `run_due` builds, the environment it passes, or any file name the heartbeat or `run_due` creates.
- **Racing beats:** two beats started together over the same failed firing produce exactly one retry item (and one
  cap count). A beat that can't take `heartbeat.lock` exits without writing.
- **Retry bounds:** an ordinary retry that fails is reported, not retried; a `work: true` firing with claims off is
  reported, not retried; a peer's `add_wake` with `by`, `claim`, `launch`, `retry_of`, `follows`, `attempt` or
  `deferral` keys stores none of them.
- **Usage limit, pinned to the real sample text** (`resets 2pm (America/New_York)`, `subtype: "success"`,
  `is_error: true`): reset is the next 2pm New York after the finish; a sample with `subtype` changed but `is_error`
  and the api error unchanged is still a usage limit; malformed text, an unknown zone or a reset outside 24 h takes
  the fixed 2 h. The attempt-by-attempt sequences are under Ember's point 6 above.
- **`wakes.json`:** a reader during a write never sees `[]` (write-then-rename); an `add_wake` racing a `run_due`
  rewrite and a Stop-waiter `take` loses no item; corrupt JSON under an update is left untouched and nothing is
  written. (Red today.)
- **Nudge item shape:** has `at`, `cwd` from the claim record (byte-equal to the stored canonical path), `by`,
  `claim`. `run_due` launches it through `cwd-exec` in that directory, not `~`. A claim with no valid `cwd` gets no
  nudge and one `no valid cwd` report. A claim whose `cwd` is swapped for a symlink after the nudge is written
  (final or intermediate component) is refused at launch by run-due, which shows as `never-started`. Claims §6 tests
  the launcher; here only the hand-over is tested.
- **Exact-id liveness in `run_due`:** a live process for another id, or for a prefix of this one, doesn't hold the
  wake; shares one case table with `ProcessHoldsSession`'s tests. (Red today: `pgrep -f`.)
- **Reports:** with a fake sink that refuses, `reported` (and the `heartbeat.json` key) stay unset and the next beat
  tries again. Once a key is marked, later beats never emit it. Every claim line matches
  claims' fixed shape, with a condition from its closed set. Every other line matches its own closed set. No code
  path opens the bridge socket, and there's no setting that changes the sink.
- **Units:** the list for `arden` is exactly the seven names above; every name in every tuple matches
  `^[a-z0-9-]+(@[a-z0-9-]+)?\.service$` with a non-empty instance where `@` appears; an unknown user checks nothing;
  the fake `systemctl` records only `is-failed` and `show` verbs, never `start`, `restart`, `stop` or `reset-failed`.
- **Room sessions:** no wake item whose session is the room's own is ever added.
- **Rate cap:** a fourth heartbeat wake in an hour isn't added, and the cap is reported once.

## Decisions (were open questions)

- **The argv rule:** ported into `hook.py` and tested locally, sharing one case table with `ProcessHoldsSession`
  (Claude and Ember agreed).
- **v1 defaults:** a 10-minute beat and 3 heartbeat wakes per hour (both agreed).
- **Report channel:** journal-only in v1. John picks a room channel only after the exclusion is mechanical (Ember).
- **Ember's Codex self:** off until claims and the reply-text parser are separated (Ember).

## What changed in the re-pin to claims revision 4 (from 165aca1)

Ember's final review (room 1570163375776735393), item 1, routed in 1570163545528860796:

- Claims pin: d163367 → **b0b8383** with its `task-claims.md` sha256. Not 2639469: b0b8383 adds the §4 text that the
  ` key=` framing relies on.
- Row A and "Who decides what" cite b0b8383 §4. The lock-order sentence and "owner never started" are now cited as
  pinned claims text, not as behaviour expected of an unpinned revision.
- The ` key=<key>` suffix is cited to claims §4 "Reports" instead of asked of it. No semantics changed; the key,
  at-least-once delivery, the identical repeat, the suffix as sink framing and `reported` tied to the key's spell
  were checked against b0b8383 and agree.

## What changed in revision 3 (from 7433a3b)

Ember's paired review of `d163367` + `7433a3b` (room 1570157282808045569, 1570157284074459168), heartbeat side:

1. **Lock order and split writes.** One global order, in a sentence shared word for word with claims revision 4,
   with `state/fired.lock` added last as a leaf. No lock is held across a launch or a journal write, so a beat now
   runs in three phases (decide and write; emit with no lock held; mark). A table of every multi-lock path. A nudge
   mints its firing id first, writes the item then the mark, and the next beat reconciles both crash halves by that
   id; `take` of a heartbeat item leaves a `taken` fact so this stays exact. Tests: a lock-rank recorder over every
   path, alone and raced, and both halves.
2. **Claimed but never started.** A failure after the spawn claim leaves the record `claimed`; the heartbeat derives
   `owner-never-started`, and the nudge is the single recovery, with `launch: "session-id"` on the same pre-minted id,
   never `--resume`, never a second owner. A failure before the spawn claim stays on the `open`/`handoff` row. Items
   carry `launch` explicitly. Cross-document tests for both routes, plus a proof that C never duplicates A. The claims
   §4 wording, and the report condition (I propose `start failed`), are Claude's.
3. **Claims off.** Ember's routing (1570157489196896399): the spawn claim stays. With claims off, C reports a failed
   `work` firing once, never retries it, and never touches the claim, whatever its `claim` field says. Turning claims
   on later retries nothing C already reported, and A leaves that claim alone until a session's own transition starts
   a new spell. Test: heartbeat on, claims off, then claims on.
4. **`fired.jsonl` concurrency.** Its own lock, `state/fired.lock`, for every append and every snapshot; one `write`
   per line on `O_APPEND`, then `fsync`. A partial trailing line means "incomplete snapshot, retry next beat": no
   ledger-based decision that beat, the fragment moved aside verbatim, the file truncated to its last newline. Tests:
   concurrent writers, and a writer killed mid-append.
5. **Reporting.** The implied "exactly once" is dropped. Every report has a stable idempotency key (firing id plus report kind
   for wake reports, and the analogue for the others), carried as a ` key=` suffix. Delivery is at least once, with
   identical duplicates possible after a crash between the journal flush and the mark; an outbox in `heartbeat.json`
   keeps repeats byte-identical. Tests cover that crash window for each report type.
6. **Deferrals vs retry identity.** Every recovery names the original firing in `retry_of`, the attempt it answers in
   `follows`, and its kind in `attempt`. Each origin gets one ordinary retry and, counted separately, up to three
   usage-limit deferrals (kept at three, with the reason given). Tests walk every attempt through the fourth refusal,
   plus a mixed retry/deferral chain. The same rule now covers A's hand-off retry and a usage-refused nudge.

## What changed in the re-pin (from 20f7f5b)

- Claims pin: f0dfc81 → **d163367** with its hash. Each item revision 2 expected is confirmed or corrected against
  it (Claude's request; Ember 1570132489232584907, 1570147323579990148).
- Pause expiry: revision 2 guessed the heartbeat might become the executor (check A′). In fact the executor is claims'
  `settle_pauses` (`by: pause-expiry`). The heartbeat calls it first in each beat and isn't an actor for it.
- History and `touch`: the heartbeat's marks are one history entry each, `by: heartbeat`. `touch` is a same-status
  entry the heartbeat never writes, and it never ends a spell.
- Reports: the claim line and its closed condition set are claims'. The sink is the heartbeat's configuration, with
  journal only in v1 and no setting to change it. `reported` is set only once the sink confirms. The other checks'
  lines get their own closed sets.
- Row A: matches d163367 §4. Wording rule for `touched since`; `failed` among start failures; the retry is once per
  spell; usage-limit deferrals; `no valid cwd`.
- `cwd`: the canonical stored path is copied byte for byte. The heartbeat checks it only as a filter, and run-due binds
  it with `openat2` and `cwd-exec` at launch. Ask: every item goes through `cwd-exec`.
- A fired hand-off is judged only by A, never also by C.
- Two points for the paired review: the lock order (`heartbeat.lock` → `wakes.lock` → `state/tasks/.lock`), and which
  row owns a hand-off that failed after the spawn claim.

## What changed from revision 1 (e583ec5, ec1348e)

- Claims pin: f0dfc81 with its hash, the re-pin to revision 3 marked pending with what it should change (Ember 1).
- Row A is now f0dfc81 §4 row by row, with the hand-off retry bounded and the silent-stall fix (Claude 1; Arden).
- The ledger is a state machine with firing ids, `enqueued`/`started`/`finished`, liveness and grace checks, and
  one locked transition with crash recovery (Ember 2).
- Retries carry fixed text plus the firing UUID and keep the session structurally; `work` wakes with claims off are
  reported, not retried; leak and race tests named (Ember 3).
- Reports are journal-only in v1; "delivered" means flushed to the journal (Ember 4; Claude 5).
- Unit checks: a fixed, fully instantiated list, observe only, pinned in tests (Ember 5).
- Nudge items get `at`, `cwd` and a separate writer for `by`/`claim` (Claude 2); three rewriters, one lock,
  write-then-rename, no `[]` on bad JSON (Claude 3); usage limit keyed on `is_error`/api error, next occurrence,
  bounded window, fixed 2 h fallback, pinned to the sample (Claude 4; Ember's addition); switches from
  `settings.json` only (Claude 6); a lock and atomic writes for `heartbeat.json` and the cap (Ember's addition).
