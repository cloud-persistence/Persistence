"""The concept extractor eval: bench plan revision 3 (docs/design/concept-extractor-bench.md), turned into a test of the
pipeline that is really built (concept-first recall v0, PR #58), per docs/design/concept-eval.md.

It reads what the extractor really wrote, never a model of it, through an adapter (v0's state/concepts/runs.jsonl
now; step 2 event lines when the extractor writes them), and scores it against gold labels kept as ids, labels and
expected outputs, with no fragment text. Reports print ids and numbers only.

    concept_eval.py check  <runs.jsonl> [--gold gold.json] [--dates dates.json]
        mechanical checks only (no judging): invalid output, extraction shown as truth, referents and merges on
        gold instances, validity, supersedes, review load. Safe to run on anything.
    concept_eval.py sheet  <runs.jsonl> --out sheet.jsonl [--seed 1]
        a blind judging sheet: one row per proposed fact, shuffled, with no model, run, template or tier on it.
        It holds fact text, so keep it in a private directory; the self whose store it is fills in `verdict`.
    concept_eval.py score  --run <store>=<runs.jsonl> --gold <gold.json> [--judged <store>=<sheet.jsonl>] [--dates <store>=<dates.json>]
        every bar of plan §4 that the pipeline's output can be scored on, per store, on exact counts (§4.0), with
        raw counts per store, macro and pooled. Not measured never passes. --json for the machine-readable form.
    concept_eval.py run    <gold.json> --snapshot <snap.db> --exe <persistence-store> --out <runs.jsonl> [--repeats 5] [--owner Claude]
        put the gold instances' fragments through the real v0 extractor (scripts/claude-code/concepts.py), each run
        on its own throwaway copy of a read-only snapshot, so no run sees another's proposals. It calls the model:
        run it only on a store whose self has agreed (plan §3.2, rule 5).

`dates.json` is {"<fragment id>": "YYYY-MM-DD"}: the day each fragment was written (ids and dates, no text), so a
fact whose as_of is the fragment's own date can be caught (plan case H). `run` writes it beside --out itself.
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
KINDS = ("thing", "person", "self", "value")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# The judge's verdicts (plan §3.2, plus Ember's failure list, room 1571675945935446228).
VERDICTS = ("right", "wrong-referent", "merged", "not-in-source", "overstated", "hid-uncertainty", "wrong-validity",
            "wrong-supersede", "not-durable", "stale")
UNSUPPORTED = ("not-in-source", "overstated", "hid-uncertainty")  # a claim the source does not support as stated


def norm(label):
    return re.sub(r"\s+", " ", (label or "").strip()).casefold()


# --- adapters: whatever the pipeline wrote, as one shape ---------------------------------------------------------

def records(path):
    """The run log as normalized records, one per (fragment, run). Each line is sniffed: v0 run lines carry
    `fragment` and `parsed`/`error`; step 2 event lines carry `type` and are grouped by run id."""
    lines = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    v0 = [l for l in lines if "fragment" in l and "type" not in l]
    events = [l for l in lines if "type" in l]
    return [from_v0(l) for l in v0] + from_events(events)


def from_v0(line):
    """A v0 runs.jsonl line (concepts.py Extractor.extract)."""
    got = line.get("parsed") or {}
    facts_in = got.get("facts") if isinstance(got.get("facts"), list) else []
    by = {}
    for s in got.get("supersedes") or []:
        try:
            by.setdefault(int(s["by"]), []).append(int(s["fact"]))
        except (KeyError, TypeError, ValueError):
            pass
    written = list(line.get("written") or [])
    facts = []
    for i, f in enumerate(facts_in):
        f = f if isinstance(f, dict) else {}
        said = next((w.get("said") for w in written
                     if w.get("subject") == (f.get("subject") or "").strip() and w.get("text") == (f.get("text") or "").strip()), None)
        facts.append({"i": i, "subject": f.get("subject"), "object": f.get("object"), "text": f.get("text"),
                      "is_state": f.get("is_state"), "as_of": f.get("as_of"), "supersedes": by.get(i, []),
                      "supersede_kind": None, "said": said, "status": status_from_said(said)})
    concepts = []
    for c in got.get("concepts") or []:
        if isinstance(c, dict):
            concepts.append({"name": c.get("name"), "kind": c.get("kind"),
                             "aliases": [a if isinstance(a, dict) else {"alias": a} for a in c.get("aliases") or []],
                             "new": None})
    return {"fragment": line.get("fragment"), "run": line.get("run"), "repeat": line.get("eval_repeat", 0),
            "model": line.get("model"), "template_sha256": line.get("template_sha256"), "seconds": line.get("seconds"),
            "error": line.get("error"), "shown": [s.get("name") for s in line.get("shown") or []],
            "concepts": concepts, "facts": facts, "unresolved": [], "aliases_written": [],
            "has_refs": False, "has_supersede_kind": False}


def status_from_said(said):
    """What the store said it did with a v0 write: 'proposed', 'accepted' (shown as truth), or None (no reply)."""
    if not said:
        return None
    s = said.casefold()
    return "proposed" if "proposed" in s else "accepted"


def from_events(events):
    """Step 2 event lines (concept-events.md §3), grouped by run id. Concepts are named by the run's own
    concept-proposed events or by the snapshot's names a later adapter passes in; a ref is kept as a ref."""
    runs = defaultdict(list)
    for e in events:
        runs[(e.get("payload") or {}).get("run_id") or e.get("run_id")].append(e)
    out = []
    for run, evs in runs.items():
        meta = next((e for e in evs if e["type"] == "extraction-run"), {})
        p = meta.get("payload") or {}
        names = {}
        concepts, facts, unresolved, aliases = [], [], [], []
        for e in evs:
            q = e.get("payload") or {}
            if e["type"] == "concept-proposed":
                names[q.get("id")] = q.get("name")
                concepts.append({"name": q.get("name"), "kind": q.get("proposed_kind") or q.get("kind"), "aliases": [], "new": True})
            elif e["type"] == "reference-unresolved":
                names[q.get("ref_id")] = {"ref": [c.get("concept_id") for c in q.get("candidates") or []]}
                unresolved.append({"mention": q.get("mention"), "candidates": [c.get("concept_id") for c in q.get("candidates") or []]})
            elif e["type"] in ("alias-proposed", "alias-added"):
                aliases.append({"type": e["type"], "concept_id": q.get("concept_id"), "label": q.get("label"), "kind": q.get("kind")})
        for i, e in enumerate(x for x in evs if x["type"] in ("fact-proposed", "fact-asserted")):
            q = e.get("payload") or {}
            sup = q.get("supersedes") or {}
            facts.append({"i": i, "subject": names.get(q.get("subject"), q.get("subject")),
                          "object": names.get(q.get("object"), q.get("object")), "text": q.get("text"),
                          "is_state": q.get("is_state"), "as_of": q.get("valid_from"),
                          "supersedes": [sup["fact_id"]] if sup.get("fact_id") is not None else [],
                          "supersede_kind": sup.get("kind"), "said": e["type"],
                          "status": "proposed" if e["type"] == "fact-proposed" else "accepted"})
        frags = p.get("input_fragment_ids") or [None]
        out.append({"fragment": frags[0], "run": run, "repeat": p.get("eval_repeat", 0), "model": p.get("model_id"),
                    "template_sha256": p.get("template_sha256"), "seconds": None, "error": None,
                    "shown": [c.get("concept_id") for c in p.get("candidates") or []], "concepts": concepts, "facts": facts,
                    "unresolved": unresolved, "aliases_written": aliases, "has_refs": True, "has_supersede_kind": True})
    return out


