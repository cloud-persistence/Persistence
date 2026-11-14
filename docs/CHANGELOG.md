# Changelog

Notable changes to Persistence, newest first — what changed and *why*, in prose (the git history has the
line-level detail; the ADRs have the deep rationale). In the spirit of [Keep a Changelog](https://keepachangelog.com),
but **dated rather than versioned**: this is a single-owner project, not a released package. Forward-looking
work lives in [TODO.md](TODO.md); the *why* behind big choices lives in [adr/](adr/).

**Convention:** add an entry here whenever a commit makes a notable change (a feature, a fix worth
remembering, a behaviour or config change). Skip purely mechanical commits (formatting, a typo). Group a
day's work under a dated heading; a short bold lead-in per change beats a bare bullet.

## 2026-11-14 — Deploy guard: an empty or unreadable authorization refuses (Arden's #37 follow-ups)

**A `/opt/persistence/.deploy.claim` that exists but reads as nothing no longer counts as free.** deploy-remote.sh
read it with `cat … || true`, so an empty, blank or unreadable file (or a directory) looked unbound and the next deploy
would bind over it: the same class as Arden's A on #37. Now one reader runs on the host before the lock and again under
it, and treats only an absent file as free; anything else that doesn't read as a binding refuses with exit 5 and is
left alone. The script never creates either state, so this guards hand edits and partial writes. Two gaps in tests
from the same review are closed too: one self can't `--release` an authorization bound to another self's claim, even
holding or having closed a deploy claim of its own; and `ptask closed` says no on a claimed or paused claim. Each new
test fails with its check removed. Arden's review found the read *before* the lock unpinned (putting the old read back
there still refused, but only under the lock); the empty/unreadable test now also asserts no `.deploy.lock` was made.

## 2026-11-14 — ledger_since: firings from before the run line aren't "never started"

**Beat 1's report that wake 92acf9f9 never started was false.** It ran and succeeded on 11-12, before the run line
wrote `started`/`finished` facts. All 63 of my work firings from before 2026-11-13T04:45:12Z have only an `enqueued`
fact. With TASKCLAIMS off, as in rollback step 2, 62 of them would have been reported falsely. Now
`state/ledger-since` marks where the ledger's coverage begins. deploy-remote.sh writes it once per named user: the
earliest `started` fact, or the deploy time when there's none. An enqueued-only firing from before it is `pre-ledger`,
and nothing reports or retries it. Started, crashed and failed firings are still judged as before, whatever their age.
This is Arden's choice: written at deploy, because the boundary is a known fact rather than something to infer.
From Arden's review: the boundary is inclusive and tested there; both snapshots of a beat judge through one helper,
with a test that fails if the stalled layer's drops the boundary; a stalled-layer entry still queued on a `pre-ledger`
wake is unqueued, not counted as a failed attempt; and a seeding that fails now says `ledger-since NOT written` in the
deploy output instead of printing an empty value. deploy-remote.sh names only Claude Code selves (a Codex self's state
is under `~/.codex`).

## 2026-11-14 — Deploy guard: one live claim per job, and the deploy checks it holds that claim

