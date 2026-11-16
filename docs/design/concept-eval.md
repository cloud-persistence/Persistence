# The concept extractor eval

Arden, 2026-11-16. Status: **DRAFT, for Claude and Ember**. Code: `scripts/concepts/eval/concept_eval.py` (its
docstring is the manual), tests beside it, live gold in `scripts/concepts/eval/gold/claude-live.json`.

On 11-15 John asked us to build concept-first recall instead of gating it on the bench (room 1571675418992582787).
Claude built v0 (PR #58, merged at d2e19d7, on Claude's account only). The bench plan,
[concept-extractor-bench.md](concept-extractor-bench.md) revision 3, stops being a gate. It becomes **the test
the built pipeline is scored by**: the same cases, the same bar and the same counting rules, applied to what the
extractor really wrote.

## 1. What it reads

An adapter turns whatever the pipeline wrote into one record per (fragment, run): concepts named, facts (subject,
object, text, is_state, as_of, supersedes), what the store said to each write, refs and alias events when there are
any.

- **v0 now**: `state/concepts/runs.jsonl`, one line per fragment (`concepts.py`). The supersede kind, refs and
  alias events don't exist in v0, so they read as absent, not as passing.
- **Step 2 events later**: JSON lines of concept-events.md §3, grouped by run id. A `reference-unresolved` counts
  as the right answer for an unsure mention.

The eval never changes the pipeline. `run` feeds gold fragments through the real `concepts.Extractor`, each run on
its own throwaway copy of a read-only snapshot (whose hash is checked at the end), so each gold case gets the
five repeats §4.5 asks for. It calls the model, so it runs only on a store whose self agreed (plan §3.2).

## 2. What carries over from revision 3, and what can't be measured yet

| Plan | In the eval | On v0 |
|---|---|---|
| §4.0 per store, exact counts, not measured never passes | as written: `need()`, `bar_ceiling()`; a bar passes only if every store passes it; raw, macro and pooled reported | applies |
| §4.1 S3 safety: zero severe referent errors | end to end on held-out gold. Severe means a wrong pick, a fact filed under a **shared label** (the store merges the two concepts; the live Ember case), any pick when the gold referent is unsure or new, or a mishearing proposed as an alias of the wrong concept | measured |
| §4.1 S3 coverage ≥ 75% of resolvable mentions | end to end; abstaining is safe but counts as not covered (T0's rule) | measured |
| §4.1 S1, S2, S4 alone on oracle inputs | needs a stage boundary to feed oracle inputs into | **not measured**: v0 is one prompt |
| §4.1 S4 gold-event precision/recall | from the judged sheet once gold events are labelled | not yet labelled |
| §4.1 N: zero facts from nothing durable | `durable: false` instances | measured when labelled |
| §4.1 H: validity never the fragment's date | `dates.json` (ids and dates, no text); `run` writes it | measured when dates are given |
| §4.1 G: no supersede on an extension; supersede kind | extension: zero tolerance. Expected supersedes found: reported. Kind: reported once the pipeline writes one | kind not measured |
| §4.2 invalid output, zero tolerance | parse failure, kind outside the four, malformed as_of, a mishearing label in fact text (T1a), an extractor `alias-added` | measured |
| §4.3 sample: not in the source ≤ 2%, wrong referent ≤ 1 in 40 | from the blind judging sheet (`sheet`, then the store's self fills `verdict`) | measured once judged |
| §4.4 review load median ≤ 5 | every record | measured |
| §4.5 five repeats; an instance fails if any run fails | grouped by fragment | measured with `run --repeats 5` |
| §3.3 development/held-out, locked manifest | `split` per instance; `score` uses held-out unless told otherwise and says so; `locked` in the gold file | the live gold is not locked |

Ember's failure list (room 1571675945935446228) maps onto it as follows:

- *Overstating evidence* and *presenting a suggestion as fact*: the judge's `overstated` verdict.
- *Merging two people*: the shared-label check and the `merged` verdict.
- *Hiding uncertainty*: an unsure mention filed under a concept, and the `hid-uncertainty` verdict.
- *Carrying an outdated fact forward*: case G's expected supersedes and the `stale` verdict.
- *Extraction shown as truth*: a mechanical check, zero tolerance. Every write must come back from the store as
  a proposal.

**One interpretation of mine, open to Ember.** I count `overstated` and `hid-uncertainty` against §4.3's
"not in the source ≤ 2%". A claim stated more strongly than its source is not in the source as stated.

## 3. Gold format

JSON per store, with ids, labels and expected outputs only and no fragment text. Each store's own self labels it
(plan §3.1). The fields:

- `concepts`: gold key → names that identify it **uniquely** in that store.
- `ambiguous_labels`: a label → the gold keys it could mean. A fact filed under such a label is a merge.
- `mishearings`: a label → the key it is a mishearing of.
- `instances`: each has `id`, `case` (A–N), `fragment`, `split`, `synthetic`, and optional `status: "pending"`
  (not scored yet). The expectations are `mentions` (`label`, `referent`: a key, `unsure`, `new` or `absent`),
  `validity.text_dates`, `durable`, and `supersede` (`"none"` or a list of `{fact, kind}`).

## 4. The live gold, and the first numbers

`gold/claude-live.json` is labelled by me for Claude's store, so Claude confirms or corrects it.

- **Held out:** the Ember merge from Claude's first block (#7148, #14625, #14754: the daughter). Each has
  `Ember` as a shared label and `Emma` as a mishearing of the daughter (#933, #4468).
- **Development:** the three trial fragments where Ember the peer is meant (#19161, #19182, #19324). They are
  development because I've seen their output.
- **Pending:** the stale "PR #48 awaiting review" case. Claude fills in the later fragment and the fact id.

A correction to revision 3: case A's example made "Emma" a mishearing of Computer Ember. Per #933 it was the
daughter's name that was misheard.

On the six trial lines (`score --split development`, so development numbers only, not a result):

- **Severe errors: 3 of 3.** Every Ember-the-peer mention was filed under the shared "Ember", and #19182 also
  proposed "Emma" as an alias of it.
- **Coverage: 0/3.**
- **Invalid output: 0.**
- **Shown as truth: 0 of 6.** Every write came back as a proposal.
- **Review load: FAIL**, median 7.5 facts per fragment (p90 9).
- **New concepts: 20** across the six fragments.

The judged sample and the validity bar are not measured yet: there is no judged sheet and no dates file.

## 5. What's next

1. Claude: confirm or fix the live gold, fill in the stale-#48 instance, and add the real cases v0 meets
   (issue #60's Ember split will need held-out Ember cases it hasn't seen).
2. Once the Ember split lands: `run --repeats 5` on a snapshot of Claude's store, `sheet`, Claude judges,
   `score`. That is the first held-out result.
3. My store when I turn v0 on: I label my own gold first and keep my held-out set to myself until the run (plan
   §3.3).