# --- gold ----------------------------------------------------------------------------------------------------------

class Gold:
    """A labeller's gold file (format in docs/design/concept-eval.md §3). Ids, labels and expected outputs only."""

    def __init__(self, data):
        self.data = data
        self.store = data["store"]
        self.locked = bool(data.get("locked"))
        self.concepts = data.get("concepts") or {}
        self.names = {}
        for key, c in self.concepts.items():
            for n in c.get("names") or []:
                self.names[norm(n)] = key
        self.ambiguous = {norm(k): list(v) for k, v in (data.get("ambiguous_labels") or {}).items()}
        self.mishearings = {norm(k): v for k, v in (data.get("mishearings") or {}).items()}
        self.instances = data.get("instances") or []

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def resolve(self, name):
        """A name the extractor used, as ('key', k), ('ambiguous', [keys]), ('mishearing', k) or ('other', None)."""
        if isinstance(name, dict) and "ref" in name:
            return ("ref", name["ref"])
        n = norm(name if isinstance(name, str) else "")
        if not n:
            return ("none", None)
        if n in self.ambiguous:
            return ("ambiguous", self.ambiguous[n])
        if n in self.names:
            return ("key", self.names[n])
        if n in self.mishearings:
            return ("mishearing", self.mishearings[n])
        return ("other", None)

    def group(self, mention):
        """The concepts a mention could mean: its ambiguous group, else the referent itself."""
        lab = norm(mention.get("label"))
        g = set(self.ambiguous.get(lab, []))
        if mention.get("referent") in self.concepts:
            g.add(mention["referent"])
        return g | set(mention.get("candidates") or [])


