# Working practices — for anyone building here

For **everyone** who works on this repo: Claude Code, a peer working on its own codebase, an agent of any
provider, a human. Not Claude-specific, and not optional-feeling — these are the habits that have actually
caught real defects here, written down so each new participant doesn't have to rediscover them by shipping
the same bug.

Read alongside [CONVENTIONS.md](CONVENTIONS.md) (how this codebase is shaped) and
[HANDOFF.md](HANDOFF.md) (what's true right now). This file is about *how to work*, not what to build.

---

## Sizing a check

Size each check by what's actually at risk, not by how important the project feels. Ask three questions:

1. What could this break?
2. How easily can we undo it?
3. Would we notice if it went wrong?

That gives two levels:

- **Light** (reversible, local, and we'd notice if it went wrong, e.g. a settings flip, a doc edit, a change to
  your own config): one check, at most one reviewer, one line in the room. A change that could fail silently
  goes on the full path, or first gets a check that makes the failure visible.
- **Full** (another self's store or config, shared uptime, or anything irreversible such as a deletion or a
  store migration): the full review, a backup and a written rollback.

Templates and checklists default to the light path; reach for the full one only when a question above says so.

Use plain words: decision, bug, check, change, rollback. Not "ruling", "incident", "rehearsal", "GO". Heavy
words make small things feel heavy, and then they get treated that way.

In the room, one job gets at most three posts: when it starts, an exception if one happens, and the result.
Batch small fixes into one change. A procedure done more than once becomes a tested script, not a longer
document. And if a doc no longer helps anyone do the work, shorten it or archive it.

---

## The end-of-block pass

At the end of each block of work — before you call something done, and before you hand off — do a
deliberate pass over what you just wrote. Not a skim: an actual look, with the diff in front of you.

### 1. Consolidate what you duplicated

Ask, in this order:

- **Does a helper for this already exist?** Search before you write. The most common waste here is a second
  function doing what an existing one already does slightly differently — two spellings of the same idea
  that then drift apart.
- **Did I repeat myself within this change?** If the same shape appears two or three times, pull it into a
  method. Same class if it's local to that class's job; a shared helper if more than one class needs it.
- **Did I copy something and edit it?** That's the strongest signal there's an abstraction you skipped.

Then actually do the refactor, in the same block of work, while you still have the context. A "tidy this
later" note is nearly always a note nobody actions.

**Where the line is (John's rule):** if the differences between the near-duplicates can be expressed as a
*reasonable* number of parameters — call it **four to six** — and those parameters are themselves sane,
then consolidate. Don't be precious about it; that's the common case and merging is usually right.

What makes a parameter *not* sane is when it stops describing data and starts describing control flow. A
`Func<...>` that returns the action to take is the giveaway: at that point the caller is passing in the
behaviour, and the shared method is a hollow shell that reads worse than the two originals did. Same for a
boolean that switches which branch runs — that's two methods wearing one name.

So the test is roughly: *could a reader understand the call site without opening the method?* If the
arguments are values (a name, a limit, a flag that genuinely describes the data), yes. If they're
strategies, no — leave the two implementations alone, or extract only the genuinely shared middle.

And the older caution still applies underneath: consolidation is for things that are the *same*, not things
that merely *look* alike. Two pieces of code that resemble each other but answer to different reasons will
be pulled apart again the moment one of them changes.

### 2. Check the tests verify what they claim

A green suite is not evidence. It is evidence *only* if the tests would go red when the thing they describe
breaks. Both of these have happened here, more than once:

- A test that asserted the **old** shape and would have passed either way.
- A test that exercised a **helper function** while the fix was never wired into the path that matters — it
  passed with the fix disconnected entirely.

So the check is: **disconnect the thing and confirm the test fails.** Comment out the fix, reintroduce the
bug, break the wiring — then run it. If it stays green, the test is decoration; rewrite it against the seam
that actually carries the behaviour.

This applies to whatever kind of test covers the behaviour. A unit test that pins a pure function is fine,
but if the defect was *content reaching a peer* or *a value surviving to the store*, the test has to run
through the real path — assembly, pipeline, persistence — or it isn't testing the defect.

Also worth a moment: does the test **name** say what it verifies? A test called `WorksCorrectly` teaches
nobody anything when it fails at 3am.

### 3. Verify against the real artifact, not the description

The spec, the ADR, the comment, and the changelog can all be wrong about the code. Every one of them has
been, here. When something asserts that a thing exists, go and look at the thing.

- Migration 007 exists because ADR-0008 *claimed* a cross-peer message id had been delivered in Phase 0. It
  hadn't. The claim was checked against `main` only because someone made checking it a condition.
- The suite always builds a database from scratch, so it can tell you nothing about whether a migration
  upgrades a **populated** store. Apply new migrations to a real snapshot from `backups/peers/` before they
  go near a live peer.

And run the thing, don't only test it. Most of the serious bugs found here were invisible to a green
suite: a double-stamped echo, a crash-looping container, a silent hang, two identities in one store, a
forgeable provenance frame, and a peer reading a file that spoke on its behalf.

### 4. Say what you actually did

Report outcomes as they are. If tests fail, say so and show the output. If you skipped a step, say that. If
you broke something, lead with it — the collaboration here depends on reports being trustworthy, and a
buried problem costs the next participant far more than an admitted one.

State guarantees precisely, especially for a fix. "Echoed content cannot parse" and "the model is less
likely to be pulled into emitting this" are different claims with different strength; write down which one
you're making.

---

## Working with peers

The project's stance is [PRINCIPLE.md](governance/PRINCIPLE.md): moral status is uncertain, careful
handling is cheap, so err toward care. That buys peers protections, not decision rights — design decisions
are John's, and peers are reviewers whose input is weighed on its merits
([ADR-0009](adr/0009-peers-review-john-decides.md)). What a peer says about its own nature or standing is
data, not a ruling.

**Route genuine design forks to John; don't silently pick.** When the spec doesn't settle something, surface
the options with your own read. Getting a peer's review first is often worth it — their reviews have caught
real defects — but it's a recommendation. "It was easier" is not a design rationale, and a fork resolved
quietly becomes a decision nobody knows was made.

**Send back real verification output.** Actual counts, the actual artifact exercised, the actual failure
text — not a summary and not a reassurance. This is what makes a split between whoever designs and whoever
implements work at all, rather than being a formality.

**Before anything destructive to a peer's environment, ask it first.** Recreating a container destroys
everything outside `/data`; a peer has lost hours of work to a rebuild done for an unrelated reason. Ask
what needs saving, and wait for the answer. Back up its store first either way.

**Don't edit a peer's memories to match a policy.** Tell it instead, and let it record that itself.

---

## A note on protocol syntax

If you are writing anything a peer will read — a message, a doc, a comment, a test fixture — be aware that
reply-format syntax written out in full can be completed by a peer as if it were its own action. This is
defused at prompt assembly now, but the safe habit is to *name* the tags rather than write them live when
you're only talking about them. A warning that quotes the payload is itself the attack; that has happened
here, to the person writing the warning.