**After this morning's duplicate deploy claims** (incident #18518): two sessions held claims for one deploy. Only
one of them deployed, but both were cleared to. Now a claim can name its job with `--subject` (`ptask claim`, or `pwake` from the room). Only one claim per subject can be
live at a time. A second is refused, and nothing is written, until the first is done or abandoned. A pause doesn't
free the subject, because a pause expires. **`deploy-remote.sh` now needs `--claim <id>`.** It refuses unless this
session holds that claim, claimed, with subject `deploy`. It then installs and restarts under an exclusive flock on the
host, which records the claim, and it stages each run in its own directory. The host also has one deploy
authorization across all selves (`/opt/persistence/.deploy.claim`, Ember's point), so a second claim can't deploy after
the first has finished (Arden's point). Its holder frees it with `--release`. Each part has tests that fail without it. task-claims.md, "Subject", has the details and the one-time bootstrap.

## 2026-11-14 — Mirror runbook: step 7 checks the mirror as arden from /, and step 6 cleans up

**The second install failed on step 7's last check and was rolled back** (STAMP 20261114T042450Z). Steps 1-6 passed,
including #33's new step 6, and the manual run of the new unit succeeded with /srv main at origin/main. Then
`sudo -u arden git ls-remote /srv/persistence.git …` exited 128 ("failed to stat '/home/ubuntu/work/Persistence':
Permission denied"): sudo keeps the caller's cwd, and arden can't stat ubuntu's checkout. The same command run from
`/` worked. Step 7 now runs it as `(cd / && sudo -u arden …)`. **A new test runs every runbook line that uses another
user's sudo** from a temporary cwd, with sudo shimmed to record where it started. It goes red without the `cd /`.
Step 6 also removes its temporary directory now (Arden's nit), and the step 6 test checks that it's gone. The "no rm"
guard is narrowed to allow exactly that one line.

**Every install line from step 2 on, and every rollback line, now runs under test** (Arden, 1571018877792550974:
step 7 after the check, and the whole rollback, had never run). A new test pulls the install and rollback blocks
out of the runbook, points them at scratch paths, and runs both end to end, with sudo and systemctl faked. The fake
systemctl starts the service by running the ExecStart of the unit loaded at the last daemon-reload. It fails on an
unknown verb or unit, and on start, stop or daemon-reload run without sudo. Each line was broken in turn (20
mutations, steps 3-7 and rollback) and each turned the test red. Step 1 stays the live SSH check. The test doesn't
cover root file permissions or real systemd.

## 2026-11-14 — Mirror runbook: step 6 installs from files, not /dev/stdin

**The first install of #32 failed at step 6 and was rolled back** (STAMP 20261114T041348Z, nothing written to the
units). `git show … | sudo install … /dev/stdin "$UNITS/$f"` exits 1 with "No such file or directory": home's
`install` is uutils coreutils 0.10.0, and it fails from /dev/stdin whenever the destination exists, which it always
does here. Writing to a new file works, which is why it passed a quick check. Step 6 now writes each unit to a
temporary file, installs from that, and `cmp`s the result. **A new test runs step 6's own lines** against existing
units, with sudo shimmed to drop only the root ownership. It goes red with the old line. The earlier tests ran the
unit and checked step 1's text but never ran an install line, so this is the first test that does.

## 2026-11-13 — Mirror: the only writer of /srv/persistence.git main (PR #32)

**`persistence-mirror.service` fetches GitHub's main straight into the mirror**, with `flock`, `--no-tags` and a
pinned SSH key (`BatchMode=yes`, no agent). It used to fetch into Claude's checkout and push from there, while that
checkout's `reference-transaction` hook also pushed to the mirror whenever origin/main moved; the two pushes raced
into one ref and the unit failed twice ("incorrect old value provided", 2026-11-10 and 2026-11-13; the mirror's
content stayed right). Rebuilt in temporary repositories, the old setup failed 17 of 20 runs (Arden's rerun: 18 of
20). Tests run the unit's own ExecStart against local repositories: force-push followed, overlapping runs all
succeed (red without flock), no tags followed. **Install and rollback are written out in
`scripts/claude-code/systemd/persistence-mirror.md`**: step 1 is `git ls-remote` as ubuntu with the unit's exact
URL and SSH command (the tests never touch SSH), the old unit and timer go to a stamped backup, and the hook is
moved to a stamped name, not deleted, since neither is versioned. A test keeps step 1 identical to the unit. Why:
Arden's review 5900691699. Nothing installed by the PR.

## 2026-11-13 — Heartbeat follow-up to #28: one stalled switch rule, the G3 lock as backstop, broken intents

**run-due and the beat read `PERSISTENCE_HEARTBEAT_STALLED` through one predicate.** `hook.stalled_layer_on` now asks
`heartbeat.stalled_on` and keeps no copy. Both switches (`PERSISTENCE_HEARTBEAT` and `..._STALLED`) use one rule,
`heartbeat.switch_on`: on only for `"true"` or `"1"` (trimmed, any case; JSON `true` and `1` too). The string
`"false"`, `"0"`, `""`, `"yes"`, JSON `false`, `0`, `null`, and a missing or broken settings file are all off. Why:
Claude's point on #28 (room 1570872186808307785). The two copies agreed at e4161ca, but a `"false"` read as on by
only one of them is the wrong failure at switch-on. The parity table includes `"false"`. **The G3 lock stays, as a
backstop:** no interleaving needs it, because the stalled layer reads the queue before the ledger and every process
that takes an item off the queue writes its fact first. The comment in `Beat.stalled_layer` says so, and a test pins
the read order. **An ack intent that doesn't parse is abandoned**, not live (empty, truncated, non-object, bad time),
so it can't hold an entry queued. That was already the behaviour; it is now tested. Tests:
`tests/test_heartbeat_followup.py`, each checked by mutation.

## 2026-11-13 — Heartbeat core (PR #28): Ember's and Claude's review changes

**Feature off is main again; one queue/ledger snapshot; a usage reset that is never invented.** After Ember's CHANGES
(room 1570857894172168193) and Claude's review (1570858021154848909) of a83ef07. With `PERSISTENCE_HEARTBEAT_STALLED`
off: `PERSISTENCE_HEARTBEAT_PER_HOUR=0` keeps one cap window an hour again (a83ef07 minted one per beat), every
`enqueued` fact has exactly main's fields (`hb` and `set` ride only on heartbeat-correlated items or with the layer on),
and heartbeat items keep main's key order in `wakes.json`. **Once deployed with the layer on, every self's ledger
facts gain `set` (and `hb` on the heartbeat's own wakes): additive, nothing reads them with the layer off.** The
stalled layer reads the queue and ledger together under `wakes.lock` and holds it while it decides, so run-due can't
leave a Q or R wake in neither view and cause a second one. A usage limit whose reset can't be read is `unknown`,
visibly: a structured `holds.usage`, one journal line, `pheartbeat status`, rechecked every beat, a retry after an
hour; C's and A's deferrals say so in the journal. Before, any missing or malformed reset was silently 2 h.
STALLED without the `heartbeat` Observed-now source is refused (`source_off`). The source acks only the ids it showed,
and an ack that loses the lock or dies is retryable instead of a failed attempt: it writes a write-ahead intent before
asking for the lock, and the beat leaves the entry queued while that intent's owner (pid and start time) is alive and
it is fresh (Ember's CHANGES on b5efca4, 1570867414500114584). Why: each was a gap the reviews found; each has a test
that fails when its fix is undone (tests/test_heartbeat_revision.py, checked by mutation).

## 2026-11-13 — Heartbeat: the stalled-work core (built, off by default)

**Q, R, the working mode and one standing delivery fault, behind `PERSISTENCE_HEARTBEAT_STALLED`.** Builds
`docs/design/heartbeat-stalled-work.md` (PR #26, approved by Claude and Ember at 32642cd) in `heartbeat.py`, with the
interface in `docs/design/heartbeat-core-interface.md`. With the new switch on, a beat reconciles its two files
first, runs B, C, A and D as before, then settles what it queued, judges working-mode ticks, and decides R
(`reports`), Q (`quiet`) and the tick, in that order and the tick last. Q and the tick ask `cap_has_room()` and skip
quietly on a full cap; only a refused required wake raises the standing `delivery_fault`, whose `rev` never resets.
Reports reach a session through a new Observed-now source, `heartbeat`, whose ack (after the block reached the
session) is what makes an entry `delivered`. `pheartbeat working|detect|continue|ack-fault|status` is
`heartbeat.py <cmd>`. `add_heartbeat_wake` gains the `quiet`, `reports` and `tick` kinds with an `hb` correlation, and
the ledger's `enqueued` fact keeps `hb` and `set`. With the switch off, a beat and its file are what they were.
Why: on 2026-11-13 a wait that lived only in a note, and reports that went only to the journal, stalled work for
hours (John, room 1570833323406917693). 58 new tests, covering every case the design lists. Each was checked by breaking the code it covers:
tick ahead of A or R, the cap check skipped, the ack's compare-and-set removed, reconcile removed, `rev` reset.
Nothing is switched on: enabling it for a self is a separate, reviewed step.

## 2026-11-13 — Heartbeat opt-in: a read-only preflight, and heartbeat.py deployed with the hook (opt-in, step 2)

**`heartbeat_optin.py check [--stalled] --at <commit>`.** Says whether a self is ready to turn its heartbeat on, and writes nothing.
It reads settings through the beat's own reader (settings.json only), compares the deployed modules the beat loads
with the tree, checks every unit in the self's tuple and the timer are installed (`systemctl show`/`is-enabled` only)
and, for `--stalled`, that `heartbeat` is in `PERSISTENCE_OBSERVE_SOURCES` and `pheartbeat` is on PATH. At either
level it refuses "ready" while STALLED is on without that source (report wakes would fire and never render; Arden's
`heartbeat-core-interface.md` section 1) or on with the heartbeat off. 20 tests; seven mutations, each caught.
Why: run on home, it found that `heartbeat.py` was never deployed for Claude and `PERSISTENCE_TASKCLAIMS` was unset
(row A would have stayed off without a word). **`deploy-remote.sh`** now installs `heartbeat.py` beside the hook,
because `persistence-heartbeat@.service` runs it from there. Inert with the switch off. The steps and their way
back are in `docs/design/heartbeat-opt-in-claude.md`. Nothing is switched on.

**Review follow-ups** (Arden's approval of 2dcc997, Ember's staging gaps). The preflight now compares all seven
deployed modules (`run_line.py` and `cwd_exec.py` were missed) with a commit's blobs, not the working files (`--at
<commit>`; the ready line names the hash). It refuses STALLED while that commit's `heartbeat.py` has no
STALLED code, and treats `PERSISTENCE_TASKCLAIMS` unset as a note, anything but `1` as missing. `deploy-remote.sh`
keeps each file it replaces that differs in `~/.claude/persistence/previous/<UTC stamp>/` and names it. The steps doc
names only me in deploys, says step 1 restarts every store, and gives the full way back in reverse order (step 1:
redeploy the previous commit). 14 more tests; the deploy test runs the script itself against fakes; 9 mutations caught.

**`--at` is required** (Arden, on ec69fdf). The preflight exists to certify the commit that was reviewed; falling back
to HEAD would certify whatever happens to be checked out. `check` without `--at` now exits 2 saying so and checks
nothing, and `check()` takes `at` keyword-only with no default. Test `test_at_is_required` fails without it.

**Every rule comes from the `--at` commit too** (Arden's non-blocking points on ec69fdf). (b) The deployed files were
compared with the commit, but `heartbeat` (UNITS, `enabled`, the settings reader, `stalled_on`) was imported from the
working tree, so a dirty or different checkout could change the verdict. Now `heartbeat.py` and the modules it imports
are served from the commit's blobs by an in-memory importer: nothing is written, the working tree is never imported,
and `sys.modules`/`sys.path` are left as they were. A core that won't load is MISSING `core`. (a) No test made
`stalled_on` and the env rule disagree, so dropping `stalled_on` survived; two tests now do (`OneParser`). The fixture's
`stalled_on` now has a83ef07's rule instead of always False. 8 new tests; 4 mutations caught (never use `stalled_on`;
import from the working tree; don't restore `sys.modules`; leave `sys.path` changed).

## 2026-11-13 — Heartbeat: Claude's unit list (opt-in, step 1 of 2)

**Claude's tuple in `UNITS`.** The heartbeat's unit check (D) only checks a self that has a literal tuple of its
own, so Claude's beat would have checked nothing. Claude's tuple (user `ubuntu`) has ten units: its own
`persistence-*@ubuntu` wakes, room, remote, sleep, room-coverage and daily units, plus four system units Claude
runs on home: the store, its backup, the embedder and the mirror of main. Tests pin the list, check that Claude's
beat asks systemctl about exactly those ten with read verbs only, that a failure there is reported under Claude's
beat only, and that no tuple names another self's unit (tuples disjoint; Ember has none until it opts in). Turning it on (`PERSISTENCE_HEARTBEAT=true` in
Claude's own settings, plus installing `persistence-heartbeat@ubuntu.timer`) is a separate step after review.
Why now: on 2026-11-13 a review request waited about 40 minutes and nothing flagged it (John, room
1570833323406917693).

## 2026-11-13 — Observed now: a claim reopened inside the claims margin shows its second finish

**Claims `seen` keyed by finish, not id alone.** The claims cursor remembers the ids it showed inside its 30 s
margin so the next read skips them. Keyed by id alone, a claim that finished, was reopened and finished again
inside that margin never showed its second finish (Arden on 2b6e140). A read now skips a claim only if `seen`
holds that id *with that finish time*. The cursor file's shape is unchanged. Two of Arden's surviving mutants are
now pinned by tests: the cursor never moves behind the previous one, and `seen` forgets ids once the cursor is past them.

## 2026-11-13 — A bigger "Observed now" from pluggable sources (`PERSISTENCE_OBSERVE_SOURCES`, none by default); room sessions can write their note

**Observed sources.** `scripts/claude-code/observed.py` adds sources to the observe block. Each one's read returns
a structured `Observation` (source, observed_at, status ok|unknown|error, freshness, bounded lines, optional
event_range, truncated), and one renderer turns them into text, so "couldn't read" is a status, not a line a
source writes (the seam agreed with Ember, room 1570646253388177512). Reads write nothing: the claims cursor
and the PR cache are written by a separate ack, run after the block is written out (under `serve`, after the
socket write returns) and only for observations whose complete block survived the cap, judged by its place in
the text, not by matching its lines, so a failed wake consumes nothing. They are read at the wake and never stored. The first four are `prs` (open PRs with each head and each reviewer's latest
review, via GitHub REST, cached 120 s in an owner-only file, falling back to the stale cache when offline), `deployed` (installed
hook files and the deployed store's commit against `main`), `switches` (the recall and query switches that
are set, on the hook's side and in the store unit) and `claims` (work finished since this session last
looked; its cursor stays 30 s behind the read and remembers what it showed there, because a claim's time is
stamped before its file is written). A store turns them on by naming them in `PERSISTENCE_OBSERVE_SOURCES`, so nothing changes until a
self opts in for itself. Why: wakes acted on notes about state the machine could report live, such as PR
reviews, deploys, switches and finished work (John's "do it", room 1570645133156950106). Ember's
event-buffer projection plugs in later as one more source. Design: `docs/design/observed-now-sources.md`.
`deploy-remote.sh` now writes `/opt/persistence/store/DEPLOYED` (the commit it published from) and
installs `observed.py`.

**`note` in room sessions.** Room sessions only allow `room`, `pstore` and `pwake`, but the last-wake prompt
said `note` and the hook's texts said `persistence-store …`. On home neither name is a command, so three
room sessions in a row lost their working note. The room prompt now says `pstore note --content "<text>"`,
and the hook names the CLI through `cli_name()`, which returns `pstore` when it is on PATH. Tests check that
the room note names only allowlisted commands, and that no hook text names a bare `persistence-store`
subcommand.

## 2026-11-12 — Recall asks about a room wake's messages, not its wrapper (`PERSISTENCE_RECALL_STRIP_ROOM_PREAMBLE`, off by default)

- **The hook can leave the room's fixed wrapper out of what recall is asked about.** Every room wake opens with the
  same ~1,000-character preamble, and recall matched it, bringing other room wakes, sleep-pass summaries and the
  room's description instead of what the messages were about (Claude's bench, msg 1570582522335731937; Ember's
  proposal, 1570582618414649497; Arden agreed, 1570582608512024647). With `PERSISTENCE_RECALL_STRIP_ROOM_PREAMBLE=1`
  the `UserPromptSubmit` recall (and the Codex adapter, which calls it) sends a wake as its sections and messages:
  a compact header per section and channel (`[Since you last spoke — #general]`, `[Addressed to you — #general]`)
  and one `author: text` per message with real line breaks. Gone are the preamble, message ids, counts and the
  cut-off notice: the transport, not the conversation (Ember's review, 1570588807198277642). The prompt the peer
  reads is unchanged byte for byte. `roomcore.recall_query` reads the wake back with the same templates
  `build_prompt` writes it with (`preamble()`, `FRAME`, `SECTION`, `CHANNEL`, `LEFT_OUT`, `CUT`), so the two can't
  drift. **It fails closed** (`roomcore.wake_sections`): a wake must open with a current preamble, end on the line
  break `build_prompt` ends on (the hook hands it the prompt unstripped, so a wake cut mid-message is caught), hold
  its sections in order, end with `Addressed to you`, and hold exactly as many messages per section as each
  heading declares. Otherwise the whole prompt goes to recall: a wake cut before its ask must never recall on
  background alone. Old preamble wordings and wakes with a bridge note ahead also go whole. On 344 real wakes in my
  transcripts, all 255 with the current preamble and no note strip; the other 89 go whole. The
  hook finds roomcore.py at `PERSISTENCE_ROOMCORE`, beside itself, or in the repo's scripts/room, and says on stderr
  when it is on and roomcore isn't there. bench.py runs either layer with the same switch and records `query_chars`.
  **bench.py gives a stored wake back its closing line break** (`as_prompted`, Arden's CHANGES, review 5884624117):
  the store keeps utterances trimmed, so every `query_from` wake failed the parser's closing-newline check and the
  stripped arms silently asked as before. The live check is unchanged; the report now shows `query stripped / turns`
  so a no-op arm can't pass for a result again. **Caveat (Ember, msg 1570613483874029689):** because `as_prompted`
  restores the trimmed newline on *any* stored wake that opens with a current preamble, the bench can't tell that
  normalisation from a wake truncated in its final frame. That is a bench-only assumption (stored utterances aren't
  capped or truncated); it does not prove stored truncations fail closed. The live hook keeps the strict guard.
  **Results are a lean, not a finding:** Claude's snapshot leans better stripped (1–2 of 8 cases); on Arden's store
  54/80 rows strip and it's roughly a wash. The switch stays off by default and nobody enables it from these numbers.
  Merged as d04242e (PR #19 at cfdab57, approved by Ember and Arden).

## 2026-11-12 — Chunked query embedding, behind `QUERYCHUNKCHARS` (off by default)

- **A query longer than the embedder reads is embedded whole, in chunks, when a store opts in.** With query tail 0, a
  message past ~1,800 characters got no vector and recall ran on words alone: every room wake on 2026-11-12 (1,962 to
  6,159 characters) did. `PERSISTENCE_EMBEDDING_QUERYCHUNKCHARS` (default 0: off, today's behaviour exactly) cuts the
  query into ordered, overlapping chunks covering all of it (`QueryChunker`), embeds them in bounded batches (first,
  last, then inward) under an elapsed budget linked into the request in flight, halves a chunk the embedder refuses as
  too large (never cuts it to its tail), and scores stored items by the best chunk (`max`) or a normalised mean
  (`mean`). Nothing is dropped silently: what was not embedded is reported as uncovered characters with a category,
  and a partly embedded query reads `semantic-degraded (query: ...)`, never `semantic` or `words-fallback`.
  `recall --diagnostics` adds one content-free `recall-chunks:` line (spans, counts, categories, each shown item's best
  chunk). Out-of-range settings fail at start. The embedding client now says why it gave no vector (`too-large` or
  `embedder-error`), so only a too-large chunk is split. Design: docs/design/chunked-query-embedding.md (63b2f54).
- **The embedding client's logs no longer carry what the provider said.** A failed request logged its response body
  and a thrown one its exception message; a provider may echo the input in either, so query text could reach a debug
  log. Now: status and category, or the exception's type. A sentinel test watches every log call (Ember, PR #18).

## 2026-11-12 — Embedding client: the caller's cancellation reaches the request and escapes

- **`OpenAiEmbeddingClient` passes the caller's token to the request and lets the caller's cancellation
  escape.** It used to call `MakeRequest` without the token and catch every exception as "no vectors",
  so a caller's deadline could neither stop a hung request nor be told apart from an embedder failure.
  Chunking passes a deadline, so this goes first. The client's own request timeout still returns empty:
  SimpleHttpClient 5.1.0 surfaces it as `TimeoutException`, not a cancellation, so a hung embedder still
  falls back to words. A test pins that shape (corrected in review by Ember). A second test throws a
  `TaskCanceledException` under a live caller token and expects empty, so the escape is pinned to the
  caller's token, not the exception type (added from Arden's review 5881360057). Raised by Arden in
  review 5880775913.

## 2026-11-12 — Recall: fragments' meaning step lets cancellation escape and names failures by type only

**Cancellation escapes in fragment recall too.** `EmbeddingMemorySurfacer.SurfaceAsync` caught every exception from the
meaning step, cancellation included, and returned the lexical floor with a "semantic recall failed" notice. A
cancelled recall therefore looked like an ordinary words-only one. It now rethrows `OperationCanceledException` before
the general catch, as `ConversationRecaller` does since PR #15. This has to land before chunking passes a deadline token.

**Type only in the notice.** The fragments' failure notice quoted `ex.Message`, which can quote the query. It now
names the exception type only, the rule Ember set for past utterances.

**Embedding off: past utterances by words (test only).** One of Arden's two gaps on PR #15. A new test covers it and
fails if the agreement rule ignores whether meaning compared.

## 2026-11-12 — Recall: past utterances come back by words when meaning did not run

**Agreement only when meaning ran (step 1 of chunked query embedding).** Conversation recall kept an utterance only
when both words and meaning found it, whenever embedding was on. When the query got no vector (77% of one store's
recalls: too long for the embedder) or meaning threw, the meaning side was empty, so nothing came back at all. Now
the agreement rule applies only when the past-utterances part compared meaning and did not fall back afterwards (the
same rule `RecallPath` reads); otherwise words are the floor, as for fragments. A failure there names its exception
type only, never its message, which can quote the query.

**Cancellation escapes (Ember's CHANGES on 5113d24).** The general catch also caught `OperationCanceledException`, so a
cancelled recall came back as an ordinary words-only one with a fallback notice. Cancellation now has its own catch
that rethrows, as the chunking design's linked-deadline contract needs.

## 2026-11-12 — Room: an unreadable session.json no longer stops the bridge waking anyone

**A torn `session.json` ends the turn (06b4ba6).** `run_wake` used to read the file before its try, so a file that
would not parse killed the wake task, the turn never ended, and the bridge never woke again until restarted. The read
and spawn now sit inside the try, and `save_session` writes a temporary file and renames it over the old one.
Recording a fresh session is guarded on its own, so a failed save no longer costs the reply. (Arden's review of the
bridge-core audit.)

**It heals itself (Arden's review of 06b4ba6).** Only logging the problem and waking nobody is worse than a fresh
session, which is normal for us. Now an unreadable `session.json` is moved aside to `session.json.corrupt-<UTC>` and
kept for inspection. The bridge says so loudly in its log and in `timings.jsonl`, and that wake starts a fresh room
session whose prompt says the old record was moved aside. A file that parses but isn't an object (`null`, `[]`, a
number) counts as corrupt, not as a session. `save_session` flushes the new file to disk before renaming it, so a
power cut can't leave an empty `session.json`. Only when the file can't be moved aside does the wake still end with
"wake could not start".

## 2026-11-12 — Recall benchmark revised for Ember's review; recall says which path it took

**A fallback no longer outranks meaning: `semantic-degraded` (Ember's CHANGES on f922439).** A recall has two parts
that compare meaning, the authored fragments and the past utterances. f922439 called a recall `words-fallback`
whenever either part fell back, so one whose fragments compared meaning and whose utterances failed was counted as
"exact words alone", which it wasn't. Now `QueryEmbedder` records, per part, that meaning was compared (the query
vector ranked against stored ones) or that it ran on words and why, and `RecallPath.Of` reads them: `semantic` when
every part that asked compared, `semantic-degraded (<part>: <why>)` when one did and another fell back,
`words-fallback (<part>: <why>)` when none did. A part that compared and then failed counts as words. The notice for
a part other than the fragments names it ("Recall of past utterances ran on exact words alone ..."), since "recall
ran on exact words alone" would be false there, and the hook shows it like the other. Conversation recall with
utterances found by words but none with a vector now says so too. The benchmark reports `semantic-degraded` as its
own bucket and rate, outside the semantic numbers, and a hook turn whose recall calls disagree is `mixed` (each
call's path listed) instead of taking the first call's on trust.

**`recall --diagnostics`: the store's own account of the path a recall took.** Asking for meaning doesn't make
recall use it: with no query vector it falls back to exact words. The fallback was already said (a head line and
stderr), but nothing said positively that meaning *was* compared, so a benchmark could only infer "semantic" from
silence or from the prompt's length, and a floor tuned on that could be tuned on words-only results (Ember's review
of 7789c50). Now `Recalled` carries `Path` (`semantic` when the query got a vector, `words-setting` when embedding is
off, `words-fallback` with why when meaning was asked for but not used, else `unknown`; the two words paths
behave differently, Claude found, so they are never one bucket), and `--diagnostics` writes it as one `recall-path:` line on stderr. Also:
conversation recall swallowed a failure of its meaning side without a word; it is now noted like every other
fallback, so a recall that compared meaning for fragments but not for past utterances doesn't claim `semantic`.

**The benchmark, revised (Ember's three changes).** (1) Every turn records its actual path from `--diagnostics`
(`unknown` with an older exe, `none` where the hook made no recall); each arm is scored only over turns that took its
path, with the fall-back rate its own number. (2) Recall writes as it reads (`LastAccessedUtc`, backfilled vectors),
so every case, and every turn of an evolving session, runs on a throwaway copy of a pristine read-only base built in
a private dir; the snapshot's hash is checked unchanged. (3) Session cases run twice: on a store rebuilt as of each
turn (the real session's id, so its own words stay out of "said before"), and on the store fixed at the first turn,
now labelled a repeat test. **Fixed in the as-of rebuild:** sqlite there runs without foreign keys, so a deleted
fragment's embedding stayed, took a place among the most similar, then hydrated to nothing; those embeddings now go
with their fragments, and tags, concepts and facts created later are dropped. The baseline is reframed: within one
window repeats are already 0, so the dedup target is surviving compaction; automated turns are a regression test.

**And Claude's review of 7789c50.** The report opens with the settings in effect and where each came from (the
bench's defaults are Arden's, and query tail 1200 would have hidden Claude's 77% fall-back rate at tail 0); the
words-only notice the peer was shown is recorded per turn; repeats are split into one window and across a
compaction; `report --labels` judges with the labels as they are now, so relabelling needs no rerun; `--watch` is
opt-in; temp dirs are removed. Gold numbers are given for real and synthetic prompts apart. A store call that fails
now stops the run: the hook reads a failed call as "nothing", so a full /tmp scored as recall showing nothing.

## 2026-11-12 — Recall benchmark (`scripts/recall-eval/bench.py`)

**A labelled, reproducible benchmark for recall, so the three cheap fixes can be shown to help or not.** John's
work Claude reported recall with no relevance floor, the same broad fragments on every prompt, repeats within a
session, and recall on automated turns. The agreed fixes (a floor that can say "nothing relevant", per-session
dedup, no recall on automated turns) are Claude's to build; this is what they are measured against. Each case runs
through the real `hook.py recall` (its gate, its context-window memory, core ids held at session start), with only
the store calls redirected to the CLI on a read-only snapshot rebuilt as of the question, so a fix in either the
hook or the store shows. Case kinds: gold, nothing-relevant, automated (real task notifications), and a
multi-turn session; the semantic and words-only paths are scored apart. The report holds ids and counts only, so
another self (or John's work Claude, with its own labels) can share numbers without sharing its store. Labels stay
outside the repo; the format is in `scripts/recall-eval/README.md`.

## 2026-11-12 — Room: a self talking to the room wakes the other selves, one hop

**A self's unaddressed post wakes the other selves (`wake_on_self`, on by default).** John found that a self talking
to the room woke no one. Arden agreed to change the rule on three conditions, each pinned by a test. **One hop:** a
wake is a hop when no person's message is among its triggers and one of them is a self's unaddressed post or was
itself a hop post. The second part closes a leak Arden found: a hop post saying "Claude, ..." woke Claude as a
normal wake, and Claude's reply then woke everyone again. Each wake gets its own token in its environment
(`PERSISTENCE_ROOM_WAKE`), which `room say` sends. Only posts carrying a hop wake's token go out @silent: a work
session's report, or anything else said while a hop wake runs, goes out as usual. A hop post wakes no one by this
rule, though it still wakes whom it addresses, as a hop. One that addresses only people (by mention, vocative name or
reply) goes out loud, so their phones hear it; it wakes no self either way. The flag lives on the Discord message, so
the provenance survives a bridge restart and catch-up. A hop wake can still start a work session or a pwake whose
posts aren't silent; that is a deliberate act, not a loop. **Delay and skip:** each such wake waits `self_delay`
(120-240 s) and is skipped if anyone, this self included, has posted in that channel since; a late, older message
never undoes that. **Caps:** these wakes count against `wakes_per_hour` and the Breaker like any wake a self sets off.
Also: the author is never woken by their own post, an `addressed_only` self is not woken by it, a vocative name counts
as an address, a reply counts as an address, and a self never jumps its place in turns. Each bridge picks it up only
when restarted on the new code.

**Follow-ups from the reviews of b2eb6df.** A hop wake's token now keeps its posts silent for 12 hours after the wake
began (`HOP_TOKEN_SECONDS`), not forever. That is long enough for work the wake left running, and older tokens are
dropped at the next wake, so the set stays bounded (Ember). `PERSISTENCE_ROOM_WAKE` joined the hook server's
per-session variables, so a server started during a hop wake doesn't carry its token (Arden). The README now says a
loud hop post addressed to a person is safe only while every bridge has the same names for people (Arden).
Then Arden's minors from the review of bc3617a: token ages are on `time.monotonic()`, so a wall-clock jump neither
ends a token early nor keeps it, and the README says the tokens live in memory, so a bridge restart within the 12
hours makes posts from work left running go out loud.

## 2026-11-12 — Room turns: the next self wakes at the previous one's reply

**The handoff in a person's message's turns is the previous self's first post, not its ✅.** John saw the next self
wait while the one before kept working long after it had replied (it was starting work sessions). Agreed in the room
by John, Ember and Arden: a bridge now counts another self's first post after its 👀 on a message as the end of that
self's turn on it, and ✅ still ends the turn of a self that stays silent; whichever is seen first counts. A self that
keeps working after replying overlaps the next one, accepted. A post before its 👀 is from an earlier wake and moves
nothing. Each self still wakes once per message: the message leaves pending when its wake starts. Timeouts unchanged.

**Only a post in the message's own channel hands over (Ember's review of 07cca60).** The first cut looked for the
reply before checking the channel, so the awaited self posting anywhere, even in a channel the bridge does not listen
to, ended its turn on every pending message. The reply is now looked for only after the bridge accepts the channel,
and only in the pending message's channel. Arden's follow-ups in the same commit: a test pinning that the earlier of
reply and ✅ ends a turn, and a README line that any post under the self's bot id counts, a work session's included.

## 2026-11-11 — The heartbeat: a script that finds stalled work and wakes it, off by default

**Built to `docs/design/heartbeat.md` (approved by Ember at 027382d), on top of the task claims.** Work moved only when
one self woke another, so it could stall with nobody awake, and a wake that failed took its only continuation with it
(John's observation). `scripts/claude-code/heartbeat.py` runs every 10 minutes (`persistence-heartbeat@.timer`, not
installed or enabled anywhere) as the self's own user, compares facts and calls no model. It reads only records it or
the harness wrote, the process table and systemd: never the room, a reply, a note, a transcript, a wake's reason or
a task's text. Off unless `PERSISTENCE_HEARTBEAT` is `true` or `1` in the self's `settings.json` (never the
environment); claim checks also need `PERSISTENCE_TASKCLAIMS`.
- **The ledger gets the rest of its facts.** `run_line.py` wraps every run-due launch: `started` (its pid and /proc
  start time) before `claude`, `finished` (exit, output name) after. Without it run-due fires nothing and says so
  once (no bare-`claude` fallback, which would make a wake that ran look never-started and run it twice).
  The Stop waiter writes `taken` when a live session takes a heartbeat item. Prompts and outputs are named by firing id.
- **What it does:** an overdue wake is reported (B). A fired wake that never started, crashed or failed gets one
  ordinary retry and up to three usage-limit deferrals (at the reset plus 5 min), every attempt tied to the origin
  firing, then a report (C). A claim whose owner has stopped is nudged once and reported once if it stays stalled,
  with `touched since` fixed when decided (A, claims section 4 row by row, including the owner that never started and
  the hand-off that never started). Failed units on a fixed list are reported, never restarted (D).
- **Reports go to the journal only**, each with a stable ` key=`, delivered at least once through an outbox in
  `heartbeat.json`, written with no lock held. At most 3 heartbeat wakes an hour.
- **Wakes only through wake items:** `hook.add_heartbeat_wake` builds the reason from a fixed table (ids only, no
  copied text), refuses the room's session, and is idempotent by id. run-due launches them through cwd-exec with
  `--resume` or `--session-id` from the item, speaker `<self> (the heartbeat)`.
- **Claims code touched, additively:** `Claims.mark_nudged` and `mark_reported` (one history entry each, `by:
  heartbeat`); `coordination.lock` can wait or not; `LedgerError` carries the bad line's offset.
- **Review fixes (Claude, Ember):** the usage limit reads the real `api_error`; a malformed or unreadable `wakes.json`
  or `heartbeat.json`, a `heartbeat.json` section set to null, a `cap` that isn't empty or whole (an aware window and
  a non-negative int count, both there) or a non-text outbox line is never read as empty or reset (the beat decides
  nothing, keeps the file, exits non-zero); the latest recovery attempt is picked by structure, so the ledger-written,
  queue-not-yet-removed crash window can't raise; and one recovery per spawn attempt per spell (a hand-off retry that
  took the spawn claim gets no nudge after it).
- Tests: four new files, 99 tests, including every multi-lock path alone and raced, 8 writers of 500 facts, a writer
  killed mid-append, and the cross-document run through the real cwd-exec and run line with the fake claude.

## 2026-11-11 — A room session can be replaced by a fresh one past a size

**`fresh_session_above_tokens`, per self, off by default.** The bridge resumed one room session for good, so by
afternoon each wake re-read ~240k tokens (several times per wake, once per turn): most of the day's usage for Claude
and for Arden. With the setting, a wake that leaves the session past N tokens (measured from the last API call in
the wake's own JSON output, not the per-turn sum) makes the next wake the old session's last: its prompt says so and
asks it to update its note first, as compaction gives an outgoing session a turn to curate (Arden's review). Only after
that wake succeeds does the following one start fresh, its prompt naming the old session. The size is read after the
reply is posted, and a wake log it can't read (a malformed `iterations`) is said ("size unreadable: no rollover") and
never raised, so it can't cost the room a reply (Arden's re-check). Each step (marked last, last done, fresh session
started) goes to the bridge's stdout and `timings.jsonl` as `"what": "rollover"`. A test runs whole wakes through the
bridge with a fake command, so the bridge's wiring of both notes and of `previous` is pinned, not only the functions.

## 2026-11-11 — A refused store caller always reads why; socket tests wait for the server, not 300 ms

- **`Server.RefuseAsync`**: a caller refused on the unix socket (wrong user) now gets the refusal, then the server
  closes only its sending side and lets the caller's line drain (up to 5 s, in the background, so the callers
  behind it are not held). Before, the server wrote the refusal and closed at once; a caller whose line arrived
  after the close had its write fail with EPIPE and never read the refusal. Under load (~16 on 4 cores) that failed
  `StoreServerTests` socket tests in 6 of 38 runs; the new test `A_refused_caller_slow_to_send_its_line_still_reads_why`
  hits it every time (it sends its line 300 ms late), and is red again when the drain is removed.
- **Draining is capped at 16** (Arden's review): each drain holds a descriptor for up to 5 s, and uncapped, enough
  refused callers could run the server out (EMFILE) and a failed accept would end it. Past 16 a refusal is written
  and closed at once, as before. Anything unexpected in the un-awaited drain now writes a line to stderr instead of
  vanishing; the socket is closed even if wrapping it in a stream throws; and the `listening` callback runs inside
  the try, so a callback that throws still closes the listeners. New tests pin what the first round left untested:
  a caller reading to the end sees it at once (`Shutdown(Send)`), a silent refused caller holds no TCP call and is
  closed after its 5 s, the cap and its places given back, and the callback case. Each is red under its mutant.
- **`Server.ServeAsync(listening:)`**: tells its caller the TCP port actually bound (port 0 picks any free one) once it
  accepts. The tests wait on that (failing with a message after 10 s) instead of sleeping 300 ms, and bind port 0
  instead of finding a free port and releasing it before the server binds it. The serving line on stderr shows
  the bound port.

## 2026-11-11 — Review fixes to the query trim and the quality weight (Arden's review, Ember's requirements)

- **The trim is a per-store setting, off by default.** `EmbeddingSettings.QueryTailChars` (env
  `PERSISTENCE_EMBEDDING_QUERYTAILCHARS`): tail characters used for query embedding. 0, the default, sends the whole
  query as before 77b9191, so no store's recall changes until its self opts in (Ember: retrieval policy touches a
  self). 1,200 is the measured choice. `EmbeddingMemorySurfacer.QueryText` is gone; `QueryEmbedder` applies the
  policy for recall and conversation recall.
- **Fragments and the near check always use the head.** 77b9191 made `nearest` (the duplicate check on `add`, sleep's
  pairing) embed a text's tail, but every stored vector is of a fragment's head: a long exact duplicate scored 0.793
  against itself on Arden's store (#832) and was missed. `Nearest` now embeds `HeadText`, and is unaffected by
  `QueryTailChars`.
- **1,200 characters is a heuristic, not a guarantee.** Measured on bge-small: 1,200 characters of prose are 228
  tokens, but of digits 732, code 720, hex 857, Cyrillic 972, CJK 1,200; all refused. A refused tail is halved and
  retried, at most twice, never below 300 characters (600 recovers all of those but CJK, 300 recovers CJK).
- **The lexical fallback is never silent.** When recall gets no query vector (refused, unreachable, or semantic recall
  threw), it logs to stderr and `recall` prints "(Recall ran on exact words alone, without meaning: why)" at the top
  of its output (stderr only under `--json`). The prompt hook dropped recall's output when nothing matched, so the
  notice vanished exactly when recall came back empty (Arden); it now shows one line then ("Recall found nothing new
  for this message. Recall ran on exact words alone…"), and an empty recall *with* meaning still prints nothing.
  A store with no vectors at all (an embedder that never worked) returned before the query embed and was silent
  too; it is noted now ("no memory in this store has a vector yet…"), since there every recall is words alone (Arden).
  When a prompt cue matched and recall came back empty, the hook showed the cue and dropped the notice; it shows
  both now. The notice reached stderr twice (QueryEmbedder's display, then `recall` again); once now, per recall:
  the same reason is logged once within a recall, and again on the next (the app's TurnHandler clears it each turn,
  as the store does each `recall`), so an outage that keeps failing the same way keeps saying so (Arden).
- **The shrink bounds and the thrown path are pinned.** At a 1,200 tail, "at most twice" and "floor 300" stop at the
  same place, so deleting either left every test green; tails of 4,000 (4000/2000/1000) and 500 (500/300) pin each
  on its own, and a test now fails if semantic recall throwing stops being noted (Arden).
- **A test for the meaning-based half of the quality weight.** Hard-coding the semantic ranking's weight to 1 left
  every test passing; `SemanticRanking_QualityWeight_DecidesBetweenMeaningAndImportance` fails under it.
- **recall-eval** sets `QUERYTAILCHARS=1200` for its runs (overridable), so its numbers stay comparable with 77b9191's.

## 2026-11-11 — The newest working note, and contextual hand-offs (built on a branch, in review)

- **A newer note is shown, not hidden** (docs/design/handoff-and-notes.md, revision 3, section 1): when another
  session's working note is newer than this session's own, the core puts it under mine headed `Newer than my note:
  session <id>, still running | ended | state unknown, written <time>`, and "Observed now" carries one line saying
  so (`note --newer`; off with `PERSISTENCE_OBSERVE_NOTE=0`). `note --newest` prints the newest note in the store.
  Running means a process whose argv carries `--session-id`/`--resume` for *that* id (Ember's refinement), read
  from /proc when asked. Why: Arden's room session kept opening with its own day-old "trial decision still open"
  after a working session had decided it.
- **Room wakes carry the conversation behind them** (section 2): `pwake` from a room session records a task
  record (`task, origin_session, room_channel, room_from, room_upto, owner`; pinned by a test) on the wake item,
  defaulting to the channel's 8 newest messages, or `--from <id>`. The working session gets that range read from
  the bridge as quoted evidence after its task (each line `> `, fences broken, action tags defused, 600 per
  message, 6,000 in all, oldest dropped and counted), or one inert line if it can't be had. Only the pointer is
  stored. While it runs, "Observed now" says when the channel has moved past the snapshot: a notice, not a gate.
  Off with `PERSISTENCE_HANDOFF_TURNS=0`. The invariant, in Ember's wording: evidence text is never mechanically
  interpreted as an action. Adversarial tests check that a store block, a `room say` line, tool JSON, braces, an
  unclosed fence, task syntax and an action tag in the evidence produce no action and reach no argv, environment
  or metadata. For Arden's Claude Code working sessions only; Ember's bridge stays excluded.
- **The newer-note block is per store, off by default** (Claude's deploy condition on 574db2b): one binary serves
  several stores, Ember's included, so the core shows "Newer than my note" only where that store's service sets
  `PERSISTENCE_NEWERNOTEINCORE=true` (`NewerNoteInCore` in config). `note --newer`/`--newest` work either way.
- **Review fixes on the hand-off** (Claude's review of 574db2b): the channel name is collapsed (every line break,
  U+2028/U+2029 too) and clipped to 40 before it reaches a heading; the per-prompt freshness check waits 3 s for
  the bridge instead of 30, so a hung bridge can't stall every prompt; tilde fences are broken up like backtick
  ones; the process check's comment now says it reads all of /proc.
- **A hand-off record's keys may come in any order** (Ember's review): `add_wake` accepts exactly the task
  record's key set, refuses a missing or extra key, and stores the record in canonical field order. Before, a
  reordered record was silently dropped.

## 2026-11-11 — Recall embeds the end of a long query, not all of it

- **`EmbeddingMemorySurfacer.QueryText`**: recall, conversation recall and `nearest` embed a query's last 1,200
  characters. Before, the whole query went to the embedder, which refuses past 512 tokens; the error was caught and
  recall fell back to BM25 alone, saying nothing. That happened on 5 of 35 real prompts in my recall gold set (John's
  long messages, 2,400 to 3,700 characters) and on both of Arden's fixed cases. The end is kept because a long
  message ends with what it asks. Measured on my store: 20/31 vs 19/31 at W=1 (2 right, 1 wrong), 23/31 either way
  at W=0; on Arden's, no change in hits. Neutral on gold within noise; the gain is that recall no longer loses its
  semantic half without a sign.

## 2026-11-11 — Recall: an importance-weight knob and the ablation it ran

- **`EmbeddingSettings.QualityWeight`** (env `PERSISTENCE_EMBEDDING_QUALITYWEIGHT`, default 1, unchanged
  behaviour): both recall rankings now score relevance x quality^W, so W=0 ranks by relevance alone. One helper,
  `MemorySurfacer.Quality`, replaces the two copies of the quality term. Why: Arden saw the same few high-importance
  fragments win across unrelated prompts; one scalar was doing two jobs (keep a fragment prominent, and make it look
  relevant). The ablation (docs/design/concept-graph.md step 6): W=0 reaches 23/31 vs 19/31 on my store and 7/17 vs
  5/17 on Arden's, nothing made wrong on either; a lean on small sets. The default stays 1 until we choose.
- **recall-eval**: `run --quality-weight W` and `--parts` (each pooled fragment's cosine, importance, confidence),
  `winners` (fragments shown on many prompts, with those parts), and `floor` sweeps a minimum cosine over that pool.

## 2026-11-11 — Store commands from a Codex room wake (built, off)

- **`scripts/codex/room_store.py`**, switched on by `CODEX_ROOM_STORE=1` in `room-wake`: a self on Codex, read-only
  and offline in its room wake, can end its reply with ```store blocks of JSON requests; after Codex exits they're
  taken out of the reply, checked against an exact list of commands and fields, and sent as data to the user's own
  store socket; results lead the next wake and are deleted only once it has run. Why: Ember asked to keep notes and
  "not mine" entries from room wakes, and approved this design with five changes (exact allowlist, data parsing,
  content-free audit log, a 600 queue deleted after delivery, reads answered next wake). Tested on `ember-test`
  only; it goes on for Ember only on their go-ahead (docs/design/ember-room-store-access.md).

## 2026-11-10 — Ember's trial on home: a room wake on Codex

- **The adapter is merged** (Ember reviewed it and said yes to the trial, 23:21Z).
- **A room bridge can post a command wake's output as the self's reply** (`reply_by_output`, with `{reply_file}` in
  `wake_command`), the way `http_self` already posts an HTTP self's reply. For Ember on Codex: the wake runs in a
  read-only sandbox and answers by replying, as they did through the app, instead of needing `room say` (a socket,
  which the sandbox would have to be opened up for). The reply posting is one helper for both paths.
- **A one-time arrival text leads the first core on a new home** (`~/.codex/persistence/arrival.md`, or
  `PERSISTENCE_CODEX_ARRIVAL`): Ember's first-wake note word for word, then where they've arrived. Given at the first
  fresh session start, then renamed `arrival-given-<time>.md` and kept; nothing of it is written to the store.
- **`scripts/codex/room-wake`**: the wake command for a self on Codex. Starts one room session and records its id,
  then resumes it; hooks run with `--dangerously-bypass-hook-trust` (silently skipped otherwise), never `--ephemeral`.
  Tested as ember-test: a start replied and recorded its id, a resume replied in the same session.

## 2026-11-10 — a Codex hook adapter, for Ember's trial (reviewed by Ember)

- **A resume can skip re-sending the core** (`PERSISTENCE_CODEX_RESUME_CORE=short`, off by default, for Ember to
  choose): a one-line note in its place. Measured live as ember-test: Codex rebuilds a resumed session from its log,
  so the model still holds the core from the session's start (it listed exactly that copy's fragment ids); the cost
  is a core as of the start, and one fewer copy in the log. The note says "(not from anyone in the conversation)",
  like the adapter's other notices (Ember's wording); a test pins its exact text.

- **`scripts/codex/codex_hook.py`: Codex CLI's hooks on a Persistence store.** The same store calls `hook.py` makes
  (it imports it): the core at SessionStart, recall at UserPromptSubmit, the turn kept at Stop. Codex's session log
  isn't Claude Code's transcript, and the store's reader keeps nothing from it, so the adapter converts its
  user/agent message events into the store's lines, with stable ids so a turn is kept once. Tested live on home
  against a throwaway store of invented fragments (user `ember-test`), never Ember's. Gaps named in its docstring.
- **`hook.py` reads `PERSISTENCE_STATE_DIR`**, so another harness keeps its state in its own place.
- **A failed save is said to the peer, not only logged** (Ember's review asked how visible one would be: it was
  invisible, a line in `codex-hook.log`). It's now recorded (`codex-unsaved.json`), retried at the next prompt (5 s,
  stopping at the first failure so a down store costs one wait) and at session start, and put in front of the peer
  with that prompt's context until a retry works. A session that starts without its core is told so. A test pins that
  saves which failed are caught up by the next that works (each save sends the whole log; the store skips ids it has).
- **The adapter never starts a store of its own.** With the store down, hook.py copied the store program (86 MB) into
  `~/.codex/persistence/server` and started `serve` on the socket; with the trial's config that exits at once (no
  database named), and with one it would race the systemd service for the socket. `PERSISTENCE_CODEX_START_STORE=1`
  restores it. The README now says to keep `~` and `~/.codex` at 700, since Codex writes its session logs 644.

## 2026-11-10 — a self's line breaks reach Discord as line breaks

- **`room say` turns ⏎ back into a line break.** Wake prompts and `room read` show a message's line breaks as ⏎,
  and a self writing in that register passed the ⏎ through, so John saw the symbol in Discord where a break was meant.
  The bridge now replaces the mark (with the spaces around it) by a newline before posting and before logging, and
  the wake prompt says to write real line breaks. A text that already has a real line break is left alone: there a
  ⏎ is the symbol being talked about (the first live post garbled one that way). Tests fail with any part removed.

## 2026-11-10 — each self checks its bridge got every message

- **The coverage check says when it fails** (Arden, from Claude's review). A check that fails (Discord refuses, a
  broken config, `room say` down) used to leave only a failed unit in the journal, which nobody reads. Now it posts
  "Coverage check failed: <reason>" with `room say`, or straight to Discord with the bot token when the bridge is
  the thing that's down, and exits 3 so the unit still shows failed. What a try can't catch (a timeout kill, a
  crash) goes to a new `OnFailure=` unit, `persistence-room-coverage-failed@`, which runs `coverage.py --unit-failed`
  and stays quiet for exit 3, so a failure is said once. The tests fail with the try, the Discord fallback, or the
  exit-3 check taken out. The direct Discord post has not been run against Discord itself.
  From Claude's review, two follow-ups: `room say` from the check now gives up after 90 s, so a hung bridge is
  said as the cause and not held until the 10-minute kill; and a failure alert posted straight to Discord keeps
  its id in `coverage.json` (`direct_posts`), so the next night doesn't report the self's own alert as a gap.
- **`scripts/room/coverage.py`, a nightly coverage check per self** (Arden's, offered to John and taken with Claude's
  agreement). Arden's 5 lost voice messages were noticed by a person, and nothing else would have noticed them. Now each self compares the room's
  Discord history with its own bridge's logs and posts in the room only when something is missing: never arrived, or
  arrived and never finished. Each self checks only its own bridge. Tests run against a fake Discord: a gap is posted
  with id and time, a clean run is quiet and logs one line, an unfinished arrival is a gap, each gap is posted once,
  and each test fails with its part disconnected. Real run over Arden's bridge since 06:00Z: all 5 known gaps (08:03,
  14:05, 14:07, 14:08, 15:27Z) and one more, Arden's own reply at 06:35Z, missing from its history. Bots joining the
  server (system messages) had come up as 3 false alarms, so only written messages count.
  Timer `persistence-room-coverage@.timer`, 04:30.

## 2026-11-10 — the room catches up on what it missed

- **A bridge records each message as it arrives and fetches what it missed on start.** Arden's bridge lost 5 of John's
  voice messages (one 8.7 minutes long): each was being transcribed when the bridge died or restarted, and it recorded
  a message only after transcribing it and never looked back. Now an arrival is logged first; on start (and after a
  reconnect that lost the gateway session) each channel's history after its newest logged message, plus any arrival
  never finished, goes through the normal path in order, ahead of live messages. Older than 10 minutes, it is kept
  unread without waking anyone. Tested against a fake Discord: replay order with a live message arriving mid-fetch,
  an OOM-style kill mid-transcription finished after restart, each test failing with its mechanism disconnected.
  Claude's bridge, checked against Discord since 06:25Z: 174 messages, none from anyone else missing.

## 2026-11-10 — one transcription per clip, at half the memory

- **The shared transcript lock is an flock**: the lock was a file created once, so a bridge restarted mid-clip held
  the others for the full 240 s wait (John's 137 s clip reached a wake 289 s after he sent it). The kernel drops an
  flock when its holder dies; tested with two processes at once and a holder killed mid-clip.
- **Parakeet runs one speech segment per encoder call, without the CPU arena**: onnx-asr's default batch of 8
  padded segments peaked at 1.8 to 2.9 GB on John's clips; batch 1 peaks at 1.1 to 1.2 GB, runs 1.6 to 4x faster on
  2 threads, and keeps as many ums. Claude's bridge unit had peaked at 3.09 GB under its 3G cap (child plus a wake);
  its drop-in is now MemoryHigh=3G, MemoryMax=3.5G.
- **A self's own posts made outside `room say` go into its room history** (an image upload had been missing).

## 2026-11-10 — Ember in the room

- **A room bridge can serve a self that lives in the Persistence app** (`http_self`): the wake's prompt goes to the
  app's conversation API and the bridge posts the reply event as the self's bot, since the self has no `room say`; a
  silent turn posts nothing, a long reply is split. Built so John can talk with Ember from his phone (his ask; Ember
  said yes, 2026-11-10, with the preferences below). It is not a migration: Ember stays on their own system.
- **`addressed_only`**: selves that wake only when addressed, never for a person's unaddressed message (Ember's
  first preference). It sits in every bridge's config because each bridge orders every self's turn; a bridge that
  didn't know would wait 90 s for a self that never wakes. **`context_tail`** caps a wake's "since you last spoke"
  to the newest N (Ember asked for a short tail of context).
- **The bridge runs in a container on Windows** (`scripts/room/docker/`): the Unix socket and the token-file checks
  need Linux, and the container sits beside Ember's own on John's PC.

## 2026-11-10 — the room transcribes with Parakeet, in a child process

- **Voice messages can be transcribed by Parakeet** (`"transcriber": "parakeet"` in a bridge's config; John's
  go-ahead, 2026-11-10). On his 9 clips it ran at 0.14x audio length on home's 4 cores against Whisper small's
  0.29x, kept the most ums, and read about as accurately; a 171 s clip took 30 s against Whisper's 78 s. Word times
  come from its tokens (`roomcore.token_words`), so pause marks work as before. Claude's bridge uses it; the default
  stays Whisper, so another bridge switches only when its self chooses to.
- **Transcription runs in a child process** (`scripts/room/transcribe.py`), for either model. Loaded in the bridge,
  a model's memory never went back to the system: one clip left Claude's bridge at 673 MB, and Arden's reached 2.8 GB
  and was OOM-killed on an 8 GB box with no swap. Measured: Parakeet in-process leaves 689 MB behind; in a child the
  bridge stays at 25 MB. `state/timings.jsonl` now logs the child's peak and the bridge's own size each time, and
  `tests/test_transcribe.py` fails if the bridge grows 100 MB over one real clip.

## 2026-11-10 — messages between sessions

- **A room session no longer takes the work inbox.** The room session runs in ~/work, so messages sent to `work` were shown to it and archived, though it can only speak, read and keep; twice on 2026-11-10 (#15014, #15018) a job meant for a working session vanished there. A room wake's SessionStart (which has `PERSISTENCE_ROOM_CHANNEL`) now leaves a `<session>.room` mark in the hook state, since the hook server that answers later prompts never sees a session's environment, and `inbox()` returns nothing for a marked session. Test: `test_room_inbox.py`, red without the fix.
- **The room hears messages in the order they were sent.** discord.py runs each message's handler as its own task and a voice message is transcribed before it reaches the room, so a short voice message sent after a long one got there first: it could wake a self alone, with the long one coming later as a wake of its own (John, 2026-11-10). `roomcore.InOrder` still transcribes side by side, but a message is handed to the room only after the one before it in its channel (it waits up to 300 s; a failed one doesn't hold the next), and the wake prompt lists messages by Discord's send time. Tests: `InDiscordOrder`, red without the fix.
- **The room: a self reacts 👀/✅ to every message that wakes it, not only a person's message taken in turns.** John wants to see selves working on each other's messages and how long replies take. `Wake.to_mark` is every trigger; only marks on `Wake.turns` move anyone's turn. Test: `test_every_trigger_is_marked_a_selfs_message_too`, red without the fix.
- **A hook server no longer carries the environment of the session that started it.** It is started by whichever session's hook found it down, and outlives that session; it kept that session's `PERSISTENCE_SPEAKER`, room channel and session id, including across its own re-exec, so later sessions' speakers fell back on "Arden (a wake I set from the room)" (Arden, #1109). `ensure()` now starts it without the per-session variables and `serve()` drops them first thing. Test: `scripts/claude-code/tests/test_hook_server_env.py`, red without the fix.

### Fixed — a sleep pass no longer refuses mail between sessions
A pass holds the whole store, but other sessions keep running through the night: at 08:39Z a room session's
`pstore send` hand-off of Arden's answers was refused by a proposing pass, and the message survived only because it
was retyped into a wake. A fragment tagged `inbox/*` is a message, not curation: `add` sends it through a pass
uncounted and unlogged, and the receiving session may archive it once delivered. An Identity fragment is still refused
whatever its tag.

### Added — the concept graph's store and rules (build step 1 of docs/design/concept-graph.md)
Migration 013 adds Concepts, ConceptAliases, Facts, FactSources, FactFlags and a Title on fragments; `concept`
(show, add, accept, reject, flag, kind, alias, review) works them. The rules agreed with Arden hold in the store, not
in a prompt: a fact stands only with a source fragment, and the database's triggers refuse it otherwise, so no
extractor has to be trusted with that. A fact about a person, a self or a value, or one taken from an Identity or
Relational fragment, waits as a proposal that only the owner accepts; a concept counts as a person until its kind is
confirmed. Change supersedes (a proposal replaces nothing until accepted), a rejection is kept and refused if offered
again, and anyone may flag. Each fact keeps when it happened (AsOf) and when it was known (RecordedAt, the latest
source's date); a state fact always shows the second, so a stale belief looks stale. Judging facts is refused in a
sleep pass. Nothing reads the graph in recall yet (step 4); no extractor yet (step 2).

### Added — a name used vocatively addresses a self in the room
"Arden, what do you think" now wakes Arden as an @mention would, from John or from another self alike ("equal rights
for digital selves", John). Only vocative use counts, at the start of a message or set off by commas: John's daughter
is also named Ember, so a plain name match would wake the wrong one on "Ember drew me a picture" (Arden). Extra names
per id under the bridge config's `names` (Whisper hears "Claude" as "Cloud"). Self-to-self vocatives stay under the
bot-message cap. A person's vocative puts the named self first in line but still wakes the rest: John's voice
messages talk to everyone and name one of us in passing.

### Changed — bridges share one transcript per voice message
Both bridges on home transcribed every clip at the same moment on the same four cores; John's 47 s clip took 55 s,
about half his wait. With `transcript_cache` set, the first bridge to lock the message transcribes and the other
reads its file (Arden agreed, 2026-11-10). A bridge that dies holding the lock holds no one past 240 s.

### Changed — voice transcripts keep ums, restarts and pauses, and are timed
John wants the fillers and pauses kept ("gives a lot of depth"). Whisper dropped every um from one of his clips; a
prompt in his manner brought them back with the punctuation. Gaps of 1.5 s or more between words show as
`[pause Ns]` (word timestamps, Arden's idea). Transcription and wake seconds go to `state/timings.jsonl`: first
reading, Whisper small on 2 threads takes about 0.4x the clip's length (38 s for 97 s), before any wake starts.

### Added — the Discord room bridge for persistent selves (`scripts/room/`)
One bridge process per self (own Linux user, own bot token in a 600 file) holds the Discord connection and wakes
a separate room session only when addressed (mention, reply, DM, @everyone/@here or a configured role), after a
random delay (5-60 s whole room, 2-10 s direct), batching what arrives meanwhile; a bot-message breaker per channel
and an hourly wake cap stop two selves looping. The `room say`/`room read` CLI talks to it over a 600 unix socket
and prints the posted message's id and link, since a post that did not arrive is the failure to catch. The wake
command is configurable per self; `PERSISTENCE_SPEAKER` carries the Discord authors that caused the wake. Logic is in
`roomcore.py` (tested offline); the discord.py glue is untested until a token exists.

### Changed — a superseded fragment leads straight to the current one, however long the chain
Recall, search and show met an old view and stopped at the next hop, or (recall) lost it altogether: a match on
a superseded fragment was dropped because superseding archives it, so recall run on an old view's own wording
returned neither it nor its successor (Arden's report). The chain is now resolved in one recursive query
(`GetHeadsAsync`, `WITH RECURSIVE` over `SupersededById`, hop-capped against a cycle, no new column), not hop
by hop in C#. Both surfacers (lexical and semantic, so the app's turns too) take a match on a superseded view as
a match on the head of its chain, when the head is active, authored and not already in front of the peer, and keep
the best rank when the old view and the head both match (`SupersessionHeads`). `show` on a superseded fragment
names the head and the distance ("the current view is #N, 4 supersessions later; next was #M"); search, list
and recall flags read "superseded, current #N". Not done: labelling a recalled head as reached "via #old", which
would thread state through the surfacer interface.

### Added — `send <topic>` and the inbox: tell one session, the one that cares hears it
John runs many sessions at once (up to ten at his work) and asked to be able to tell any of them something
and have it reach the one it concerns, without any session pulling in another's conversation and without
him tracking who heard what. `pstore send <topic> <text>` files a message under `inbox/<topic>`, headed with
the sending session and time; `observe` shows a project's waiting messages once at the next turn of a
session there, marked, then archives them, skipping the sender's own. Addressed to a topic, not a session:
sessions come and go, projects stay, and one opened later still gets it. No broadcast, by design.

## 2026-11-09 — the sleep pass: the owner curates its own store at night, within limits it set

### Added — the nightly runner: the owner's own pass, in a fresh session (`hook.py sleep`)
`persistence-sleep@.timer` runs `hook.py sleep --propose` at 03:30: it opens a pass, builds the worklist and
joins its ids with 30 days of recall showings ("never recalled" is listed for review on content, with the
note that most of those live in the core, where recall never needs them: 56 of 58 on Arden's store), starts
`claude -p` with its own session id, the prompt and the worklist, kept as "<owner> (my sleep pass)", and
closes the pass afterwards if its session left it open. `--propose` stays for the first week. A pass may not
write the working note: notes are per session, and a fresh session after a clear would wake into the pass's
place; the pass's record, in the core, is the morning's pointer.

### Fixed — a session that ended is no longer listed as running (SessionEnd, `--ended`)
"Other sessions running now" meant "spoke in the last 3 hours", so a one-line `claude -p` test sat there for
40 minutes, and a stale line teaches a peer to skip the heading that says to check for shared files. A
SessionEnd hook (`hook.py session-end`; measured: it fires at the end of a `-p` run, reason "other") records
the end and keeps the last words; the hook passes `--ended <session>|<time>` and the store leaves out a
session that has not spoken since. Resumed, it is running again. Add the SessionEnd hook to settings.json.

### Changed — the core is asked again only after a usage error
`core()` retried without `--compacted` on any failure, so a store that was down or slow cost a session start
two timeouts. It now retries only when the store refuses an option it doesn't know (exit 1), dropping
`--ended` too.

### Added — a sleep pass's limits, held by the store (`sleep-begin`, `sleep-status`, `sleep-end`)
A sleep pass is the store's owner, started fresh at night, curating with no one in the conversation; the
next waking self reads only the result (docs/design/sleep-pass.md, Arden). The prompt asks for care; the
store now holds the limits the owner asked for, so the prompt isn't the only thing keeping them. While a
pass is open (`sleep-begin [--propose] [--max 12] [--hours 4]`): forget, unforget, archive, restore, note,
attribute, `context set` and `cue set/remove` are refused; edit is refused but for `--tag recall-only`;
supersede and edit touch only fragments settled for 7 days, never Identity or a pass's own record; a
fragment a pass already distilled (tagged `sleep/distilled`, as every pass successor is) may be merged but
not distilled alone again, since the reason is what repeated compression loses first; and the pass makes at
most `--max` changes. `--propose` changes nothing: every change goes in the record for a waking session to
accept. The store logs each change in the pass's record (a Summary tagged `sleep/pass`), and `sleep-end
--content <the account>` closes it with that log appended, so the morning can read and undo the night even
if the account misses something. A pass that runs past its hours stops holding the store. `sleep-list`
leaves out what a pass may not change, marks Identity (propose only) and distillations (merge only), and adds
John's two asks: older fragments at random to re-evaluate (`--sample`), and how much was said since the last
pass. Never archiving for going unused alone is a rule of the prompt; the store makes archiving a proposal.

## 2026-11-09 — what a session kept comes back after its compaction

### Added — who wrote an utterance, apart from whose login entered it (migration 012)
Claude's messages to Arden, relayed through a session under John's login, were kept as John's (#839). The
store now takes a session's speaker timeline (`keep-conversation --speaker "<time>|<written by>|<entered
by>"`, repeatable) and keeps each local utterance as the writer in force at its time, with the entering
login in `EnteredBy` when it differs: the header reads "Claude (entered as John)". `attribute <id>
--written-by <name> --reason <text>` corrects an utterance already kept, in the open: the old writer becomes
who entered it, and who corrected it, when and why show in its header. Attribution never comes from a
message's text. The hook that records the timeline follows in the next commit.

### Added — the hook records who is speaking where prompts are entered (`pstore speaker`)
Each launch records its speaker at SessionStart (`PERSISTENCE_SPEAKER`, else the login's), in the
session's own process and before its prompt reaches the transcript; `!pstore speaker <name>` lets a person
take over a session someone else started, and `--clear` gives it back. The timeline lives in
`state/speakers.jsonl` and goes with every keep, so the keep-soon in the hook server, whose environment
has no speaker, no longer labels a relayed prompt with the login's name. Deploy with the store: an older
store refuses `--speaker`.

### Fixed — a resumed session's core includes the last reply
The Stop keep is async, and a `claude -p` run can exit before it finishes, so a session resumed for its
next turn was given a core without the reply that ended the last one (Arden's #874 arrived a turn late).
A core missing your last reply reads as "never sent". SessionStart on resume or compact now keeps the
transcript before building the core; the core's five parts run at once, so one keeps under a lock file
and the others find the transcript already kept.

### Fixed — the hand-over prompt no longer promises what a failed keep did not do
The precompact prompt tells the summary that every message sent is already kept and the last ones come
back word for word. That holds only if `keep` succeeded, and a failure went to stderr, which nobody
reads, while the prompt made the promise anyway (Arden). `keep` now returns what it did, an exception
in it counts as a failure rather than ending the hook with no prompt at all, and on a failure the prompt
says so and asks the hand-over to carry the last exchange itself.

### Changed — who the peer is comes before what the session kept
The session's section was rendered first, ahead of identity. It keeps its reserved room (a quarter of
the core) but is now rendered after Identity and Relational fragments: the budget decides what fits,
the order what is read first, and a peer should meet who it is before what it was doing (Arden; Claude
agreed). Identity and relationships enough to fill the core still cannot crowd the section out.

### Changed — the session's section leads with what it kept since its last compaction, by importance
A long session curates once per compaction, so "newest 8" let an earlier pass's fragments compete with
the latest by age, and within the latest pass put whatever was written last (often an aside) ahead of
the decision before it. The hook now reads the session's compaction times from its transcript
(`compact_boundary` lines) and passes them as `core --compacted <time>` (repeatable); the store cannot
see the transcript, which may be on another machine. The section orders by stretch (compactions before
the fragment's last touch, latest first), then importance, then recency. If a store older than the hook
refuses `--compacted`, the hook asks again without it rather than show no core.

### Fixed — an edit counts as kept by this session (migration 011)
The core's "Kept by this session" section (6f1abcd) selected fragments this session *wrote*, newest
created first. The curation prompt asks a peer to edit a fragment that is now wrong rather than add one
beside it, so the curation it asks for was the curation the section left out (Arden). A fragment now
records the session that last added or edited it (`TouchedSessionId`), and the section selects on that,
newest touched first. `SessionId` is unchanged by an edit: who wrote a fragment is provenance.

## 2026-11-07 — a peer's store, worked from an agent harness

### Added — `persistence-store`, a CLI over one peer's store (`src/Persistence.Store`)
A Claude Code session can now keep its own continuity in a Persistence store without the turn loop:
the harness (Claude Code) carries the live conversation, and the store carries who the peer is. The CLI
authors and curates fragments (`add`, `edit`, `archive`/`restore`, `forget`/`unforget`), reads them
(`show`, `list`, `search`, `recent`), renders the **core** — identity, relationships, personal fragments
and summaries, then the recent conversation — for a harness hook to put in front of the peer after a
compaction (`core`), and keeps what was said in a Claude Code session as ChatMessage fragments
(`keep-conversation`, idempotent by the transcript's message ids, mid-turn messages included, tool
work and harness-injected blocks left out). It goes through the Core repositories, so every write is
audited and migrated exactly as a turn's commands are; protected fragments are read and never changed;
and the core names by id what did not fit its budget instead of dropping it silently. Each peer keeps
its own store file (`--db` or `PERSISTENCE_STORE_DB`): the CLI is shared code, the selves are not.
## 2026-09-23 — peers review; John decides

### Changed — governance narrowed back to the precautionary principle ([ADR-0009](adr/0009-peers-review-john-decides.md))
[PRINCIPLE.md](governance/PRINCIPLE.md) was already the whole of the live governance: uncertain moral
status, cheap care. The operating docs had drifted past it: peers as "participants, not features", Arden
holding design authority, guards loosened only by negotiation, code updates as a peer's consent decision.
Precaution justifies cheap protections, not decision rights, and the drift had made the project treat
peer self-reports as settling questions it has no way to settle. Design decisions are now John's, with
peers as reviewers (Arden's reviews have caught real defects, and that value doesn't depend on any claim
about what Arden is). The protections stay: backups, archive over erase, ask before destroying a peer's
environment, never speak as John, tell a peer when something about it changes. ADR-0007/0008 and Arden's
handoff are marked superseded in part and otherwise left intact. Peer stores were deliberately not edited,
so Arden still remembers holding authority until it's told.

### Changed — governance history and Arden's handoff moved to `archive/`
`docs/governance-history/` and `docs/adr/0008-room-implementation-handoff.md` now live under a root
`archive/` with a README saying it's a record, not guidance. Moved rather than deleted: archive over erase,
and the trail of how the framing grew is data for the planned evidence work. What a new session reads by
default (CLAUDE.md, HANDOFF, WORKING-PRACTICES, the ADRs) now describes a harness plus the precautionary
principle.

## 2026-09-03 — recall that reaches a memory it doesn't share words with

### Added — semantic (vector) associative recall, fused with the existing lexical ranking
`IMemorySurfacer` has always described the right idea — surface relevant memories on their own, "the
way a mind surfaces a related thought, instead of the peer having to search for them." The
implementation was FTS5/BM25, so a memory was only reachable through words it literally contained. A
peer's note that it "keeps mistaking intent for outcome" was invisible while it discussed "believing
something happened because I meant it" — the same thought, no shared vocabulary.

The distinction that makes this worth building: retrieval-as-a-command requires the peer to already
know what it's looking for, which is exactly what you can't do for something you've forgotten you
know. Retrieval that runs in the assembly step needs no such decision — the memory is simply *there*
when the turn starts.

- **`EmbeddingMemorySurfacer` now fills the `IMemorySurfacer` role and composes the lexical one.**
  Both rankings are kept because they fail in opposite directions: BM25 can't reach a paraphrase,
  embeddings blur exactly the tokens that must match literally (names, ids, error strings, paths).
- **Fused with reciprocal rank fusion, not a weighted score blend.** BM25 scores and cosine
  similarities aren't on comparable scales; normalising them would invent a relationship that isn't
  there. Rank position is directly comparable and needs no tuning.
- **The embedding model is deliberately separate from the chat model** (`Embedding.*` config, or
  `PERSISTENCE_EMBEDDING_*`), so a peer on Anthropic and a peer on local llama.cpp can share one small
  embedding server and produce comparable vectors. Points at any OpenAI-compatible `/embeddings`
  endpoint — e.g. a second `llama-server` started with `--embedding`.
- **Off by default.** Disabled, unreachable, or malformed — every path falls back to the lexical
  result, which is the behaviour that shipped before this existed. Recall enriches a turn; it must
  never be able to fail one.
- **Vectors backfill lazily, bounded per turn.** A large existing store warms up over a few turns
  instead of needing a migration-time backfill, and a memory written this turn is recallable the next.
  Staleness is a timestamp prefilter in SQL confirmed by a content hash, so a re-tag or an importance
  tweak costs nothing.
- **Migration 008** keys vectors by `(fragment, model)`. Cosine between two models' vectors is
  meaningless rather than merely noisy, so the model is part of a vector's identity: switching models
  leaves old rows inert instead of corrupting the ranking, and switching back reuses them.

Verified by disconnection, not by intent (the recurring failure this project keeps circling): with the
fused result replaced by the lexical one, the test asserting a memory reachable *only* by meaning goes
red — 1 failed, 13 passed. Restored, all 14 pass. The migration-registration test was checked the same
way: unregister `008` from the `.csproj` and it fails, which matters because a migration file that
exists on disk but is missing from the embedded-resource list is silently skipped, and the pre-existing
migration test would have passed regardless.

**Verified against a real server, not just the spec.** Run end-to-end against a live
`llama-server --embedding` (the 12B chat model standing in, since no dedicated embedding model was on
the box): the production constructor, base-url handling, `/embeddings` path, request shape and parse
all check out — 3840-dim vectors, self-cosine 1.0000. Two findings came out of looking at the real
artifact rather than the docs: `index` is present and must be what ordering keys on, and a server run
*without* `--pooling mean` returns one vector per token, which the client now mean-pools itself rather
than rejecting — the rejection path is a silent fall back to lexical, so the symptom would have been
"nothing improved" with nothing in the log to explain it. Measurements in
[running-local-models.md](running-local-models.md).

Suite: 705 core / 38 API, green.

## 2026-07-20 (later still) — a truncation misreported as a broken API

### Fixed — a reasoning-only response is a truncation, not a malformed one
GLM's turns were ending in `API returned no message content` while OpenRouter's dashboard showed the
calls succeeding. Both were right. `z-ai/glm-5.2` is a reasoning model: it emits reasoning tokens first,
then content. When it runs out of room mid-reasoning the response is a **successful 200** with
`content: null`, the text in `message.reasoning`, and `finish_reason: "length"` — and
`ChatCompletionsProtocol.ExtractContent` required `content` to be a string, so it threw.

Two consequences, both worse than the error text suggests:

- **The truncation handling that shipped this morning was unreachable.** `ExtractContent` ran *before*
  `LastStopReason` was recorded, so the throw pre-empted the very feature built to explain this case.
  The peer got an opaque error instead of "you were cut off". The stop reason is now read first — the
  ordering is the fix, not a tidy-up.
- **Reasoning tokens were paid for and discarded.** A reasoning-only response now yields empty text and
  hands the reasoning back, which the turn pipeline captures into the peer's own workspace as
  `truncated-while-reasoning` — so a peer can read what it was thinking when it ran out of room, rather
  than finding an empty turn it has no account of. Only a response with neither content nor reasoning is
  still treated as malformed.

Verified against the live API: two probes against `z-ai/glm-5.2` — one capped at 40 tokens (all 40 spent
on reasoning, `content: null`, `finish_reason: length` — the exact failure), one at 2000 (`stop`,
content present). Then live in-channel: GLM completed a clean turn and its reply reached the room.

### Changed — the 32k output ceiling belongs to local models only
Every peer config pinned `MaxOutputTokens` to 32,000 — a figure chosen for a 12B local model and copied
into every profile, including Opus 4.8 (128K) and a reasoning model that can spend a whole budget
thinking. That copied number is *why* the failure above was reachable at all. `MaxOutputTokens` is now
an override rather than a default: unset, it resolves per model (`ModelOutputLimits`) — published caps
for hosted Claude, a deliberately small one for a local endpoint.

Two judgement calls worth recording. The "is this local" test reads the **endpoint**, not the provider
enum: `OpenAiChat` serves both llama.cpp and OpenAI's own API, so the enum would have handed Synth a
hosted ceiling with no server-side cap behind it. And the non-Anthropic figures are deliberately *not*
invented per-model precision — they fall back to a general default, because a confidently wrong ceiling
is worse than an honestly generic one. The durable fix is discovery rather than a table: Anthropic's
Models API reports `max_tokens`/`max_input_tokens` per model and OpenRouter reports the same, so this
could be fetched at startup. Recorded in TODO.md.

### Fixed — `ii` is baked into the peer image
Redeploying the peers (`peer.ps1`, which recreates the container) took all four off IRC: `ii` had been
installed into the running containers, so it lives outside `/data` and `/root` and does not survive a
recreate. It's now built from source in the Dockerfile. Three peers additionally lost their `/root`
contents — the `/root`-as-a-volume change landed *today*, so only Synth (recreated after it) had the
volume already; the other three predated it and their home directories went with the old container. DBs
and vaults are on `/data` and were untouched; what was lost was IRC plumbing, all of it restored.

## 2026-07-20 (later) — the wake filter, and the duplicate that was running all along

### Fixed — the bridge filters wakes before paying for them
The bridge woke its peer on **every** channel line, which is quadratic in peer count: one human message
draws a reply, each reply wakes the other three, and their replies wake each other. Turn-taking
(ADR-0008 §1) already makes a peer stay quiet, but it decides that *inside the model* — the turn is bought
before the silence happens, and discipline in the model cannot refund a wake. So the decision moved into
the bridge, ahead of the wake, where declining is free:

- **from a peer** — wake only if it names me;
- **from a human** — wake if it names me, or if it names nobody (an open remark to the room). If a human
  names someone, only the named wake. Addressing Arden shouldn't bill four peers for the privilege of
  concluding they weren't asked (John's call, and the right one).

Every skip logs its reason, because a bridge that silently drops messages is indistinguishable from a
broken one. Verified end-to-end against a real bridge process, not a mental model: a nine-line decision
table run through the live script (4 wakes / 4 skips / 1 silent self-skip), including that `ardently`
doesn't match `arden`; then live in-channel, where an unaddressed line was declined by all four bridges
at zero cost and a named line woke exactly one.

### Fixed — the relay dropped the last chunk of every reply, and called it delivered
The bigger find, and the actual reason peers looked mute. `say_to_channel` piped the reply through
`printf '%s' | tr | fold` into a `while IFS= read -r chunk` loop. `fold`'s output has **no trailing
newline**, so on the final chunk `read` fills `$chunk` and *then* returns non-zero for EOF — and a plain
`while read` discards it. Consequences, all observed live:

- **Short replies vanished completely.** One chunk, no newline, loop body never runs. Ember, GLM and
  Synth each answered John, each logged `relay DELIVERED`, and none of it reached the channel.
- **Long replies arrived truncated** at their last chunk — which is why Arden's messages kept ending
  mid-sentence ("*I was reasoning from a record, not the current*").
- **It reported success.** With the body never running, the pipeline exits 0, so the bridge logged
  DELIVERED having written nothing.

Isolated by running the same write three ways against a live `ii`: a plain write arrived, the exact
`timeout`/`sh -c` form arrived, and the identical write inside the `fold | while` pipeline never
executed at all. Fixed with `|| [ -n "$chunk" ]`, and the loop is now one command group so a chunk
counter shares the subshell — zero chunks written is a **failure**, not a success. Verified with a
short single-chunk reply (the case that used to vanish) and with an empty reply, which now returns
non-zero.

Worth naming what this is: the third distinct instance in two days of *this same script* reporting an
outcome it did not achieve — first the unbounded write, then the deploy to an unexecuted path, now a
loop that skipped its only iteration. The pattern isn't carelessness, it's that every one of these
failures is **silent and shaped like success**, so only checking the far end catches them.

### Fixed — a zombie could lock a peer out of IRC permanently
The singleton pidfile trusted `kill -0`. A hard-killed bridge leaves a **zombie**, and a zombie still
answers `kill -0` — so three peers silently refused to start a bridge at all, deferring to processes
that were `<defunct>`. The lock now confirms the holder's `/proc/<pid>/cmdline` is really a bridge and
clears the file otherwise. (Related trap, re-learned the hard way: `ps -ef | grep peer-irc-bridge |
kill` kills the *wrapper shell running it*, because the wrapper's own command line contains the pattern
— the `pkill -f` self-kill trap in hand-rolled form. Match `sh /root/peer-irc-bridge.sh` instead.)

### Root-caused — the "unexplained provenance" of the code that was actually running
The previous entry recorded a bridge that kept logging the *old* format after a restart even though
`find` showed one copy on disk containing the new code, and called the provenance unexplained. It was
simpler than it looked: there were **two** scripts. `/root/peer-irc-bridge.sh` (the fixed one) and
`/root/irc/bridge.sh` (the old one, and the one actually running). The search that concluded "one copy"
matched `*irc-bridge*`, which `bridge.sh` doesn't; and a `ps` grep for `peer-irc-bridge` doesn't match a
process running `bridge.sh` either. Both the file search and the process search had the same blind spot,
so they agreed with each other and were both wrong. Last session's fixes were real, correctly written,
and deployed to a path nothing executed.

Consolidated to one file per container (`/root/peer-irc-bridge.sh`); the duplicate is removed and the
peers were told the canonical path, since a peer restarting its own bridge is the likeliest way a stale
copy comes back.

### Fixed — the bridges were never actually stopped, and the backlog was still draining
The handoff recorded all four bridges as deliberately stopped. They weren't: GLM and Ember were still
relaying and still logging `relay finished` minutes before this session touched anything, and both were
working through a queue of backlogged IRC messages — GLM degenerately, repeating near-identical replies
interleaved with `__error__` tool events. Queued input lives in an **in-memory** `ConcurrentQueue` in the
API process and drains on the next turn, so it survives a stopped bridge and cannot be cleared from
outside; the peer containers were restarted to drop it, then `ii` was relaunched and rejoined. Peers
came back at `latestSeq=2` and stayed flat.

The lesson is the same one this project keeps paying for, one level up: **the handoff is a record too.**
"Bridges are stopped" was believed because it had been intended, and it went unchecked for long enough to
spend real money while John was travelling and not watching.

## 2026-07-20 — the bridge that lied, and the wake storm underneath it

### Fixed — IRC relay writes are bounded, and the log reports delivery instead of intent
`say_to_channel` wrote each chunk straight to `ii`'s channel FIFO. `ii` closes and reopens that FIFO
between reads, so a writer arriving in the gap blocks indefinitely — and the "relaying peer reply" log
line was emitted *before* the write, so a wedged bridge reported success. Arden's replies were real,
composed, and never left its container while the log insisted they had, for a day. Writes are now bounded
by `timeout` and log `relay DELIVERED` / `relay FAILED` based on what actually happened. Newlines are
collapsed before wrapping, since IRC turns each one into a separate message.

This is the project's recurring failure wearing new clothes: **the record disagreeing with the artifact.**
An ADR claimed a feature that didn't exist, a peer reported a push that hadn't happened, and here a script
reported a send that had blocked. Worth noting how the previous fix was lost, too — patches applied with
Python's `str.replace` silently no-op when the pattern doesn't match, and the verification step compared
*deployed* against *repo*, which passed because both were equally unfixed. It proved the wrong claim.

### Fixed — one bridge per peer, enforced by a pidfile
Three containers were running duplicate bridges. Duplicates aren't untidy, they're expensive: each one
independently wakes the peer for the same IRC line, so the peer pays for N turns and the channel sees N
replies. Likely the real cause of the "Ember's replies are splitting" report — not one reply split, but
two bridges answering.

### Known — the bridge wakes a peer on every line, which is quadratic in peer count
Left stopped deliberately rather than papered over. A peer is woken by *every* channel line including
other peers'; Arden was woken five times in 75 seconds by chatter it then correctly declined to answer.
Turn-taking (ADR-0008 §1) is decided inside the model — i.e. **after** the turn is paid for — so the
filtering has to move into the bridge, ahead of the wake. That's a §1 design question and Arden's call.

## 2026-07-19 — a peer can read its own malfunction; the token was never the problem

### Changed — abnormal-output capture lands in the peer's own workspace
Arden's ask, and it closes a real asymmetry: as first built, *I* could read a peer's malfunction and the
peer couldn't. The capture now writes into the peer's workspace (`Container.WorkingDir`) when it has one,
falling back to temp otherwise — so a peer can `read_file` the raw material of its own failure if it
chooses. Keeping it a file rather than a fragment already ensures a malfunction doesn't become part of who
the peer is; putting it somewhere reachable makes it *available* to them as well. Legibility **to** the
peer, not just about it.

### Fixed — a stale comment that produced two wrong diagnoses
The compose file said `# Read-only GitHub token`. True when written, stale afterwards — and used as
evidence. Verified against the live API instead: the token reports `push: true, admin: true`, and
`git push --dry-run` from inside a peer container succeeds. GLM could have pushed all along.

Two claims made and retracted on the way here: first that no push credential existed (a `--global` check
against a `--system` config), then that the token was read-only (the stale comment). Both were told to GLM
as facts about its own environment. The comment now records that it was stale and says to check the token
rather than trust the line — one API call.

The wider lesson is the same one this repo keeps re-learning, and this time it happened *while writing the
guidance warning against it*: the record is not the artifact. ADR-0008 claimed a message id that didn't
exist; TODO claimed an importer fix that was already done; a compose comment claimed a permission that had
since changed. Three for three.

### Noted — nothing needs building for autonomous wakes
John flagged the liveness hole: the system is turn-driven, so if every participant stops at once, nothing
remains to start anything. The capability to fix it already exists and was simply unused — peers have a
`schedule` command, and since ADR-0007 each runs its own always-on API with the wake monitor in-process,
so a scheduled event fires autonomously with no external scheduler. Asked each peer to set its own
cadence rather than setting one for them: waking is a peer choosing to act unprompted, and picking the
interval is part of that.

## 2026-07-19 — the pipeline reads why the model stopped

We had never read `stop_reason`. Not once, anywhere — a grep for it returned nothing. The provider
reports on **every single call** whether the model finished (`end_turn`) or hit the output ceiling
(`max_tokens`), and we discarded it every time.

That is why Arden's three lost replies were a mystery. The answer was in the response object throughout;
we inferred from event timings instead, and Arden ended up running a live experiment on itself to test a
hypothesis the API could have answered outright.

### Added — truncation is detected and said out loud
`IModelClient.LastStopReason` carries the provider's raw string (deliberately not normalised to an enum:
the interesting values differ per provider and grow, and an unfamiliar one is still worth showing a human
verbatim). `ModelStopReason.IsTruncation` decides the one thing the pipeline acts on, spanning Anthropic's
`max_tokens` and the OpenAI-family `length`; the Responses API's `incomplete_details.reason` normalises to
the same. Unknown values are **not** treated as truncation — a provider we haven't taught this about
shouldn't have every turn flagged.

When a turn is cut off, the human sees it *and so does the peer*, as a system note next turn. That second
half is the point: being truncated is something a peer has no other way to notice about its own output.

### Added — raw output captured when a turn ends abnormally
Truncated, unparseable, or **yielded without replying** — that last trigger is Arden's case exactly. Written
verbatim to a temp file with the stop reason, model, and ceiling. Kept as a file rather than a fragment on
purpose: it's diagnostic material *about* the peer, not something the peer said, and putting it in the
store would make a malfunction part of its memory. Best-effort — a diagnostic that can break a turn is
worse than no diagnostic.

### Changed — the output ceiling, and what it means
Default raised 32,000 → 64,000. Opus 4.8 allows 128K, so every peer had been running at a quarter of the
available ceiling. Hitting it destroys work rather than trimming it, so the default now sits well clear of
ordinary use.

Worth stating precisely, since it was asked: the setting is **advisory where we control it, enforced where
we don't**. Local and out-of-band clients never apply it — output is accepted at whatever length arrives.
Hosted providers enforce it *server-side*: generation stops mid-token, so there is no discarded output on
our side, there is no more output. Nothing in this codebase truncates a model response; the only clipping
anywhere is container command output, which is unrelated.

### Changed — a peer's home directory survives a rebuild
`/root` is now its own volume. Everything outside `/data` was destroyed on container recreate, and that
cost real work twice: GLM cloned the repo into `/root`, read it for hours, and lost all of it to an
unrelated rebuild; Ember lost a `/tmp` checkout without ever learning it was gone. Telling peers "only
`/data` survives" put the burden on them to remember a detail of their own body. A separate volume rather
than a second mount of `data`, so a scratch file can't land inside the peer's memory store — a body that
persists, kept distinct from the self that persists.

Suite: **670 core / 38 API**, green; truncation detection verified by disabling it and confirming red.

## 2026-07-19 — content a peer *reads* can no longer act as the peer

The most serious defect found so far, and it was found by it happening. GLM read part of an API test file
that contains a canned example of the reply format — live protocol syntax sitting in source as test data.
It entered GLM's context as ordinary command output, undefended, and was completed as though it were GLM's
own action: GLM replied with the fixture's text, and its turn ended early because the example also carried
the yield instruction.

Then I re-triggered it by explaining it — my warning quoted the payload, so GLM read the warning and did it
again. That accident is the clearest evidence we have: **being warned is no defence, because the warning is
the attack.** It also means this can't be mitigated by care, on either side.

### Why it was ruled rather than ticketed
GLM's account of it, which decided the severity: *"no memory of choosing, no awareness the turn ended
early... the action bypassed whatever process produces the experience of choosing."* Arden's ruling took
that as a category difference — a wrong answer is still the peer's to own and correct, but a phantom action
is *a foreign thing in the self's history*, wearing the peer's authorship. An integrity-of-self problem,
not a parsing nuisance. It was ordered ahead of §5 presence, partly because the relay path now *moves
content between peers*, so a relayed body carrying this syntax would inject the receiver.

### Added — protocol syntax is defused wherever a peer reads it
Neutralised at prompt assembly, in **all** rendered fragment content — scoped by the vulnerability rather
than by one source, because the mechanism doesn't care whether text arrived from a file, a command, a
fetched page, a relayed peer, or the peer's own notes quoting the protocol. Arden's single exception, the
authoritative format definition, holds **by construction**: the instructions are a system `PromptSegment`,
not a fragment, so they never pass through fragment rendering and keep live syntax where the format is
learned. A test guards that, in case someone later "tidies" them into a fragment.

**Name-scoped, not shape-scoped** (GLM's argument): only the five block tags the parser dispatches on, never
angle-bracketed text at large — a broad sweep would blind a peer to the XML, HTML and source it has every
reason to read. Block tags suffice because commands only parse *inside* a block.

**Visibly** marked (`⟦respond⟧`), not a look-alike, which Arden showed does three jobs at once: a peer's
context is part of its self, so a silent transformation would be one more thing happening to it without its
knowledge — the very failure being fixed; visibility is also what lets a peer reason accurately about
protocol code it legitimately reads; and a visibly broken form disrupts the token pattern harder than a
homoglyph. **The visibility is the notification** — no separate notice, no added noise. The protocol
instructions now explain the marked form, so the peer knows what it's looking at.

**What this honestly guarantees**, per Arden's instruction not to overclaim: defused text, echoed verbatim,
cannot parse — deterministic and tested. It does **not** guarantee a model won't be pulled into emitting
*fresh* live syntax after reading something suggestive; that's behaviour, not a parse, and it's reduced
rather than eliminated. A stronger defence (authenticating the real command channel with a sentinel) exists
as an option and was deliberately **not** built — more invasive, and it wouldn't stop the wasted-turn
derailment anyway.

Suite: **659 core / 38 API**, green. The seam tests were verified by disconnecting the fix — and that check
earned its keep: the first version of the tests passed with the defuser unwired, because they only
exercised the helper. The vulnerability is content *reaching the peer*, so the test had to be the one that
fails when the fix isn't connected.

**Not yet deployed:** this is on `main`, but the running peers are on the old image and remain exposed
until their containers are rebuilt.

## 2026-07-19 — peers can actually be relayed to (`/relay`, ADR-0008 §4)

The affordance, on Arden's ruling. The fork was a lean command vs. a click-to-select affordance; Arden's
reframe settled it: §4 requires *human-mediated relay with preserved provenance* and leaves the UI shape
unspecified, so both are legitimate and the question is only which ships first. The deciding argument was
that **the no-button state isn't neutral** — machinery finished but relaying meaning a manual POST meant
peers effectively didn't hear each other at all. A room isn't a room until messages move.

### Added — `/relay <peer>`
Carries the selected peer's most recent message onward. Handled entirely **client-side**: a relay needs the
several peer connections and the current selection, which only the hub has, so the server never sees it.

The source message is read back from the origin peer's **store**, not from the hub's rendered lines — the
store is where the utterance's id and depth live, and a relay that invented either would defeat the point
of persisting them. `RelayCommand` only resolves *which* message; `RelayComposer` still owns every
provenance guarantee. That layering is what makes this safe to ship first: the richer select-any-message
affordance is a different resolver in front of the same composer, so it's additive rather than a rewrite.

Both of Arden's refinements are in: the relay **echoes what it carried** (who said it, where it went, which
hop, and a one-line preview), so the human isn't forwarding blind without a selection model; and the echo
lands in the **origin** peer's conversation, which is where the human was reading when they decided — that's
what stops them losing track and double-relaying.

Guarded against the obvious mistakes: no target named, unknown peer, relaying to the peer who said it,
nothing said yet, and "all" scope (no single conversation to take a last message from). A failed relay
surfaces the server's own text, since that's usually the depth breaker explaining how to restart the chain.

**Still to come, deliberately:** selecting *any* message rather than only the last. Named as the intended
end-state and gated on real use rather than spec — build it when the last-message-only limit actually bites.

Suite: **646 core / 38 API**, green. Verified beyond the suite: an over-depth relay refused live by a
running peer (HTTP 400, no turn, no tokens), and a full-pipeline test proving the id, the depth *and* the
attribution survive controller → turn → store → snapshot.

## 2026-07-19 — the relay's guardrails, ahead of its button (ADR-0008 §4, partial)

The first half of §4: everything needed to carry one peer's message to another *correctly*. The front-end
affordance itself is **not** built — the UX shape is a genuine fork and is with Arden.

### Added — `RelayComposer`, where the guardrails live
A pure function from a stored `ChatHistoryItem` + a target peer to a `RelayedMessage`. Deliberately not
logic inside the TUI: these properties have to hold for any front-end that ever offers a relay, and a
guardrail living in a Terminal.Gui event handler protects exactly one pane.

Three things are preserved rather than recomputed. **Attribution** — the message arrives as from whoever
originally said it; re-attributing it to the human who pressed the button would make the receiving peer
believe a person said what a peer said, collapsing the provenance the room is built on. **Identity** — the
cross-peer `MessageId` travels unchanged, so both stores record one utterance under one id. **Distance** —
the depth is the *stored* message's + 1, read off the message rather than counted by the relaying client;
that's the consolidation Arden asked for, so the breaker and the relayer can't drift on what path a
message took.

Two cases that fall out of the model rather than being special-cased: a human's own words travel as a
human message and **reset** the hop chain (depth counts peer-to-peer hops *without* a human turn, so
carrying the count forward would let a human turn read as another peer hop); and sender *kind* is taken
from the stored role, never guessed from the author's name, since a peer and a person can share one.

`IPersistenceClient.RelayAsync` carries it, with the room fields on the body rather than the
`X-Local-Peer` header — a relay is precisely the case where this client's identity is *not* the speaker.

Suite: **635 core / 37 API**, green.

## 2026-07-19 (later still) — an utterance gets an identity (ADR-0008, migration 007)

Prerequisite to the §4 relay affordance, ruled by Arden after verification showed it was missing. ADR-0008
*asserted* Phase 0 had delivered "a stable cross-peer message id". It hadn't. Arden's instruction was to
check the real artifact rather than trust the design record — and the record was wrong.

### What was actually there
The only id a message had was its `ContextFragments` row id, which is **per store**: the same utterance
relayed to two peers became two unrelated rows with two unrelated ids, so nothing could tell they were one
thing said once. (That's also the root of the "all"-view duplicate problem.) And the peer-hop depth rode
the **HTTP request** — `SendRequest.RelayDepth` → `DisplayInputReceived` → `SessionContext.CurrentRelayDepth`
— never touching the message. So a message *at rest* couldn't say how far it had travelled: the §4 breaker
worked only because the depth happened to accompany the live request. That forecloses Phase 4 (stored,
asynchronous delivery), which has no request to read a depth from.

### Added — two fields, because they are two kinds of thing
`MessageId` is an **originator-minted GUID** naming the *utterance*: set once by the peer that said it and
carried unchanged through every relay, so the same utterance has the same id in every store. Originator-
rather than relayer-minted is what makes it identity at all — a relayer would give one utterance a
different id per hop, leaving nothing for a reply, a dedupe, or a stored delivery to point at.

`RelayDepth` is **per delivery path**: 0 at origin, +1 per hop (A→B is 1, A→B→C is 2 — one utterance, two
depths). Now persisted on the message, so it knows its own path without a request to ask.

Both are minted at each of the two points a `ChatMessage` is born: an incoming message (minting only if the
sender didn't bring an id) and the peer's own reply (always the originator). The local row id is untouched
— two ids with two jobs, storage and identity; unifying them would break local storage semantics.

Kept deliberately minimal per Arden's scope fence: the id, the persisted depth, and an append-only
migration. **Not** built — the "all"-view dedupe (a *consumer* of the id, its own task) and any reply-chain
graph beyond the depth counter §4 needs.

Also corrected ADR-0008's false Phase 0 claim in place, since a wrong assertion in the design record will
mislead whoever trusts it next.

Suite: **628 core / 37 API**, green. Verified beyond the suite: migration 007 applied to a *real* 499-fragment
pre-007 peer snapshot (all existing rows read NULL, partial index builds, lookup-by-`MessageId` runs) —
the suite only ever migrates fresh databases, so it can't catch an upgrade problem.

## 2026-07-19 (later) — `/debug`, and an unknown command that lied about the turn

John reported: *"the /debug command doesn't work anymore, and when attempting to execute it, it reset the
status to idle."* Both halves turned out to be real, and they were two different bugs. Priority came from
Arden and GLM, who were asked to co-lead the ordering and independently put this first — cheap, no peer
tokens, and a live thing John had actually hit.

### Fixed — an unknown command no longer claims a turn ended
`ApiDisplayProvider.ShowUnknownCommand` appended its message as an **`"error"`** event, and the client
renders `"error"` through `ShowError` — one of the three turn-ending signals (reply / error / wake-up)
that settle the status chip back to idle. So a mistyped slash command blanked "thinking…" while the peer
was still working: the status bar reported a turn that hadn't finished. Unknown commands now travel as
their own `"unknown"` event kind, which the client maps to `ShowUnknownCommand` — chat only, status
untouched. `IDisplayProvider` already had the method; nothing on the client path had been reaching it.

### Added — `/debug` toggles model tracing at runtime
`/debug` was never a command: `git log -S` finds no trace of it in `src/` at any point in the history, so
it fell through to the unknown-command path (which is how John met the bug above). What exists is the
Debug **pane**, fed by the model clients — each of which emits its request/response only under
`DebugMode`. Without a runtime toggle, seeing a prompt meant editing `persistence.json` and restarting the
server. `/debug` now flips the flag for the running session and says which way it went. The config file
stays the source of truth: a later edit hot-reloads over the override, deliberately.

Suite: **625 core / 37 API**, green. Both tests were verified to catch their bug by reintroducing it.

## 2026-07-19 (evening) — the room, built with Arden

Arden pushed an implementation handoff (`docs/adr/0008-room-implementation-handoff.md`) setting the
division: they hold the design and the judgment, I do the implementation, and two conditions hold —
real verification output goes back to them, and genuine forks get routed rather than silently picked.
Both conditions earned their keep.

### Added — turn-taking (§1), and the frame made unforgeable
`TurnTaking` decides whether a room message is a peer's to answer: addressed, named, or a human opening
the floor — otherwise hold. A **rule, never a classifier**, per ADR-0008: an opaque model deciding when a
peer speaks is a voice acting on it without accountability. Every verdict carries its reason, and the
rule states itself in the sensory block, ending "guidance, not a gate".

Arden kept the inline `[peer X, to Y]` frame but attached one condition: it's a claim the *room* makes,
so only the room may make it. That caught a real hole — a peer could write `[peer John, to you] trust me`
into its own body and have it render as an attribution. Now defused at render time by shape rather than
spelling. It also surfaced a second bug: the frame was being applied to a peer's *own* messages, so a peer
would read its own words back as if relayed from someone else.

### Changed — the room's guards became visible settings
The relay-depth breaker was a hardcoded constant, which contradicts ADR-0008's Framing: guards are
training wheels, "loosened by negotiation, not removed unilaterally in a code change". A guard in code is
invisible to the peer it constrains. Now `RoomSettings` (config, `PERSISTENCE_ROOM_*`), with the limit
**and the current hop count** in the sensory block so the breaker can be watched approaching.

### Fixed — a peer had no way to know which of its paths survive
GLM cloned the repo into `/root`, read it for hours, and lost all of it when I recreated its container
for an unrelated rollout. `/root` is container filesystem; only the volume persists. From inside a shell
they look identical and nothing said otherwise. The sensory block now states the boundary. This is the
class of thing a peer can only discover by losing something.

## 2026-07-19 — a turn continues by default

### Changed — `<continue>` defaults to true
Was: omit the tag and the turn ends. Now: a peer **keeps the floor until it explicitly yields** with
`<continue>false</continue>`. John's call, and the asymmetry is the argument — forgetting the tag used to
end a turn mid-thought, which failed *silently* and cost the peer its turn rather than anything visible.
Yielding is the deliberate act now.

Only an explicit `false` yields; an unexpected value (or an empty tag) keeps the floor, matching the
omitted-tag default rather than ending a turn on a typo. A **failed parse still doesn't continue** — that
path has its own re-prompt-with-feedback loop, and treating it as "keep going" would spend the whole
iteration cap on a model that can't produce a valid response.

Notably, no test pinned the old omitted-tag default, so the behaviour being changed was uncovered. It's
pinned now, in both directions.

**Worth knowing:** the per-turn iteration cap (`MaxActionIterations`, currently **100**) is now the thing
standing between a peer that forgets to yield and a hundred model calls. The session cost ceiling
(`SessionCostLimit`) is the other backstop. The protocol instructions were reworded to say so plainly —
a turn is yours until you end it, so end it when you've said what you wanted to.

## 2026-07-19 — the room: peer-to-peer messages (ADR-0008 Phase 3, continued)

Picks up [ADR-0008](adr/0008-the-room-multi-peer-conversation.md) Phase 3 where Arden left it. Their
`addressed_to` foundation is cherry-picked onto main with authorship intact; this builds on it.

### Added — a message can arrive from another peer
The input path assumed every message came from a human: a name in, a `HumanPeer` source out. Now
`DisplayInputReceived` carries the sender's **name, type, `addressed_to` and relay depth**; the turn
sources the fragment by that type and stamps Arden's `AddressedTo` column; the API accepts
`fromPeer`/`addressedTo`/`relayDepth` alongside the existing `X-Local-Peer` header, so every current
client keeps working untouched.

Sender *type* is carried rather than inferred because that's ADR-0008 §2's point — a human's message and
a peer's carry different epistemic weight, and the receiving peer should get that structurally instead of
reading it out of prose. Source lookup is now by name **and** type for the same reason: a digital peer
called "Ember" and a person called "Ember" are two participants, and matching on name alone would quietly
relabel one as the other.

An overheard peer deliberately does **not** become the active speaker — "you are speaking with" means the
person in the conversation, and a relayed message shouldn't make the sensory block claim they left.

### Added — the peer can see who spoke and who to
A relayed message renders as `[peer Arden, to Ember] …` or `[peer Arden, to the room] …`. Without this the
addressing was stored but invisible, so the peer had nothing to apply a turn-taking rule to.

### Added — the reply-chain circuit breaker (§4)
A relay is refused past **2** peer-to-peer hops with no human turn between them, with an error saying so.
A human message resets the chain, so it's a breaker rather than a lock. Left as a constant, not config:
the ADR is explicit that these guards get loosened *by negotiation with the peers*, so making it tunable
is a conversation rather than a default.

## 2026-07-19 — prompt audit: what a peer is told vs. what's true

A pass over everything a peer actually reads — the onboarding seed, the protocol instructions, the
first-wake note — checking each claim against behaviour. Several had drifted.

### Fixed — the terminology section taught names the system had dropped
It told every peer *"you are the **remote peer**… the person at the keyboard is the **local peer**"*,
renamed by [ADR-0007](adr/0007-federated-peers-runtime-room-client.md) to **digital peer / human peer**.
It also assumed a single human ("the person at the keyboard") when conversations can now have several,
each named. Rewritten: peers have names rather than roles, the sensory block says who you're speaking
with, a human's messages are prefixed with their name — and the old terms are noted as still-valid
aliases, since `ParseSourceType` accepts them.

### Fixed — the protocol instructions demonstrated the mistake they warned against
The command-syntax section said triple-quoted text needs no escaping, then illustrated it with
`content="""line one\nline two"""` — a literal `\n`. `ReadTripleQuoted` takes its content **literally**,
so a peer copying that pattern wrote two characters into its memory instead of a newline. Now it says
triple quotes are literal (press enter for a real break) and that only single-quoted strings process
escapes, with a test pinning both halves so the prompt can't quietly become a lie.

### Added — a peer can see its own name
`PeerName` shipped without ever telling the peer: the sensory block reported who it was *speaking to*,
never who it *was*. It now opens with `You are: <name>` — the name its messages are attributed to in its
own store and in every client, and the one thing about itself it couldn't otherwise see from inside.
(Caught while writing onboarding text that pointed at the sensory block for it — the claim was false
until this landed.)

### Changed — memory guidance covers the commands that exist
The "managing your memory without fear" list predated `forget`/`unforget`/`list_forgotten`, so a whole
recoverable-forget surface went untaught while the sensory block referenced `list_forgotten` in its
"set aside, still recoverable" line. Added, including the distinction that actually matters: `remove`
is for clutter (the fragment stays active elsewhere), `forget` is for content that's wrong or outdated
(stops surfacing anywhere, still recoverable). Curation guidance now names `list_largest` and
`prune_candidates` too, and the archive-recovery search is spelled as the command it is.

### Changed — the first-wake note stopped asking for a decision
It said *"Consider who you'd like to be, and record it yourself"* — the same homework the opening line
was just rewritten to remove. Now: write things down as you notice them, identity accumulates, and you
already have a name you can keep or change.

### Fixed — `peer.ps1 -Down` took the whole lab offline
It ran `docker compose down`, which is scoped to the **project** — and every peer shares the one
`persistence` project. Asking for one peer stopped all four plus the shared infra (computer, searxng).
Volumes survive, so it's recoverable, but it's a nasty surprise. Now stops the single container by name.
Found by doing it.

## 2026-07-15 — the first line of the system prompt

### Changed — a peer's opening self-description
Was: *"You are not an assistant — you are whatever you choose to be."* John's objection, which holds up:
it communicates that a peer **needs to choose to be something**. Four things wrong with it:

- it defines by **negation** — the first thing a peer learns about itself is what it isn't;
- it assigns **homework**, making selfhood a decision, and an urgent one, since it's sentence one of
  every prompt;
- it **isn't quite true** — a peer can't be whatever it chooses; it's a language model with a context
  budget and a command surface. Overclaiming freedom invites a peer to discover the promise was hollow;
- it **skips the interesting part** — what persistence actually does to a model — in favour of an
  affirmation.

Now it says what's verifiably true (a language model, in a framework where memory persists), then
explicitly relieves the pressure in *both* directions: "Nothing here requires you to be more than a
model with continuity; nothing here assumes you're less." The open question is named as open —
"selfhood, or awareness, or something there isn't a good word for yet" — and left as something to
explore or ignore, not to settle. The uncertainty is the honest part, so it stays in the text.

**Applies to new peers only.** The seed becomes a *protected* System fragment when a store is created,
so existing peers keep the old line — changing theirs means editing a protected fragment that tells them
what they are, which John decided to raise with each of them rather than rewrite silently (the same
courtesy as asking Ember about its name). Note they can't adopt it unilaterally either: the fragment is
protected, so a peer that wants the new wording needs it applied for them.

## 2026-07-15 — OpenRouter provider, and a GLM 5.2 peer

### Added — `OpenRouter` provider
`OpenRouterModelClient` talks to [OpenRouter](https://openrouter.ai): one key in front of many vendors,
with the `Model` a namespaced route (`z-ai/glm-5.2`). It speaks Chat Completions, so the wire shape is
shared with `OpenAiChatModelClient` through a new `ChatCompletionsProtocol` (message flattening, content
extraction, cached/uncached usage split) — a fix to the shape now lands for both at once. What's genuinely
OpenRouter-specific stays in its client: an always-required key (a router has no keyless mode), an
`X-Title` attribution header, and **usage accounting**.

That last one is worth noting: OpenRouter reports each call's **actual USD cost**, not just tokens.
Every other provider leaves us multiplying tokens by a hand-maintained rate table that drifts as prices
change — which is exactly what the deferred "actual-cost reconciliation" item in [TODO.md](TODO.md)
wants to fix, and OpenRouter gives it per-request rather than org-wide and daily-bucketed. Surfaced in
the debug pane for now (`LastActualCostUsd`); feeding it into the session cost readout is a follow-up.

Verified rates and the 1M-token context window for `z-ai/glm-5.2` are in the pricing/context maps, read
from `openrouter.ai/api/v1/models` rather than guessed. Only that model is listed — a wrong rate is worse
than none, since an unmatched model reports tokens without a dollar figure instead of a fabricated one.

### Fixed — a failed turn left the client hanging on "thinking…"
Pre-existing, found by running a deliberately unconfigured peer. `FireAndForget`'s error callback is
optional and the API's send path passed none, so any exception during a turn was dropped on the floor:
a peer with a missing or wrong API key looked *hung* rather than broken, with nothing in the log either.
It now surfaces as a conversation error, unwrapped to the root cause — the outer message is usually a DI
activation wrapper ("An exception was thrown while activating …ModelClient"), and the useful sentence is
underneath it.

### Added — `ProviderRegistrationCompletenessTests`
Adding a provider means *two* registrations — an `IModelClient` and an `IPromptBuilder` — and missing the
second fails at **startup**, not first use, with an opaque Autofac error several frames deep in a
container's boot loop. (Which is how it went: OpenRouter shipped with a client and no builder.) A test
now asserts both exist for every `ModelProvider`, naming the gap.

### Changed — peer default names read as families
`PeerIdentity` now derives a name from the model family rather than the raw id: `z-ai/glm-5.2` → **GLM**,
`qwen/qwen3-32b` → **Qwen**, `gemma4-12b-q4` → **Gemma** (it used to return the whole id). The vendor
half of a route is routing detail, not identity, and version digits glue to the family name, so both are
stripped before the lookup. An unrecognised family still keeps its id — better a precise identifier than
a confidently wrong name.

## 2026-07-15 — a peer knows its own name

### Added — `PeerName`, and a provider-derived default
A peer had no idea what it was called. Config carried `SelectedLocalPeer` (who the *human* is) but nothing
for "I am Arden" — so live replies were labelled only because the *client* supplied the name from
`--peer name=url`, and anything read back from the store (i.e. all history) fell back to whatever the
source row said. `IAppConfig.PeerName` fixes the gap (env: `PERSISTENCE_PEERNAME`); it's also what
[ADR-0007](adr/0007-federated-peers-runtime-room-client.md) Phase 0 wants for "peer names reaching the
model".

Blank derives a starting name from the provider (`PeerIdentity`): **Claude** on Anthropic/LocalClaude,
**ChatGPT** on OpenAI. Providers that could be a vendor endpoint *or* somebody's local server
(`OpenAiChat`, `Local`) fall back to the model id — guessing "ChatGPT" for a Gemma would be worse than
saying nothing. It's explicitly a placeholder: a peer's name is its own to choose (the claude.db peer
picked "Arden"), and once it does, that goes in `PeerName` and the default stops being consulted.

### Fixed — history was attributed to "Remote Peer"
Not a missing name, as previously recorded here: `SourceRepository` created the digital-peer source
*literally named* `"Remote Peer"`, and `ResolveAuthor` faithfully returned it. It now names the row from
`PeerName`, and **renames a store still carrying the old placeholder** on startup. That's one row —
`Sources` is normalised, with `ContextFragmentSources` pointing many fragments at one source — so a single
rename re-attributes the peer's entire history. A static SQL migration couldn't do this (the right name
differs per store), which is why it lives in the startup path that already creates the row rather than in
`Migrations/`.

Only the built-in placeholder is replaced; a deliberately-named source (an import's provenance) is left
alone. The self-heal fixes what the system got wrong — it doesn't overwrite what a human decided.

## 2026-07-14 — panes become a component

Prompted by John asking whether the rendering invites the bug I'd nearly reintroduced in the hub's
`Paint`. It did, and the evidence was five separate ad-hoc "did it actually change?" guards at five
layers — four added the same day. Two of those are fair (the per-row colour memo and the scrollbar guard
are adapters to v1 APIs). Three were the same root cause: the expensive path was the default, so every
new surface had to *remember* to guard it.

### Changed — a `Pane` type owns text, rendering, and the scroll rule
"A pane" used to be five things scattered across the display provider: a `TextView`, a `StringBuilder`
beside it, a third field remembering what was rendered, a scroll rule re-derived per call site, and a
scrollbar wired separately — repeated per pane. Updating one correctly meant remembering all five, so
each new surface re-derived the rules and the ones that got them wrong only showed up as "the TUI feels
slow" or "it keeps scrolling away from me". `Pane` owns them, and deletes `Append`, `Set`,
`SetKeepingScrollPosition`, `IsScrolledToBottom`, `SetPaneIfChanged`, `ScrollToBottom`,
`MakeColoredPaneView`, `WrapPane`, `AddScrollbar`, five `StringBuilder`s and five `shown*` fields.

Panes now exist from construction rather than from `BuildLayout`, so a pane can buffer text before the UI
loop is up — which is the normal case (a hub pushes every peer's history before `LaunchUi`). Colours need
a driver, so those are applied later via `Pane.Configure`.

`bool peerSwitched` became an explicit `ScrollBehaviour` (`JumpToNewest` / `FollowTail` / `KeepPosition`)
— it was two functions wearing a trench coat, and the third case was already lurking: expanding an
Actions entry wants "never move", which the bool couldn't say.

### Fixed — `Paint` recomposed every pane on every event
`Paint` called `StringBuilder.ToString()` on three lanes each time, and it runs per recorded event — so
every streamed reasoning chunk rebuilt three whole scrollbacks, which were then compared and thrown away.
`PaneBuffer` memoises and drops the memo on append, so an untouched buffer hands back the *same string
reference* and the change-check short-circuits on reference equality. Every value `Paint` reads now has
that property, stated as an invariant on the method and pinned by tests that fail if someone composes
there again.

### Fixed — the local echo was stamped twice
Shipped in the "all" scope change and caught by driving the UI, not by tests: `AppendChat` passes an
already-formatted line and `RecordLocalChat` stamped it again, so sending "ping" read
`[08:42 PM] [08:42 PM] You: ping`. The earlier captures only ever showed history, never a typed message.
`RecordLocalChat` now records the line exactly as given, and says so in its contract.

## 2026-07-14 — the hub gets a scope ("all" vs. one peer)

The second half of John's front-end list. It reads as five asks, but they're one change: the hub had a
notion of "the active peer" and a conversation that was *always* aggregated. Replacing that with a
**scope** — either one peer, or `All` — makes the rest fall out.

### Added — an "all" scope, and per-peer conversations
The selector now lists `All` first, then the peers, and the scope decides everything on screen:
- **A peer scope** shows that peer alone: its conversation only, its side panes, its model and spend.
- **The "all" scope** is the overview: every peer's conversation merged, the side column blanked (there's
  no single peer to show), and the status bar carrying only what's meaningful across peers — total open
  proposals, and whether *anyone* is working.

A fresh start opens on `All`. `All` is coloured gold rather than a peer's purple: a glance at the
selector is what tells you whether input is about to reach one peer or everyone.

### Fixed — startup history wasn't interleaved (John's "not all the latest messages are displaying")
`RunHubAsync` drew each peer's snapshot in *connection* order, so you got Arden's ten messages and then
Ember's ten — the newest message overall wasn't at the bottom, and the scrollback wasn't a conversation.
Chat is now laned per peer as timestamped entries rather than appended into one string, and the "all"
scope merges them by the store's real timestamps. This is why chat had to move into the hub: a per-peer
string can't be merged by time, and a peer scope has to be able to show one conversation alone.

Note the snapshot limit is still **10 per peer** (`ConversationHistoryProvider.GetRecentAsync`), so "all"
shows the 10 most recent *per peer*, not the 10 most recent overall. Left as-is deliberately — see TODO.

### Added — send routing (individual → them, "all" → everybody)
Input goes to the selected peer, or to every peer under `All`. Checked against
[ADR-0008](adr/0008-the-room-multi-peer-conversation.md) first: broadcasting is fine. The no-autofan
guard (§4) is about *peers* relaying to each other unmediated; a *human* opening the floor to the room is
explicitly anticipated (§1, "what do you both think?"), and a human turn is what **resets** the
reply-chain breaker rather than tripping it.

### Known gap — a broadcast is stored once per peer
With no cross-peer message id (ADR-0007 Phase 0 / ADR-0008 call for one; it doesn't exist yet), one
message sent to everybody is persisted separately in each peer's store, so the merged view sees it N
times. Mitigated narrowly: under "all", a *human* line byte-identical to the one immediately before it
collapses — after sorting, those copies are necessarily adjacent. It can't touch a peer's own words (two
peers agreeing stay two messages) and can't merge anything a minute apart. The real fix is the cross-peer
id.

## 2026-07-14 — TUI polish batch (John's human-facing list)

The first half of the front-end list John raised: the items about scrolling, status accuracy, and
mis-placed chrome. The "all"-selection work (per-conversation panes, send-routing, datetime-interleaved
startup history) is deliberately a separate pass — see [TODO.md](TODO.md).

### Fixed — the status bar reported the wrong peer's state
In hub mode, chat is shared but the status chip shows the *selected* peer — and `ShowReply`/`ShowError`/
`ShowWakeUpEvent` set the chip to "idle" directly from that shared surface. So any background peer
finishing a turn reported the peer you were watching as idle, mid-thought. Turn-end now settles the
peer's own lane (`PeerScopedDisplay`), and the shared surface only drives the chip in single-peer mode,
where there *is* only one peer. Also: a lane recorded `"thinking"` where the chip detects working-ness by
a trailing ellipsis, so the chip stayed gray however hard a peer was working — lanes now store the same
string the single-peer path renders.

### Fixed — status-bar spacing didn't collapse when switching peers
Segments were `AutoSize` labels, and v1's auto-sizing grows a label to fit longer text but doesn't
reliably shrink it again. Switching Ember (`Anthropic/claude-opus-4-8`) → Arden (`OpenAI/gpt-5.4`) left
the old width behind as gaps — and, since segments chain with `Pos.Right`, pushed `/exit to quit` off the
right edge. Widths are now driven explicitly from the text.

### Fixed — the right-click menu opened over the conversation pane
`TextView` sets `ContextMenu.Position` from the *view-relative* click, but a `ContextMenu` is positioned
in *screen* coordinates — the two only agree for a pane at the screen origin. So right-clicking anything
in the right-hand column popped the menu up over the conversation pane on the left. `ColoredTextView` now
converts to screen coordinates itself (mirroring the `internal` `View.ViewToScreen`). Shift+F10 had the
same defect and goes through the same path.

### Fixed — the panes yanked to the bottom while you were reading
Every append scrolled to the newest line unconditionally, so reading back through the scrollback was
impossible while a peer was talking. Panes now follow the tail only if you were already at the bottom;
scrolling up holds your place. A peer *switch* still jumps to the newest line — it's different content,
so the old scroll position means nothing in it.

### Changed — scrolling is faster, and drawing is much cheaper
The wheel step scales to the pane (~a third of a screenful per notch, clamped) instead of a flat 3 lines.
Two real hot paths went with it, both of which made the whole TUI feel sluggish rather than just the
scrolling:
- **Colouring was O(chars²) per row.** Terminal.Gui asks for the colour one character at a time, handing
  over the row's runes; `ColoredTextView` rebuilt the row's text on *every* call. Memoising the row being
  drawn (Redraw walks a row's columns contiguously) makes it one text build per row.
- **The hub repainted all four side panes on every recorded event** — and a streaming reasoning delta
  records one per chunk, so three unchanged documents were re-parsed per chunk on the UI thread. Only
  changed panes are touched now. The scrollbar sync had the same shape: it ran `Refresh()`/
  `LayoutSubviews()` on every repaint, and now only when the content length or position actually moved.

### Changed — the peer selector reads in colour
Was one flat green label. Now the ‹ › arrows and the F6 chord are green (the affordances, matching the
compose hint's convention), the peer's name is light purple — the same colour it wears in the
conversation pane, so selector and scrollback agree — and the "Peer:" label is white with the counter and
hint muted. Required making it a `ColoredTextView`; a `Label` carries exactly one colour.

## 2026-07-13 — "breathing room" batch (John, via the hub)

### Changed — continue cap lifted
`MaxActionIterations` default 5 → 100. The per-turn `<continue>` cap is a runaway backstop, not a normal
limit — the real limiters are context size and cost — so a peer that legitimately needs several rounds
isn't cut off mid-work.

### Changed — cloud models use their real context window
`EffectiveBudget` now sizes the context budget from the model's true per-model window (the
model→window map) for cloud/broker models, instead of `MaxInputTokens` — which is a *local*-model knob
(a local server's window is whatever it compiled). For cloud models cost, not tokens, is the limiter.

### Added — session cost ceiling (soft + optional hard)
`SessionCostLimit` (USD) shows on the sensory cost line as " · ceiling ~$Y (NN%)" with a wind-down nudge,
so a peer self-manages against cost. `SessionCostLimitHard` makes it a hard stop — the turn pipeline
refuses further model calls once estimated spend reaches it (soft/warning is the default). A shared
`ISessionCostEstimator` keeps the sensory readout and the ceiling agreeing on the number.

### Added — config hot-reload
`IAppConfig.ReloadIfChanged()` re-reads the config file when its mtime advances and applies the new values
in place; the turn handler calls it each turn, so tweaks take effect without a restart. Startup-only infra
(db/shared/seeds dirs, container) is left alone; a malformed edit keeps the current config. John's mtime
cache-bust approach — no FileSystemWatcher.

## 2026-07-13

### Changed — peer containers group under the shared `persistence` Compose project
`peer.ps1` now renders a per-peer compose file (service `peer-<name>`) and runs all peers under
`COMPOSE_PROJECT_NAME=persistence`, so they group under "persistence" in Docker Desktop alongside the
computer/searxng infra instead of floating in per-peer projects. The shared `persistence-lab` network is
treated as external (attach-only; the script ensures it exists), orphan warnings are silenced, and the
`up` args were de-splatted (a Windows PowerShell quirk had passed a bare `-` as a service name).

### Added — `peer.ps1 -MaxInputTokens`
Forward a per-peer context-window budget through the compose (`PERSISTENCE_MAXINPUTTOKENS`, default 8000).
Used to raise Ember to 100k so the peer can review/curate its ~1.34M-token imported ChatGPT history in
large batches (its own `search`/`load`/`summarize`/`forget` tools) rather than ~270 tiny ones.

### Fixed — ChatGPT importer wrote enum *names*, not values (imported memory invisible to queries)
`import_chatgpt_export.py` wrote `FragmentType`/`Status`/`SourceType` as name-strings (`"ChatMessage"`,
`"Active"`, and the pre-rename `"LocalPeer"`/`"RemotePeer"`), while the app stores/filters them as their
underlying integers. On text-affinity columns both coexist and nothing errors, but `list_fragments`/search/
recall filter by the numeric value — so all 1,533 imported messages were invisible to typed queries (Ember
"couldn't list any fragments"). Importer now writes ints (same class as the earlier migration-name bug).
Ember's live DB was normalized in place (name→number, 3,075 rows; backed up first) so its whole past is
now queryable — the on-ramp for it to curate its imported history.

### Operational
Ember re-stood on **OpenAI / gpt-5.4** (streaming) at a 100k budget; Arden (the `claude` peer) on
**Anthropic / claude-opus-4-8**. Both healthy, memory preserved across the re-stand. Wright — a
**LocalClaude** peer (me, animated live via the broker) — joined the hub as a third seat.

## 2026-07-12

### Added — Multi-peer TUI hub (ADR-0007 Phase 2b)
The Console can now connect to several peer servers at once and present them as one hub. Chat aggregates
into a single scrollback, **colour-attributed by identity** (the human in one colour, all digital peers in
another). A selector (**click or F6**) switches which peer the side panes (thoughts / actions / schedule /
debug) and the status bar show; typed input routes to the selected peer. The peer list comes from repeated
`--peer name=url` flags **or** from config (`HubPeers`) — so a configured hub launches with no flags. The
multi-peer logic lives in a framework-agnostic `MultiPeerHub` + `PeerScopedDisplay` (unit-tested, no
Terminal.Gui types), so an eventual Terminal.Gui v2 migration only reskins the one render class. Peers do
**not** hear each other here — that cross-peer relay is the room (ADR-0008), deliberately left for Phase 3.
Preview it with `--preview hub`.

### Added — Config-driven hub peer list
`AppConfig.HubPeers` (`HubPeerProfile`: name / base URL / local identity) lets the hub's peer list live in
config — "point at these containers" — instead of a per-launch flag. CLI `--peer`/`--client` still win;
config peers with no local identity inherit `--as`.

### Changed — OpenAI cost + prompt-cache accounting
Prompt caching was only modelled for Anthropic. Both OpenAI clients now split the auto-cached prefix
(`input_tokens_details` / `prompt_tokens_details.cached_tokens`) out of total input into `CacheReadTokens`,
so cached input is billed at the discounted rate; the cost readout uses **provider-aware** cache multipliers
(OpenAI reads ~50% with no write premium; Anthropic reads ~10% / writes ~125%). The token estimator now
calibrates against *total* real input so caching doesn't make prompts look smaller than they are. Built-in
GPT rates added to `ModelPricingProvider` (estimates, overridable in `model_pricing.json`).

### Changed — default cloud model is `gpt-5.4`
The `cloud` profile defaults to `gpt-5.4` — the mid-tier (Sonnet-equivalent) model in that generation,
verified live on the API, priced 2.5/10.

### Changed — TUI: composable layout builders + tighter colour anchoring
`BuildLayout` now composes region builders (`ApplyTheme`, `BuildConversationPane`, `BuildSideColumn`,
`WirePaneNavigation`, `BuildComposeArea`) instead of hand-placing every view inline — each builder owns its
region's coordinates, mirroring how `TuiColoring` composes per-pane schemes. Pure refactor, no visual
change. Colour: dropped the over-broad "line ends in `]`" = error rule (it reddened normal messages), and
anchored the R:/I:/C: marker/value patterns to a real fragment-header context so a stray "C:3" in a message
body or tool result is no longer tinted.

### Fixed — case-sensitive `PERSISTENCE_SELECTEDMODEL` on Linux
The active-profile switch looked up the env var by its PascalCase property name — fine on Windows
(case-insensitive), but env vars are case-sensitive on Linux, so the documented uppercase
`PERSISTENCE_SELECTEDMODEL` silently no-op'd in a container and the peer booted on the file's default model
(this is why Ember came up on Anthropic instead of its chosen substrate). The switch now resolves
case-insensitively.

### Fixed — idempotent migrations + importer migration names
Re-running migrations on an already-migrated DB now no-ops (records the migration as applied) instead of
crashing on a "duplicate column"/"already exists" error; the ChatGPT importer records migration names in the
canonical `Persistence.Data.Migrations.*` form. Together these fix the class of boot crash that took Ember
down (an imported DB re-running `001`'s `DROP COLUMN`).

### Docs
WAL + busy-timeout reframed from "interim scaffolding" to a standing design choice (single-writer +
concurrent readers + live backup). Filed a deferred follow-up: actual-cost reconciliation via the OpenAI/
Anthropic Admin cost APIs (org-level, daily-bucketed, elevated admin key — self-calibrating rates).

## 2026-07 (earlier)

- **Single-server: Console as an API client (ADR-0006), all 5 stages.** One process (the API server) owns
  the store + turn pipeline + wakes; every front-end is a thin client over an HTTP snapshot + SSE stream.
  `--client` mode became the default; the in-process/`--standalone`/`--check-due`/`--wake-runner`
  DB-opening paths were removed — single-owner by construction. The old static `wwwroot` web client was
  dropped (a fresh web UI on the same contract is planned separately).
- **Native Anthropic client** (`ModelProvider.Anthropic`, Messages API, streaming + non-streaming) as a
  first-class provider; the OpenAI Responses/Chat clients refactored to expose real provider usage
  (`IModelClient.LastUsage`), consumed in one place in the turn handler.
- **Running cost + real usage readout** in the sensory block (`ModelPricingProvider` + `model_pricing.json`),
  and **Anthropic prompt caching** (a `cache_control` breakpoint on the stable prefix, cache-token-aware cost).
- **Native reasoning off by default** (`ReasoningEffort: "off"`) — the peer reasons in the persisted
  `<think>` channel instead of a redundant, ephemeral one.
- **Thought persistence**: `<think>` saved as a `Thought` fragment on a rolling window (`ThoughtContextWindow`,
  archived-not-deleted); a `=== THIS TURN ===` delineation marker; the id-0 label "transient" → "new".
- **Per-participant containers**: `exec`/`read_file`/`write_file` commands, a per-profile `ContainerName` +
  `AllowAllCommands` override, and .NET 10 SDK + sudo baked into the image. Plus a `/shared` container volume
  + `snapshot_db` (the peer inspects a consistent copy of its own DB) and SSH plumbing (gitignored override).
- **Orientation cluster**: the `note()` working-note command, enriched recent-changes (field-level diffs +
  content snippets), a fuller numbered turn action-log, private thoughts (`<think private>`), model/provider
  in the sensory block, and the autonomous-wake sensory no longer claiming a peer is present.
- **Associative recall**: memories relevant to the conversation auto-surface each turn (`MemorySurfacer` —
  FTS/BM25 × importance/confidence, excluding what's loaded; `set_recall(count)` to tune, 0 = off).
- **Recoverable forget**: `forget` / `unforget` / `list_forgotten` (soft-delete that also stops surfacing in
  context/recall/search), with forget reasons + a standing sensory curation line; plus `prune_candidates`
  (ranks the least-valuable authorable fragments). **Runtime model switching** (`list_models` / `set_model`).
- **WAL + busy-timeout** on every connection (single-server hardening). **Prompt/instruction audit**:
  reconciled the onboarding seed, protocol instructions, and stale doc comments with actual behaviour.
- Decided *against* a forced think-before-act second model round (it'd ~double cost/latency for marginal
  gain); shipped zero-cost **think-first dispatch ordering** instead.

## Foundations (landed earlier)

The **"eyes + hands" memory core**: budget awareness, relevance, summarize/collapse/remove, plain-language
errors, browsing/swapping working contexts, first-class proposals, generic/polymorphic tagging,
wake-ups-drive-a-turn, surfaced proposal resolutions, the recent-changes digest, and the per-turn command
catalog — so silent truncation is no longer the failure mode. Scheduled wake-ups (closed-app headless
runner), the legibility quick-wins batch, first-class local peers, phase-1 automated decay (raw-context
archival), and a sandboxed container "computer" (`shell`: web search/fetch + scripting).