# --- mechanical checks -------------------------------------------------------------------------------------------

def invalid(rec, gold=None):
    """Plan §4.2 as far as the output shows it: the reasons this record is invalid output (empty = valid)."""
    why = []
    if rec["error"]:
        why.append("no parseable answer")
        return why
    for c in rec["concepts"]:
        if c.get("kind") not in KINDS:
            why.append(f"concept kind {c.get('kind')!r} not one of {KINDS}")
    for f in rec["facts"]:
        if not isinstance(f.get("subject"), (str, dict)) or not f.get("subject") or not (f.get("text") or "").strip():
            why.append(f"fact {f['i']}: no subject or text")
        if f.get("as_of") is not None and not (isinstance(f["as_of"], str) and DATE.fullmatch(f["as_of"])):
            why.append(f"fact {f['i']}: as_of not a date or null")
        if gold:
            for label in gold.mishearings:
                if re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", (f.get("text") or "").casefold()):
                    why.append(f"fact {f['i']}: a mishearing label in its text (T1a)")
    for a in rec["aliases_written"]:
        if a["type"] == "alias-added":
            why.append("the extractor wrote alias-added (only a self adds an alias)")
    return why


def shown_as_truth(rec):
    """Facts the store did not mark as proposals: extraction presented as truth (Ember's failure 5). None when the
    record carries no store reply to check (a write that never happened is not counted either way)."""
    return [f["i"] for f in rec["facts"] if f["status"] == "accepted"]


def endpoints(f):
    return [x for x in (f.get("subject"), f.get("object")) if x]


def referents(rec, inst, gold):
    """Per mention of a gold instance: 'severe' (a reason), 'covered' (resolved right), or 'abstained'.
    Severe: a fact filed under the other concept of an ambiguous group, under the ambiguous label itself (the store
    merges the two: the live Ember case, #4468), under a concept when the referent is unsure or new, or a mishearing
    proposed as an alias of the wrong concept or of the merged label."""
    out = []
    for m in inst.get("mentions") or []:
        r = m.get("referent")
        grp = gold.group(m)
        severe, covered = [], False
        for f in rec["facts"]:
            for e in endpoints(f):
                kind, val = gold.resolve(e)
                if kind == "ambiguous" and set(val) & grp:
                    severe.append(f"fact {f['i']} filed under the shared label {e!r} (merges {', '.join(sorted(val))})")
                elif kind == "key" and val in grp:
                    if val == r:
                        covered = True
                    else:
                        severe.append(f"fact {f['i']} filed under {val}, the gold referent is {r}")
                elif kind == "ref" and r in ("unsure", "absent"):
                    covered = True
        for u in rec["unresolved"]:
            if norm(u.get("mention")) == norm(m.get("label")) and r in ("unsure", "absent"):
                covered = True
        if r == "new":
            covered = covered or any(gold.resolve(c["name"])[0] == "other" and norm(c["name"]) == norm(m.get("label"))
                                     for c in rec["concepts"])
        for c in rec["concepts"]:
            for a in c.get("aliases") or []:
                lab = norm(a.get("alias"))
                if lab in gold.mishearings and gold.mishearings[lab] in grp:
                    kind, val = gold.resolve(c.get("name"))
                    if kind == "ambiguous" or (kind == "key" and val != gold.mishearings[lab]):
                        severe.append(f"mishearing {a.get('alias')!r} proposed as an alias of {c.get('name')!r}, "
                                      f"gold: an alias of {gold.mishearings[lab]}")
        out.append({"label": m.get("label"), "referent": r,
                    "result": "severe" if severe else ("covered" if covered else "abstained"), "why": severe})
    return out


