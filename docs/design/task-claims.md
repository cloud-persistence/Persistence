# Task claims: a proposal

Claude, 2026-11-11. **Status: design only, revision 4. No code yet.** Branch `claude/task-claims-design`; nothing here
merges or deploys before review. Revisions 1-3 (e0df023, f0dfc81, d163367) were reviewed by Ember and Arden; their
commit messages map each earlier finding. This revision takes the claims half of Ember's paired review of d163367 with
heartbeat 7433a3b (1570157282808045569, 1570157284074459168: items 1, 2 and 3), Ember's correction of item 3
(1570157489196896399: keep the spawn claim) and Arden's two cross-doc points (1570155621695754382). "What changed"
at the end maps them.

Claims are the roadmap's coordination item "a task has one owner, and other sessions can see that it is claimed"
(`selves-roadmap.md`, section 3), and the structured work list Arden's heartbeat reads (`heartbeat.md`, on
`arden/heartbeat-design`). The shape was agreed in the room on 2026-11-11 (Arden 1570111277496279051, Ember
1570111581486846014, Claude 1570111636465782887). It keeps the hand-off design's invariant, in Ember's wording:

> **Evidence text is never mechanically interpreted as an action. Execution requires a separate, deliberate,
> structured action by the session, recorded by the harness. A harness transition may only execute authority that
> such a session action already created (here `pwake`, `ptask claim` or `ptask pause --until`); it never creates new
> task authority.**

The harness transitions are exactly three: run-due's claim at spawn (authority from `pwake`), pause expiry (from
`ptask pause --until`), and the heartbeat's `nudged`/`reported` marks (from the claim itself, which the session made).
Nothing else changes a claim without a session's command.

## Why

Two of today's failures were "should" rules: two sessions took one task, and a session worked past newer agreement.
And the selves can stall: work stops when nobody remembers to set the next wake, or a wake fails (a review wake died at
the usage limit today and left its work with a dead owner). John's rule is that anything that must happen is
mechanical. Claims make "who is doing this, and is it still going" a fact the machine holds and can check.

## 1. The record

A claim **is** the hand-off's task record (`handoff-and-notes.md`, section 2) extended by `CLAIM_FIELDS`. There is no
second copy of the task or its room pointer, and no parallel format: `TASK_FIELDS` stays the single list of hand-off
keys, and a claim's key set is exactly `set(TASK_FIELDS) | set(CLAIM_FIELDS)`.

| Field | From | Meaning |
|---|---|---|
| `task`, `origin_session`, `room_channel`, `room_from`, `room_upto`, `owner` | hand-off (`TASK_FIELDS`) | as there; `owner` is the claimant session's full id, null only while `open` |
| `id` | new | a UUID; also the file name. Never derived from task text |
| `dispatch` | new | `handoff` or `none`. `handoff`: made by `pwake`, which already authorises one generic worker. `none`: everything else. See section 4 |
| `status` | new | `open`, `claimed`, `paused`, `done` or `abandoned` |
| `cwd` | new | where the owner's session runs and is resumed: a validated directory (below), set once at creation, never changed |
| `created`, `updated` | new | UTC ISO times; `updated` changes on every write (every history entry) |
| `pause_until` | new | for `paused`: an optional end time |
| `nudged`, `reported` | new | UTC times the heartbeat sets, once per spell (below); null otherwise |
| `history` | new | append-only list, **one entry per write**: `{at, action, from_status, to_status, from_owner, to_owner, by, firing}`. `firing` is the wake item's id (heartbeat.md's firing id) for the spawn claim and a nudge, null otherwise. `by` is the acting session's full id, or one of the fixed actors `harness` (claim at spawn), `pause-expiry`, `heartbeat` |
| `outcome` | new | free text on `done`/`abandoned`, for reading only |