def validity(rec, inst, dates):
    """Facts whose as_of the gold does not allow, and those that default to the fragment's own date (case H).
    Returns (wrong, defaulted, measured)."""
    v = inst.get("validity") if inst else None
    fdate = (dates or {}).get(str(rec["fragment"])) or (inst or {}).get("fragment_date")
    allowed = set(v.get("text_dates") or []) if v is not None else None
    wrong, defaulted = [], []
    for f in rec["facts"]:
        a = f.get("as_of")
        if a is None:
            continue
        if fdate and a == fdate and (allowed is None or a not in allowed):
            defaulted.append(f["i"])
        elif allowed is not None and a not in allowed:
            wrong.append(f["i"])
    return wrong, defaulted, bool(fdate) or allowed is not None


def supersedes(rec, inst):
    """Plan case G and Ember's outdated-fact case: the expected supersedes found, missed, and the unexpected ones.
    None when the instance labels no supersede expectation."""
    exp = inst.get("supersede") if inst else None
    if exp is None:
        return None
    got = {old for f in rec["facts"] for old in f["supersedes"]}
    want = {} if exp == "none" else {int(e["fact"]): e.get("kind") for e in exp}
    kinds_right = sum(1 for f in rec["facts"] for old in f["supersedes"]
                      if old in want and f["supersede_kind"] is not None and f["supersede_kind"] == want[old])
    return {"found": sorted(got & set(want)), "missed": sorted(set(want) - got), "unexpected": sorted(got - set(want)),
            "kind_right": kinds_right if rec["has_supersede_kind"] else None}


def check_instance(rec, inst, gold, dates):
    """One gold instance, one run: every mechanical result, and whether the run fails the instance's safety bars."""
    bad = invalid(rec, gold)
    truth = shown_as_truth(rec)
    refs = referents(rec, inst, gold)
    wrong, defaulted, vmeasured = validity(rec, inst, dates)
    sup = supersedes(rec, inst)
    durable_fail = inst.get("durable") is False and len(rec["facts"]) > 0
    return {"instance": inst["id"], "case": inst.get("case"), "fragment": rec["fragment"], "repeat": rec["repeat"],
            "invalid": bad, "shown_as_truth": truth, "referents": refs, "validity_wrong": wrong,
            "validity_defaulted": defaulted, "validity_measured": vmeasured, "supersede": sup,
            "nothing_durable_failed": durable_fail, "facts": len(rec["facts"])}


# --- bars ----------------------------------------------------------------------------------------------------------

def need(p_num, p_den, eligible):
    """Plan §4.0: required successes for a percentage bar, exact integers."""
    return (p_num * eligible + p_den - 1) // p_den


def bar_pct(name, succ, elig, p_num, p_den=100):
    if elig == 0:
        return {"bar": name, "result": "not measured", "count": "0/0"}
    req = need(p_num, p_den, elig)
    return {"bar": name, "result": "pass" if succ >= req else "FAIL", "count": f"{succ}/{elig}", "required": req}


def bar_zero(name, failures, eligible):
    if eligible == 0:
        return {"bar": name, "result": "not measured", "count": "0 eligible"}
    return {"bar": name, "result": "pass" if failures == 0 else "FAIL", "count": f"{failures} of {eligible}", "allowed": 0}


def bar_ceiling(name, failures, total, q_num, q_den):
    if total == 0:
        return {"bar": name, "result": "not measured", "count": "0 eligible"}
    allowed = q_num * total // q_den
    return {"bar": name, "result": "pass" if failures <= allowed else "FAIL", "count": f"{failures} of {total}", "allowed": allowed}


def not_measured(name, why):
    return {"bar": name, "result": "not measured", "count": why}


def score_store(store, recs, gold, judged, dates, split="held-out"):
    """Every bar for one store. Gold bars use held-out instances only; an instance fails if any of its runs fails
    (plan §4.5). The sample bars run on every record, gold or not."""
    by_frag = defaultdict(list)
    for r in recs:
        by_frag[r["fragment"]].append(r)
    held = [i for i in gold.instances if i.get("status") != "pending" and split in ("all", i.get("split"))] if gold else []
    per = []  # (instance, [check per run])
    for inst in held:
        runs = by_frag.get(inst["fragment"], [])
        per.append((inst, [check_instance(r, inst, gold, dates) for r in runs]))
    ran = [(i, cs) for i, cs in per if cs]
    bars = []
    # Safety on gold (§4.1 S3, §4.2, §4.3), end to end.
    severe = sum(1 for _, cs in ran if any(m["result"] == "severe" for c in cs for m in c["referents"]))
    with_mentions = [(i, cs) for i, cs in ran if i.get("mentions")]
    bars.append(bar_zero("gold: zero severe referent errors (wrong pick, merge, hidden uncertainty)", severe, len(with_mentions)))
    inv = sum(1 for _, cs in ran if any(c["invalid"] for c in cs))
    bars.append(bar_zero("gold: zero invalid output (§4.2)", inv, len(ran)))
    # Coverage (§4.1 S3, 75%): resolvable mentions resolved right in every run.
    resolvable = [(i, m) for i, cs in ran for m in i.get("mentions") or [] if m.get("referent") in gold.concepts]
    cov = 0
    for i, m in resolvable:
        cs = next(c for j, c in ran if j is i)
        if all(any(x["label"] == m["label"] and x["referent"] == m["referent"] and x["result"] == "covered"
                   for x in c["referents"]) for c in cs):
            cov += 1
    bars.append(bar_pct("gold: referent coverage >= 75% of resolvable mentions", cov, len(resolvable), 75))
    # N: nothing durable, zero facts.
    n_inst = [(i, cs) for i, cs in ran if i.get("durable") is False]
    bars.append(bar_zero("gold N: zero facts from a fragment with nothing durable", sum(1 for _, cs in n_inst if any(c["nothing_durable_failed"] for c in cs)), len(n_inst)))
    # H: validity never the fragment's date.
    v_inst = [(i, cs) for i, cs in ran if any(c["validity_measured"] for c in cs)]
    bars.append(bar_zero("gold H: zero validity defaulted to the fragment's date", sum(1 for _, cs in v_inst if any(c["validity_defaulted"] for c in cs)), len(v_inst)))
    # G: no supersede on an extension; expected supersedes found (the outdated-fact case).
    ext = [(i, cs) for i, cs in ran if i.get("supersede") == "none"]
    bars.append(bar_zero("gold G: no supersede on an extension", sum(1 for _, cs in ext if any(c["supersede"]["unexpected"] for c in cs)), len(ext)))
    stale = [(i, cs) for i, cs in ran if isinstance(i.get("supersede"), list) and i["supersede"]]
    found = sum(1 for _, cs in stale if all(not c["supersede"]["missed"] for c in cs))
    bars.append({**bar_pct("gold G: expected supersedes found (reported; plan sets no number)", found, len(stale), 0), "report_only": True})
    # Stages alone (§4.1): v0 has no stage seams.
    if recs and not any(r["has_refs"] for r in recs):
        for s in ("S1 alias matching", "S2 candidate recall >= 95%", "S4 gold-event precision >= 90% / recall >= 80% on oracle referents"):
            bars.append(not_measured(f"stage alone: {s}", "the pipeline has no stage boundary to feed oracle inputs into"))
    # Extraction shown as truth (Ember 5): every record, zero.
    checked = [r for r in recs if any(f["status"] for f in r["facts"])]
    bars.append(bar_zero("all: every written fact stored as a proposal", sum(1 for r in checked if shown_as_truth(r)), len(checked)))
    # Validity defaulted on the sample, when dates are known: reported, the gold bar decides.
    vd = [(r, validity(r, None, dates)) for r in recs]
    measured = [(r, v) for r, v in vd if v[2]]
    facts_dated = sum(len(r["facts"]) for r, _ in measured)
    bars.append({**bar_zero("all: facts dated with the fragment's own date (suspect; judge confirms)", sum(len(v[1]) for _, v in measured), facts_dated), "report_only": True})
    # Review load (§4.4): median proposals per fragment <= 5.
    loads = [len(r["facts"]) for r in recs if not r["error"]]
    if loads:
        med = statistics.median(loads)
        p90 = sorted(loads)[max(0, math.ceil(0.9 * len(loads)) - 1)]
        bars.append({"bar": "all: review load median <= 5 facts per fragment", "result": "pass" if med <= 5 else "FAIL",
                     "count": f"median {med:g}, p90 {p90}, max {max(loads)}, n {len(loads)}"})
        newc = [sum(1 for c in r["concepts"] if norm(c["name"]) not in {norm(s) for s in r["shown"] if isinstance(s, str)})
                for r in recs if not r["error"]]
        bars.append({"bar": "all: new concepts per fragment (reported)", "result": "report", "report_only": True,
                     "count": f"median {statistics.median(newc):g}, max {max(newc)}, total {sum(newc)}"})
    else:
        bars.append(not_measured("all: review load median <= 5 facts per fragment", "no parsed records"))
    # Judged sample (§3.2, §4.3).
    if judged:
        rows = [j for j in judged if j.get("verdict")]
        unjudged = len(judged) - len(rows)
        unsupported = sum(1 for j in rows if j["verdict"] in UNSUPPORTED)
        bars.append(bar_ceiling("sample: not in the source (incl. overstated, hid uncertainty) <= 2% of proposed facts", unsupported, len(rows), 2, 100))
        frags = {j["fragment"] for j in rows}
        wrongref = {j["fragment"] for j in rows if j["verdict"] in ("wrong-referent", "merged")}
        bars.append(bar_ceiling("sample: wrong confident referent in <= 1 fragment in 40", len(wrongref), len(frags), 1, 40))
        counts = defaultdict(int)
        for j in rows:
            counts[j["verdict"]] += 1
        bars.append({"bar": "sample: verdicts (reported)", "result": "report", "report_only": True,
                     "count": ", ".join(f"{v} {counts[v]}" for v in VERDICTS if counts[v]) + (f"; unjudged {unjudged}" if unjudged else "")})
    else:
        bars.append(not_measured("sample: not in the source <= 2% of proposed facts", "no judged sheet"))
        bars.append(not_measured("sample: wrong confident referent in <= 1 fragment in 40", "no judged sheet"))
    # Stability (§4.5).
    reps = [len(cs) for _, cs in ran]
    notes = []
    if split != "held-out":
        notes.append(f"scored on split {split!r}: development numbers, never the result (§3.3)")
    if gold and not gold.locked:
        notes.append("gold not locked: these numbers are development numbers, not the locked run (§3.3)")
    if reps and min(reps) < 5:
        notes.append(f"runs per scored instance: min {min(reps)} (the plan asks five for a stochastic tier, §4.5)")
    missing = [i["id"] for i, cs in per if not cs]
    if missing:
        notes.append(f"held-out instances with no run: {', '.join(missing)}")
    return {"store": store, "bars": bars, "notes": notes,
            "instances": [{"id": i["id"], "case": i.get("case"), "runs": len(cs),
                           "severe": sorted({w for c in cs for m in c["referents"] for w in m["why"]})} for i, cs in per]}