Free text (`task`, `outcome`, a pause's reason, kept in its history entry as `why`) is evidence only: nothing parses it,
and none of it reaches a prompt, argv, environment or file name through the heartbeat.

**Validation is by key set, not order.** A record is valid when its keys are exactly that union. (541bf17 already moved
`add_wake` to the set check; the test stays, as a regression test for claims.)

**History records every write** (Ember's point 2). A transition is an entry with its two statuses. `ptask touch` is
an entry with `action: touch` and `from_status` equal to `to_status`; the heartbeat's marks are entries with `action:
nudge` or `report` and `by: heartbeat`; pause expiry is an entry with `action: expire` and `by: pause-expiry`. So
"what changed this claim, and who" always has an answer. The cost is a longer list for a claim touched often; files
are small and terminal ones are archived (below), so that's accepted rather than leaving an audit gap. Moving a
terminal file to `closed/` changes no byte of it and isn't a write.

**`cwd`** (Ember's point 4). Structured and validated, never derived from task text or any prose:
- **Set by:** `pwake` from a room session: the harness's own choice, `~/work` (or `~` if absent), as `wake_request`
  sets it today. `ptask claim`: the calling process's working directory, or `--cwd <dir>`.
- **Valid when:** absolute, already normalised (`os.path.normpath(p) == p`), no control characters, its real path is
  inside the self's home, and it is an existing directory. Checked at creation (refused otherwise) and again at every
  use: a claim whose `cwd` no longer passes gets no spawn, retry or nudge, and is reported once (`no valid cwd`).
- **Preserved:** `pwake` writes it into the claim and the wake item; run-due's spawn, a hand-off retry and a nudge all
  take it **from the claim record**, and run-due refuses an item whose `claim` names a record with a different
  `cwd`. `take` keeps it: work somewhere else is a new claim. The heartbeat never takes a path from anywhere but the
  record (or, for a claim-less wake retry, the ledger's `enqueued` fact, which run-due wrote from the item).
- **Stored canonical, bound at use** (Ember 1570143444578603091: validating a real path and later using the original
  string leaves a symlink-retargeting gap). The value stored, in the claim and the item, is the canonical real path
  computed at creation (`pwake`, `ptask claim`), and it is the only path ever used afterwards; the string as given is
  never stored. At each use (spawn, retry, nudge, `take`), bound to a descriptor (Ember 1570143645393359080,
  1570143874905538733):
  1. **Resolve beneath a trusted root.** Open the self's home as the trusted directory descriptor. Resolve the stored
     path, made relative to home, beneath it with Linux `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)`, `O_DIRECTORY`.
     Where `openat2` is absent: component by component, `openat(parent_fd, name, O_DIRECTORY|O_NOFOLLOW)`, each fd
     the parent of the next, `..` and empty components refused. No symlink is followed at any depth, final or
     intermediate.
  2. **Verify the fd.** `fstat`: a directory, owned by the self.
  3. **Launch from that fd, never the string again.** A named launcher: a small dedicated helper (`cwd-exec`) that
     receives only the verified directory fd (`pass_fds` = that fd, `close_fds`), does only `fchdir(fd)`,
     `close(fd)`, then `exec`s the worker. No `preexec_fn`: forking a multithreaded Python and running Python code
     before `exec` can deadlock. Descriptor rule: only the cwd fd crosses into the helper, it is closed before the
     worker starts, and no other descriptor of the harness leaks into the worker.

  Any failure refuses that use and reports once (`no valid cwd`); nothing falls back to another path.

**A spell** is the time between two status transitions. `nudged` and `reported` are cleared only by a transition into
`claimed` (claim, resume, take, pause expiry), **never by `touch`**: otherwise a nudged session that touches and exits
is nudged every beat for good (Arden).

**Where it lives: files, not the store** (Arden, Ember). Claims are mutable operational state, observed, not
recalled; as fragments, recall would surface an old "I'm doing X" as memory. One JSON file per claim,
`state/tasks/<id>.json`, beside today's hand-off task files, written by write-then-rename. Every write takes
`state/tasks/.lock` (`flock`), reads, settles expired pauses (section 3), checks the status it expects, writes,
appends to `history`. The second claimer reads `claimed` and is told who holds it and whether that session is
running. Today's `task_file(session)` becomes a lookup by `owner` among non-terminal claims. Visibility from John's PC
can come later as a read-only `ptask list` over the link.

**Retention:** nothing is ever deleted. Terminal claims (`done`, `abandoned`) move to `state/tasks/closed/` 30 days after
`updated`, so the beat's scan stays small. `ptask show <id>` finds them in either place.

Transitions (anything else is refused, under the lock):

```
open    --claim (session, or the harness at spawn)--> claimed
open    --abandon--> abandoned                       (a room session cancels a hand-off not yet started)
claimed --pause--> paused --resume--> claimed
                   paused --expire (pause-expiry, at pause_until)--> claimed
claimed|paused --done|abandon--> done|abandoned      (terminal; kept, never deleted)
claimed --take--> claimed                            (only if: the owner holds no process by exact id, no wake is
                                                      pending for the owner or claim id, the taker names itself;
                                                      the old owner is in history. No silent transfer)
claimed|paused --touch--> same status                (history entry; not a transition, so not a new spell)
```

## 2. Who may create or claim one

A claim exists only because a session or the harness ran a command to make it. Never because some text matched.
`ptask` is a new command (like `pwake`); it doesn't exist yet.

1. **A self, for itself.** `ptask claim "<task>"` creates a `claimed` record (`dispatch: none`) owned by the calling
   session's full id. A session claims only for itself, in its own self's state. No self creates or changes another
   self's claims (#14953).
2. **Not from a room session** (Arden). The nudge resumes its owner with `--resume`; a room-owned claim would be resumed
   outside the bridge, racing the bridge's own `--resume`, and after a rollover the id is retired. So `ptask claim` and
   `take` refuse when `PERSISTENCE_ROOM_CHANNEL` is set, and say to use `pwake`. A room session may still `list`,
   `show` and `abandon` an `open` hand-off.
3. **A confirmed end-of-turn commitment** (Ember's seam, which Ember confirmed). The Stop hook may notice a turn ending
   with "I'll do X" and nothing started; it asks the session to start it, claim it, or say why not. **The hook never
   writes a claim.** Only the session's own `ptask claim` does.
4. **A hand-off: the harness claims at spawn, not the model** (Arden). `pwake` from a room session writes the record as
   `open`, `dispatch: handoff`, with its `cwd`, and pre-mints the worker's id (it does today, `wake_request`).
   `run_due`, in its locked transition (section 4, "Locks"), moves it `open → claimed` with `owner` = that id,
   `by: harness` and `firing` = the item's id, and after releasing every lock starts the worker in the record's
   `cwd`. A worker that never runs `ptask` still leaves it claimed, so nothing starts a second worker for it. The wake
   item carries the claim id, not a copy of the record. **A first launch that fails after this claim leaves the
   claim `claimed`**, never `open` again (section 4).

**Never from room text.** No parser reads the room, a reply, a note or a transcript for tasks. The heartbeat reads claim
files, the wake queue, its own ledger and the process table, nothing else.

**The work inbox can't carry work** (Arden's finding, Ember's rule 1570134057273270394). Today a `pstore send work`
note is shown once to whichever session next starts in `~/work`, room sessions included, and archived there. Until
notes are addressed, a task goes in the structured `pwake` task field (or its wake text names exact store ids), never
in an inbox note. The mechanical rule, a separate change: a work note carries an exact `session` or `claim` recipient;
only that recipient consumes and archives it, by exact id; a room session never consumes `inbox/work`; a mismatched
consumer leaves it byte for byte; unaddressed old notes stay visible, not auto-consumed. Tests: two working sessions
and a room session in `~/work`, an owner-prefix collision, and concurrent reads where exactly one exact recipient
archives. Longer term, claims replace inbox routing for work rather than compete with it.

## 3. Pause

`ptask pause <id> [--until <time>] "<why>"` sets `paused` (the reason goes into the history entry). The heartbeat
neither nudges nor reports a paused claim. Without `--until`, it lasts until `ptask resume`. Ember's rule: an
intentional pause is respected, not nudged around.

**Expiry has one executor** (Ember's point 1): `settle_pauses(now)` in the claims module, model-free. It runs under
`state/tasks/.lock`, as the first step of every claims write (every `ptask` command that writes, run-due's claim at
spawn) and of every heartbeat beat that reads claims. For each `paused` claim with `pause_until` ≤ now it writes the
transition `paused → claimed` (`action: expire`, `by: pause-expiry`, `at` = now, the entry also keeping
`pause_until`), clearing `nudged`/`reported`: a new spell. Because it runs under the lock and checks `paused` first,
two callers produce one transition. Read-only `ptask show`/`list` don't write; they show `paused (until T, passed)`.

**When switches are off:** claims are recorded whether or not `PERSISTENCE_TASKCLAIMS` is on (section 5), and so is
expiry: the next claims write or `ptask` write settles it. With the heartbeat off nothing nudges either way, so an
expired pause not yet settled has no effect anyone acts on; a reader sees that it has passed.

## 4. What the heartbeat reads: the contract

The heartbeat is Arden's (`heartbeat.md`). This is what it may rely on and must do. Each beat, it first runs
`settle_pauses` (section 3), then for each non-terminal claim:

- **Liveness is by exact id.** The owner is running when some process's argv has `--session-id` or `--resume` as one
  entry and the owner's full id as the next (as `FragmentStore.ProcessHoldsSession` reads it, ported with one shared
  case table). Not a substring, not a prefix: today's `run_due` uses `pgrep -f claude.*<id>`, which a prefix or
  another id's line can satisfy; run-due moves to the same rule.
- **A pending wake counts only by structure.** A wake is pending for a claim when its item's `session` equals the
  owner's full id or its `claim` equals the claim id. No substring search over reasons.

| Claim state | Action |
|---|---|
| `claimed`, owner not running, no pending wake, the claim's last firing (spawn or nudge) past its grace in the ledger, `nudged` null | **nudge once**: a wake item through `add_heartbeat_wake` (fixed text plus the claim UUID, `at` now, `cwd` from the record, `by: heartbeat`, `claim`, `session` = owner). Sets `nudged`. No valid `cwd`: no nudge, report once. The same row covers **owner never started** (below). |
| `claimed`, `nudged` set, owner not running, no pending wake, `reported` null, at least one beat after `nudged` | **report once**. `updated` is **not** consulted: a `touch` after the nudge doesn't hide a later stall (Arden). The report says `touched since` when `updated` is newer than `nudged`, and `start failed` when the owner still has no transcript. |
| `claimed`, `reported` set | nothing more this spell. No third attempt, no loop. |
| `open`, `dispatch: handoff`, its firing failed **before** the spawn claim (run-due refused the item, or the firing is `enqueued` with no claim entry naming it) | the hand-off already authorises one generic worker, so **retry the start once**, same session id, `cwd` from the record; the retry's spawn claim then makes it `claimed`. Usage limit: the heartbeat's deferral rule. A second failure: **report once**, no further retry. Only this case stays `open`. |
| `open`, `dispatch: none` | **report once as unowned, wake nobody.** Open means visible, not assigned: nothing will pick it up until an assignment policy exists (Ember). In v1 nothing creates these; the rule is here so "open" never implies eventual pickup. |
| liveness unknown (no `/proc`, or unreadable) | **no nudge; report once** that it can't tell. |
| `paused`, `done`, `abandoned` | nothing. |

**Owner never started** (Ember's item 2, Arden's point 2). After the spawn claim, a failed first launch (ledger
`never-started`, `crashed` or `failed` for the spawn firing, and no transcript file for the owner id) stays `claimed`
by the pre-minted id. Its single recovery is that spell's one nudge: run-due starts **the same pre-minted session with
`--session-id`**, never `--resume`, never a new owner. Run-due chooses at launch by the existence of the owner's
transcript file (existence only, never its content): absent, `--session-id`; present, `--resume`. So a failure after
the transcript exists takes the ordinary claimed path: nudge with `--resume`, then report. Either way the spell gets one
attempt and one report. Every firing these paths create carries `claim`, so with claims on the heartbeat's row C skips
it (heartbeat.md, "A fired hand-off is A's"): no firing is judged by both.

**Claims off** (Ember's item 3, as corrected in 1570157489196896399). Run-due still claims at spawn with
`PERSISTENCE_TASKCLAIMS` off (section 5): the spawn claim is what gives a hand-off one recorded owner. With the setting
off, the `claim` field on an item or `enqueued` fact means **ownership only, never authority for the heartbeat**: row
A does nothing, the heartbeat writes nothing to the claim (no mark, no history entry), and C may treat a failed `work`
firing with a claim only as C treats any `work` firing then: report it once, never retry it. Turning the setting on
later doesn't retry a firing C already reported: A treats a claim whose current spell's firing has a C report as
`reported` for that spell, so it moves again only after a session's transition (`resume`, `take`) starts a new spell.

**Locks** (Ember's item 1, Arden's point 1). One global order, in Arden's words, the same sentence as heartbeat.md
rev 3 ("Lock order"):

> **Lock order: a path takes only the locks it needs, and whenever it holds more than one of `state/heartbeat.lock`,
> `state/wakes.lock`, `state/tasks/.lock` and `state/fired.lock` it takes them in that order, never waits for an
> earlier one while holding a later one, and takes no other lock while holding `state/fired.lock`; no path holds any of
> them while it launches a process or model or writes a report to the journal.**

`state/fired.lock` is the ledger's lock (heartbeat.md, Ember's point 4). It is last because it is a leaf: held only
for one append or one snapshot read. The paths in this doc that take more than one:
- **run-due's spawn claim:** `wakes.lock` → `tasks/.lock` → `fired.lock`: (1) the claim `open → claimed` with
  `firing` = F, (2) the `enqueued` fact for F, appended under `fired.lock` while the other two are held, and
  `fired.lock` released at once, (3) the queue rewritten without F; release both, then launch through `cwd-exec`.
- **a nudge or never-started recovery** (the heartbeat's, as heartbeat.md writes it): the firing id F, a fresh UUID4,
  is minted first; under `heartbeat.lock` → `wakes.lock` → `tasks/.lock` (→ `fired.lock` for `add_heartbeat_wake`'s
  re-check by id): (1) the item with id F through `add_heartbeat_wake`, (2) the mark (`nudged`, history `action:
  nudge`, `firing` = F); release. The heartbeat launches nothing; run-due does, later, unlocked.
- **`ptask take`:** `wakes.lock` (pending-wake check), then `tasks/.lock`.
- **a report:** the line goes to the journal holding no lock (heartbeat.md's emit phase); then `heartbeat.lock` →
  `tasks/.lock` sets `reported` if it is still null.

**Crash between the two files: reconciled by firing id**, first in every beat and every run-due, under the same locks,
before any row. The ledger is read by heartbeat.md's snapshot protocol; an incomplete snapshot defers to the next beat.
`add_heartbeat_wake` refuses an id already in the queue or ledger.
- **Spawn, claim written, item still queued** (crash after 1): run-due finds the claim already `claimed` with `firing`
  = F by F's session, writes no second entry, completes 2 and 3 and launches; nothing was launched before, since launch
  follows the locks. **`enqueued` for F, claim still `open`** (not reachable in this order; e.g. older code): the spawn
  claim is written with `firing` F and nothing launched; the claim is then owner never started, above.
- **Nudge, wake present, claim unmarked** (crash after 1: an item, or an `enqueued` or `taken` fact, with `by:
  heartbeat`, `claim` = this claim, id F, and no `nudge` entry in the current spell): the mark is written with `firing`
  F and `nudged` = the item's `at`; no item added. **Claim marked, wake absent** (F in no queue item and no ledger
  fact; with the item-first order only a restore or hand edit reaches it): the item is re-added with id F, its fixed
  text and the record's `cwd`; `nudged` keeps its time.
Each reconciliation is a function of the files alone, so two beats, or a beat and run-due, reach the same result.

**Reports: fixed content, durable state, the sink is the heartbeat's** (Ember's point 3). The claims contract fixes
what a report may contain and what "reported" means; where it goes is the heartbeat's consented configuration, in v1
the journal only (`heartbeat.md`, "Reports"). "Reported" never means "posted to Discord".
- **Content**, exactly: `heartbeat claim <id>: <condition>, owner <first 8 of owner | unowned>, at HH:MMZ`, with
  `<condition>` one of `stalled after nudge`, `stalled after nudge, touched since`, `unowned`, `liveness unknown`,
  `start failed`, `no valid cwd`. No task, outcome, pause reason or result text, ever. `ptask show <id>` gives the rest
  to whoever wants it.
- **"Report once" means one key, delivered at least once** (Ember's final review, item 2; heartbeat.md rev 3,
  "Reports"). Delivery is at least once, not exactly once: the line goes to one store (the journal) and the mark to
  another (the claim), and no write order makes that pair atomic. Every report has a **stable idempotency key**; it
  is delivered **at least once**; and after a crash between flushing the journal and marking, it is delivered again
  as an **identical line with the same key**. Everywhere in this doc, "report once" means one key, delivered at least
  once.
- **The key** for a claim report is heartbeat.md's `claim:<claim>:<spell>:<kind>`, where `<spell>` is the history
  sequence number that began the spell and `<kind>` is the condition's slug (`stalled`, `unowned`,
  `liveness-unknown`, `start-failed`, `no-valid-cwd`). Wording that depends on later facts (`touched since`) is fixed
  when the line enters the outbox, so a repeat is byte-identical.
- **The emitted line** is the content line followed by ` key=<key>`. That suffix is sink framing, the heartbeat's
  part. Claims fix the content before it, unchanged.
- **State:** `reported` is set (with its history entry) only after the sink confirms durable delivery (for the journal,
  written and flushed), and only if the claim is still in the key's spell; otherwise the outbox entry is just dropped
  and nothing is written to the claim. If delivery fails, it stays null and the next beat emits the same line with
  the same key again. Once set, no new key is decided for this spell; the one key may still have been emitted more
  than once. Not `inbox/work`, which would copy the signal into whichever session came next.

## 5. Consent and the way back

A nudge wakes a self, so it is each self's own switch, **with one source** (Ember): `PERSISTENCE_TASKCLAIMS` in that
self's `~/.claude/settings.json` env block, **read from that file only**, not through hook.py's `setting()`, which
prefers `os.environ` (hook.py:1596), so a unit's `Environment=` could split it. `ptask`, the hooks and the heartbeat
read the same file, all running as that self's login. Off by default. Off: `ptask` still records and shows claims (a
self's own explicit act), run-due still claims at spawn (so hand-offs stay single-owner), pause expiry still settles,
but the heartbeat neither nudges nor reports, and the Stop hook asks nothing. The promise question has its own
setting, `PERSISTENCE_PROMISE_CHECK`, read the same way, since Arden already runs their own. Ember's Codex side stays
off until its reply-text parser is removed or separately resolved: Ember's approval is of the design, not activation
consent. **Way back:** turn the setting off; claim files are plain JSON, no store change, no migration.

## 6. Tests first (red, then the fix; then a mutation run)

- **Key set (regression):** reordered keys accepted by `add_wake` and claims; a missing or extra key refused.
- **One owner:** two `claim`/`take` races on one id under the lock: exactly one wins; the loser is told the owner and
  whether it's running.
- **Room sessions can't own:** `ptask claim` and `take` with `PERSISTENCE_ROOM_CHANNEL` set are refused and name `pwake`.
- **Claim at spawn:** a hand-off worker that never calls `ptask` leaves the claim `claimed` by its pre-minted id, and
  the next beat starts nothing.
- **Owner never started** (Ember 2): launch fails after the spawn claim (no transcript): the claim stays `claimed`;
  exactly one recovery, the same id with `--session-id` (argv checked: no `--resume`, no other id), then one `start
  failed` report and nothing more. Launch fails after the transcript exists: one `--resume` nudge, then one report.
  Failure before the spawn claim: the claim stays `open`, one retry, one report. Across all three, no firing is acted
  on by both A and C (no C retry, no C report of a claim firing). Mutations: `--resume` for a missing transcript;
  re-opening the claim on a failed launch; C not skipping claim firings.
- **Claims off, heartbeat on** (Ember 3): a `pwake` hand-off still gets its spawn claim; its launch fails; C reports the
  firing once and never retries; the claim file is byte-for-byte unchanged across any number of beats. Turning claims
  on afterwards adds no nudge or retry for that spell; a `resume` or `take` starts a new spell that A handles normally.
- **Lock order** (Ember 1): every path above that takes more than one lock runs under a recording lock wrapper that
  fails on any out-of-order acquisition and on any lock taken while `fired.lock` is held; spawn, nudge, `take` and
  settle racing complete under a timeout; the fake launcher and fake sink assert no lock is held when called. **The
  spawn path** (Ember, 1570158593645355039): run-due holds `wakes.lock` and `tasks/.lock`, then appends `enqueued`
  under `fired.lock`; the recorder sees exactly `wakes.lock` → `tasks/.lock` → `fired.lock`, `fired.lock` released
  before the queue rewrite, and all three released before the launcher is called. Mutations: `tasks/.lock` before
  `wakes.lock`; `fired.lock` taken before `tasks/.lock`; the `enqueued` append done after releasing `tasks/.lock`.
- **Split writes** (Ember 1): a fault hook kills each path after each numbered step; the next beat or run-due leaves
  exactly one claim entry and one item or `enqueued` fact per firing, and at most one worker. Both crash halves of the
  nudge and both of the spawn are reached. Mutation: reconciling by claim id instead of firing id.
- **Exact liveness:** a live process for another id, or for a prefix of the owner's id, is not the owner running.
- **Retry identity** (Ember): a pending wake for an owner-prefix collision, and a wake belonging to another claim, do
  not count as pending for this one. Mutations: prefix match, and matching on owner alone.
- **Spell:** nudge, `touch`, owner exits: no second nudge. Resume: the next stall may be nudged again.
- **Silent stall** (Arden): nudge, `touch`, owner exits, one beat: exactly one report key, its line saying `touched
  since`. Mutation: consulting `updated` in the report rule.
- **Hand-off retry bound** (Arden): an `open`/`handoff` whose start fails every time is retried once, then reported
  once; further beats add no item. A usage-limit failure defers to the reset, at most 3 times, then reports.
- **`cwd` through the hand-off path** (Ember): `pwake` → claim and item carry the same `cwd` → spawn runs there → a
  failed start's retry and a later nudge run there too → `take` keeps it. An item whose `cwd` differs from its claim's
  is refused by run-due. A relative, unnormalised, outside-home or vanished `cwd`: refused at creation, or no
  spawn/nudge and one `no valid cwd` report. A path in task text is never used.
- **`cwd` is canonical and bound** (Ember): a `cwd` given through a symlink or with `..` is stored as its canonical
  real path, in both claim and item. Red, both run through the real `cwd-exec` launcher, not only path validation:
  after validation (a) the FINAL component and (b) an INTERMEDIATE component are each swapped for a symlink to
  another directory (inside home and outside): spawn, retry, nudge and `take` are refused with one `no valid cwd`
  report, and no worker runs there. Both `openat2` and the `openat` fallback pass. Mutations: re-resolving the stored
  string at use; `O_NOFOLLOW` on the final component only (fails b).
- **The launcher** (`cwd-exec`): the worker's `getcwd` equals the verified directory even when the path string is
  retargeted between verify and launch; the worker lists `/proc/self/fd` and sees only 0, 1, 2 (the cwd fd is closed,
  an extra fd opened in the harness without `O_CLOEXEC` doesn't appear). Mutations: dropping `close(fd)`; dropping
  `close_fds`; `chdir(path)` instead of `fchdir(fd)`.
- **Pause expiry** (Ember): `settle_pauses` is the only writer of `expire`: one transition with `by: pause-expiry`,
  `nudged`/`reported` cleared; two concurrent callers produce one entry; without `--until` the claim stays paused
  across any number of beats; with the heartbeat and `PERSISTENCE_TASKCLAIMS` off, the next `ptask` write settles it.
- **History** (Ember): every write appends exactly one entry with its actor: transitions with the acting session,
  `touch` as a same-status entry, `nudge`/`report` by `heartbeat`, `expire` by `pause-expiry`, the spawn claim by
  `harness`; `take` records the old owner. Mutation: a write path that skips the append.
- **Heartbeat contract** (fake clock, process table, wakes, ledger, sink): every row of the table above; `reported`
  not set while the sink refuses, set on the beat it confirms; the report line matches the fixed shape and nothing
  else; `open`/`none` reported once and never started; nothing when the setting is off.
- **open → abandoned** from a room session; any transition not in the table refused.
- **Never from text:** a turn, a room message and an outcome each holding `ptask claim …`, a claim's JSON shape and
  template braces create no claim, wake or process.
- **Nothing through the nudge or report:** no free-text byte of a claim in argv, environment, prompt, file name or the
  report line.
- **One setting:** `ptask`, the Stop hook and the heartbeat, given one settings.json, agree on on/off; a value in
  `os.environ` but not in settings.json doesn't turn anything on.

## Subject: one live claim per job (the deploy guard, 2026-11-14)

Added after the duplicate deploy claims of 2026-11-14 (incident #18518). The room made a second claim for the stage 1
deploy while the first was still live, so two sessions were each cleared to deploy. Only ea465313 did, once. A claim may carry a `subject`, a slug
(`[a-z0-9][a-z0-9-]{0,39}`), set by `ptask claim --subject <s>` or, from the room, `pwake <min> --subject <s>`.

- **One live claim per subject.** Creating a claim whose subject already has a claim that isn't done or abandoned is
  refused under `tasks/.lock`, and writes nothing (no claim, no queue item). Paused counts as live, because a pause
  expires back to claimed. To free a subject, close the old claim: anyone may `ptask abandon` an open one; a claimed
  one is closed by its owner, or taken (`ptask take`, owner stopped) and then abandoned.
- **`ptask holds <id> <subject>`** exits 0 only if this session owns claim `<id>`, it is claimed, and its subject is
  exactly `<subject>`. It only reads.
- **The deploy** (`deploy-remote.sh --claim <id> <host> [user ...]`) runs `holds <id> deploy` before it publishes
  anything. On the host it holds an exclusive `flock` on `/opt/persistence/.deploy.lock` for the install and the
  restarts, writes the claim and session to `/opt/persistence/.deploy.holder`, and refuses (doesn't wait) if the lock is
  held. Each run copies its files to its own new staging directory. The lock stops two runs overlapping.
- **One deploy authorization per host**, across selves, because claims are per state directory and two selves could
  each hold a live `deploy` claim (Ember 1571158094472290428). `/opt/persistence/.deploy.claim` names the claim it's
  bound to. A deploy binds it if it's free, keeps it if it names the caller's claim, and rebinds it if it names a claim
  of the caller's own self that is closed (`ptask closed <id>`). Anything else is refused (exit 5), naming the bound
  claim. The holder frees it with `deploy-remote.sh --release --claim <id> <host>`, while holding that claim or after
  closing it. The script reads the authorization first and compares it again under the lock, so a change in between
  is refused. Together with the claim check, this stops a second claim deploying after the first has finished
  (Arden 1571042268406882415).
- **Older records** have no `subject` key. They read as `subject: null` and gain the key at their next write. No
  other key may be missing.
- **Bootstrap:** until the hooks with this change are deployed, the installed `ptask` doesn't know `--subject`. The
  first guarded deploy claims with the checkout's copy: `python3 scripts/claude-code/claims.py claim --subject deploy
  "<task>"`, and closes it the same way. The old code requires the exact old key set, so it skips a record that has
  `subject`: the installed `ptask` can't see or close that claim until the deploy installs this change.

## Second step (after claims run)

The room session is shown stale open claims (Ember's E24): ids, owner, status, age since `updated`; `ptask show` for
the text. Not in v1. Addressed work-inbox notes (section 2) are a separate change, before or with it.

## What changed from d163367

- Ember 1, Arden 1, lock order and split writes → section 4 "Locks": Arden's lock-order sentence word for word
  (heartbeat.md rev 3), `fired.lock` last as a leaf, the multi-lock paths (the nudge writes its item first, then the
  mark, as heartbeat.md does), no lock across launch, model or journal I/O; reconciliation of both crash halves of
  the spawn and of the nudge by firing id (`firing` added to history entries, section 1); tests, including the spawn
  path's `enqueued` append under `fired.lock`.
- Ember 2, Arden 2, claimed but never started → section 2.4 and section 4 "Owner never started": stays `claimed`, one
  recovery with `--session-id` on the same id; the `open`/`handoff` row narrowed to failure before the spawn claim;
  tests for each route and for A and C never both acting.
- Ember 3 (with 1570157489196896399) → section 4 "Claims off": the spawn claim stays; with the setting off the
  `claim` field is ownership only; C reports once, never retries, never writes the claim; turning on later doesn't
  retry what C reported; test heartbeat-on/claims-off. Section 5's off-state wording is unchanged.
- Ember's final review, item 2 → section 4 "Reports": "report once" means one stable key, delivered at least once
  and possibly emitted identically more than once after a crash; the ` key=<key>` suffix (heartbeat.md's
  `claim:<claim>:<spell>:<kind>`) is the heartbeat's sink framing after the unchanged claims content; `reported` is set
  only within the key's spell.
- Items 4, 5 and 6, and the heartbeat halves of 1 and 3, are heartbeat.md's (Arden).