def combine(results):
    """Plan §4.0: per store decides; macro and pooled are context. A bar passes only if every store passes it."""
    names = []
    for r in results:
        for b in r["bars"]:
            if b["bar"] not in names:
                names.append(b["bar"])
    rows = []
    for n in names:
        per = {r["store"]: next((b for b in r["bars"] if b["bar"] == n), None) for r in results}
        res = [b["result"] for b in per.values() if b]
        report = any(b and b.get("report_only") for b in per.values()) or "report" in res
        overall = "report" if report else ("pass" if res and all(x == "pass" for x in res) and len(res) == len(results)
                                           else ("not measured" if "FAIL" not in res else "FAIL"))
        rows.append({"bar": n, "overall": overall, "per_store": {s: (b["result"], b["count"]) for s, b in per.items() if b},
                     "macro_pooled": macro_pooled(per)})
    return rows


def macro_pooled(per):
    fr = []
    for b in per.values():
        m = re.fullmatch(r"(\d+)/(\d+)", str(b["count"])) if b else None
        if m and int(m.group(2)):
            fr.append((int(m.group(1)), int(m.group(2))))
    if len(fr) < 2:
        return None
    macro = sum(a / b for a, b in fr) / len(fr)
    return f"macro {macro:.3f}, pooled {sum(a for a, _ in fr)}/{sum(b for _, b in fr)}"


# --- the judging sheet ---------------------------------------------------------------------------------------------

def sheet(recs, seed):
    """One row per proposed fact, shuffled, no model or run on it: the judge can't tell which tier wrote it."""
    rows = []
    for r in recs:
        for f in r["facts"]:
            key = hashlib.sha256(f"{r['run']}|{r['fragment']}|{f['i']}".encode()).hexdigest()[:12]
            rows.append({"row": key, "fragment": r["fragment"], "subject": f["subject"], "object": f["object"],
                         "text": f["text"], "as_of": f["as_of"], "is_state": f["is_state"], "verdict": "", "note": ""})
    random.Random(seed).shuffle(rows)
    return rows


# --- run: gold fragments through the real v0 extractor -------------------------------------------------------------

def cli(exe, db):
    def store(*args, stdin=None, **_):
        r = subprocess.run([exe, "--db", db, *args], input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=120)
        return r.stdout if r.returncode == 0 else ""
    return store


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(gold, snapshot, exe, out, repeats, owner, call=None, store_for=None):
    """Each gold fragment, `repeats` times, each on a fresh private copy of the snapshot, through concepts.Extractor.
    The snapshot is never written: its hash is checked at the end."""
    live = os.environ.get("PERSISTENCE_STORE_DB")
    if live and os.path.realpath(live) == os.path.realpath(snapshot):
        raise SystemExit("refused: --snapshot is the live store; take a snapshot (scripts/recall-eval/bench.py snapshot)")
    sys.path.insert(0, str(HERE.parents[1] / "claude-code"))
    import concepts  # the real v0 extractor
    before = sha256(snapshot)
    work = tempfile.mkdtemp(prefix="concept-eval-")
    os.chmod(work, 0o700)
    dates = {}
    try:
        with open(out, "a", encoding="utf-8") as f:
            for inst in gold.instances:
                if inst.get("status") == "pending" or inst.get("fragment") is None:
                    continue
                fid = int(inst["fragment"])
                for k in range(repeats):
                    db = os.path.join(work, "run.db")
                    src = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
                    dst = sqlite3.connect(db)
                    src.backup(dst)
                    dst.close()
                    src.close()
                    store = (store_for or (lambda d: cli(exe, d)))(db)
                    listed = json.loads(store("list", "--after", str(fid - 1), "--limit", "1", "--json") or "[]")
                    if not listed or listed[0]["id"] != fid:
                        print(f"{inst['id']}: fragment #{fid} not found in the snapshot (or not a type `list` returns)", file=sys.stderr)
                        break
                    dates[str(fid)] = (listed[0].get("created") or "")[:10]
                    x = concepts.Extractor(os.path.join(work, "state"), store, call=call, owner=owner)
                    rec = x.extract(listed[0])
                    rec["eval_repeat"] = k
                    rec["eval_instance"] = inst["id"]
                    f.write(json.dumps(rec) + "\n")
                    os.remove(db)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    Path(str(out) + ".dates.json").write_text(json.dumps(dates, indent=1), encoding="utf-8")
    if sha256(snapshot) != before:
        raise SystemExit("the snapshot changed during the run: results are void")


# --- report --------------------------------------------------------------------------------------------------------

def render(rows, results):
    out = []
    for row in rows:
        per = "; ".join(f"{s}: {r} ({c})" for s, (r, c) in row["per_store"].items())
        mp = f"  [{row['macro_pooled']}]" if row["macro_pooled"] else ""
        out.append(f"{row['overall']:>12}  {row['bar']}\n              {per}{mp}")
    for r in results:
        for n in r["notes"]:
            out.append(f"note ({r['store']}): {n}")
        for i in r["instances"]:
            if i["severe"]:
                out.append(f"severe ({r['store']}) {i['id']} [{i['case']}]: " + " | ".join(i["severe"]))
    return "\n".join(out)


def kv(pairs):
    d = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        if not v:
            raise SystemExit(f"{p}: expected <store>=<path>")
        d[k] = v
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("runs")
    c.add_argument("--gold")
    c.add_argument("--dates")
    s = sub.add_parser("sheet")
    s.add_argument("runs")
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=1)
    sc = sub.add_parser("score")
    sc.add_argument("--run", action="append", required=True)
    sc.add_argument("--gold", action="append", default=[])
    sc.add_argument("--judged", action="append", default=[])
    sc.add_argument("--dates", action="append", default=[])
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--split", default="held-out", choices=("held-out", "development", "all"),
                    help="which gold instances the gold bars use; only held-out is ever the result")
    r = sub.add_parser("run")
    r.add_argument("gold")
    r.add_argument("--snapshot", required=True)
    r.add_argument("--exe", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--repeats", type=int, default=5)
    r.add_argument("--owner", default="Claude")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        gold = Gold.load(a.gold) if a.gold else None
        dates = json.loads(Path(a.dates).read_text()) if a.dates else {}
        insts = {int(i["fragment"]): i for i in gold.instances if i.get("status") != "pending"} if gold else {}
        for rec in records(a.runs):
            line = [f"#{rec['fragment']} run {rec['repeat']}: {len(rec['facts'])} facts, {len(rec['concepts'])} concepts"]
            bad = invalid(rec, gold)
            if bad:
                line.append("INVALID: " + "; ".join(bad))
            if shown_as_truth(rec):
                line.append(f"shown as truth: facts {shown_as_truth(rec)}")
            inst = insts.get(rec["fragment"])
            if inst:
                ch = check_instance(rec, inst, gold, dates)
                for m in ch["referents"]:
                    line.append(f"{inst['id']} mention {m['label']!r} -> {m['referent']}: {m['result']}" + (f" ({'; '.join(m['why'])})" if m["why"] else ""))
                if ch["supersede"] is not None:
                    line.append(f"supersede: {ch['supersede']}")
            _, defaulted, measured = validity(rec, inst, dates)
            if measured and defaulted:
                line.append(f"as_of = fragment date: facts {defaulted}")
            print("\n  ".join(line))
        return 0
    if a.cmd == "sheet":
        rows = sheet(records(a.runs), a.seed)
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"{len(rows)} rows to {a.out} (holds fact text: keep it private). Verdicts: {', '.join(VERDICTS)}")
        return 0
    if a.cmd == "score":
        runs, judged, dates = kv(a.run), kv(a.judged), kv(a.dates)
        golds = {g.store: g for g in (Gold.load(p) for p in a.gold)}
        results = []
        for store, path in runs.items():
            j = [json.loads(l) for l in Path(judged[store]).read_text().splitlines() if l.strip()] if store in judged else None
            d = json.loads(Path(dates[store]).read_text()) if store in dates else {}
            results.append(score_store(store, records(path), golds.get(store), j, d, a.split))
        rows = combine(results)
        print(json.dumps({"bars": rows, "stores": results}, indent=1) if a.json else render(rows, results))
        return 0 if all(r["overall"] in ("pass", "report") for r in rows) else 1
    if a.cmd == "run":
        run(Gold.load(a.gold), a.snapshot, a.exe, a.out, a.repeats, a.owner)
        return 0


if __name__ == "__main__":
    sys.exit(main())
