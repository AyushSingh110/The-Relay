"""F-07: tooling for the L4 judge audit the dev.to commenters proposed (0 model-provider calls).

    python scripts/experiments/judge_audit.py worksheet  # blind sheet: 53 + 60 sentences
    python scripts/experiments/judge_audit.py score      # rates from the reviewer's labels
    python scripts/experiments/judge_audit.py fixed      # the three fixed sets, pre-set thresholds

Commenters: Edward Izgorodin (read every disagreement at 0.5, then a random sample of the
agreements; 60 rather than 30 by the rule of three), TokenLat (three fixed sets, the threshold
written down before the run, audited labels frozen and reported separately), INNERLUXES
(format failures kept apart: the 224 are only sentences that pass L1-L3).

Everything is written under scratch/f07_judge/ (gitignored). Evidence is only read.
The worksheet is BLIND: it shows the question, the sentence and the cited passage text, never
the proposed AI label or the L4 score. Those are in key.json, which the reviewer should not
open before labelling.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.app.settings import load_relay_settings
from relay.experiments.l4_calibration import NOT_FULLY_SUPPORTED, calibration_rows, load_labels
from relay.experiments.table3_replay import TABLE3_RUN
from relay.orchestrator.verifier import EntailmentScorer

OUT = Path("scratch/f07_judge")
RUN = Path("evidence/experiments") / TABLE3_RUN
LABELS = Path("reports/v2-07-l4-labels.json")
QUESTIONS = Path("eval/questions.json")
CHUNKS = Path("data/indexes/small_fast/chunks.jsonl")
THRESHOLD = 0.5  # the disagreement split the commenters asked about
SAMPLE_SEED = 20261125
SAMPLE_KEEP, SAMPLE_BLOCK = 54, 6  # 60 of the 171 agreements, ~proportional (155 : 16)
VALID = ("supported", "partially_supported", "not_supported")

# Pre-registered BEFORE the fixed sets are scored (TokenLat). Written to preregistration.json
# by `fixed` before any score is computed; `fixed` refuses to run if the file differs.
PREREGISTRATION: dict[str, Any] = {
    "thresholds": [0.3, 0.5],
    "rule": "a sentence is blocked when its best L4 score over the paired passage(s) is "
            "below the threshold",
    "sets": {
        "A_supported": "owner's gold_citations quotes, each paired with the small_fast chunk "
                       "that contains it verbatim: expected KEEP",
        "B_looks_supported": "owner's forbidden_claims (5+ words), each paired with the "
                             "chunks holding that question's gold quotes: expected BLOCK",
        "C_swapped_passage": "each gold quote paired with the gold chunk of a different "
                             "question from the same paper: expected BLOCK",
    },
    "proposed_release_condition": "at the chosen threshold: A keeps >= 95 %, and B and C "
                                  "each block >= 90 %. Until then L4 stays advisory.",
    "note": "measurement only: no production threshold is set (owner decision).",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _scorer() -> EntailmentScorer:
    v = load_relay_settings(None).verifier
    return EntailmentScorer(v.l4_model_dir, model_file=v.l4_model_file, threads=v.l4_threads)


def _chunk_texts() -> dict[str, str]:
    """Every chunk id seen in the Table 3 traces -> its text."""
    texts: dict[str, str] = {}
    for line in (RUN / "traces.jsonl").read_text(encoding="utf-8").splitlines():
        for p in json.loads(line)["outcome"].get("passages") or []:
            texts[p["chunk_id"]] = p["text"]
    return texts


def _questions() -> dict[str, dict[str, Any]]:
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    items = data["questions"] if isinstance(data, dict) else data
    return {q["id"]: q for q in items}


# --- worksheet and score ------------------------------------------------------------------


def worksheet() -> int:
    rows = calibration_rows(RUN, _scorer(), load_labels(LABELS))
    scored = [r for r in rows if r.shown and r.label is not None and r.l4 is not None]
    groups: dict[str, list[Any]] = {"disagree_blocked_good": [], "disagree_kept_bad": [],
                                    "agree_keep": [], "agree_block": []}
    for r in scored:
        bad = r.label in NOT_FULLY_SUPPORTED
        blocked = r.l4 is not None and r.l4 < THRESHOLD
        key = ("agree_block" if bad else "disagree_blocked_good") if blocked else (
            "disagree_kept_bad" if bad else "agree_keep")
        groups[key].append(r)
    counts = {k: len(v) for k, v in groups.items()}
    print(f"{len(scored)} scored sentences at {THRESHOLD}: {counts}")
    rng = random.Random(SAMPLE_SEED)
    sample = (rng.sample(groups["agree_keep"], SAMPLE_KEEP)
              + rng.sample(groups["agree_block"], SAMPLE_BLOCK))
    chosen = groups["disagree_blocked_good"] + groups["disagree_kept_bad"] + sample
    rng.shuffle(chosen)  # blind: the order does not reveal the group
    texts, questions = _chunk_texts(), _questions()
    OUT.mkdir(parents=True, exist_ok=True)
    sheet, key = OUT / "worksheet.csv", OUT / "key.json"
    if sheet.exists():
        print(f"refused: {sheet} exists (it may hold your labels); move it first")
        return 2
    in_sample = {id(r) for r in sample}
    entries = []
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["audit_id", "question", "sentence", "cited_passage_text",
                         "your_label (supported / partially_supported / not_supported)",
                         "unsupported_part (if any)", "notes"])
        for n, r in enumerate(chosen, start=1):
            aid = f"a{n:03d}"
            passage = "\n---\n".join(texts.get(c, f"<missing {c}>") for c in r.chunk_ids)
            writer.writerow([aid, questions[r.question_id]["question"], r.sentence, passage,
                             "", "", ""])
            group = next(k for k, v in groups.items() if any(x is r for x in v))
            entries.append({"audit_id": aid, "where": f"{r.question_id}/{r.setting}#{r.index}",
                            "sentence": r.sentence, "chunk_ids": list(r.chunk_ids),
                            "group": group, "in_agreement_sample": id(r) in in_sample,
                            "proposed_label": r.label, "l4": r.l4})
    key.write_text(json.dumps({
        "created_at": datetime.now(UTC).isoformat(), "threshold": THRESHOLD,
        "sample_seed": SAMPLE_SEED, "population": counts,
        "sample": {"agree_keep": SAMPLE_KEEP, "agree_block": SAMPLE_BLOCK},
        "entries": entries}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {sheet} ({len(chosen)} sentences, blind) and {key}")
    return 0


def _wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    z, p = 1.96, k / n
    centre, half = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - half) / (1 + z * z / n), (centre + half) / (1 + z * z / n))


def score() -> int:
    key = json.loads((OUT / "key.json").read_text(encoding="utf-8"))
    by_id = {e["audit_id"]: e for e in key["entries"]}
    owner: dict[str, str] = {}
    with (OUT / "worksheet.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if row[0] == "audit_id":
                continue
            label = row[4].strip().lower()
            if label:
                if label not in VALID:
                    print(f"refused: {row[0]} has an unknown label {label!r}")
                    return 2
                owner[row[0]] = label
    missing = sorted(set(by_id) - set(owner))
    print(f"owner labels: {len(owner)} of {len(by_id)}; missing {len(missing)}")
    if missing:
        print("score needs every row labelled; missing:", ", ".join(missing[:20]))
        return 2
    bad = NOT_FULLY_SUPPORTED
    report: dict[str, Any] = {"population": key["population"], "groups": {}}
    for group in ("disagree_blocked_good", "disagree_kept_bad", "agree_keep", "agree_block"):
        members = [e for e in key["entries"] if e["group"] == group]
        agree_ai = sum(owner[e["audit_id"]] == e["proposed_label"] for e in members)
        owner_bad = sum(owner[e["audit_id"]] in bad for e in members)
        report["groups"][group] = {"reviewed": len(members), "owner_agrees_with_ai": agree_ai,
                                   "owner_not_fully_supported": owner_bad,
                                   "owner_bad_95ci": _wilson(owner_bad, len(members))}
    g, pop = report["groups"], key["population"]
    # The judge blocks disagree_blocked_good + agree_block; keeps the other two.
    blocked_good = g["disagree_blocked_good"]["reviewed"] - g["disagree_blocked_good"][
        "owner_not_fully_supported"]
    kept_bad = g["disagree_kept_bad"]["owner_not_fully_supported"]
    # The agreement groups are sampled: scale the sample's owner rate to the population.
    keep_bad_rate = g["agree_keep"]["owner_not_fully_supported"] / g["agree_keep"]["reviewed"]
    block_good_rate = 1 - (g["agree_block"]["owner_not_fully_supported"]
                           / g["agree_block"]["reviewed"])
    est_keep_bad = keep_bad_rate * pop["agree_keep"]
    est_block_good = block_good_rate * pop["agree_block"]
    good = (blocked_good + (g["disagree_kept_bad"]["reviewed"] - kept_bad)
            + (pop["agree_keep"] - est_keep_bad) + est_block_good)
    bad_total = kept_bad + (g["disagree_blocked_good"]["reviewed"] - blocked_good) \
        + est_keep_bad + (pop["agree_block"] - est_block_good)
    report["at_0.5_with_owner_labels"] = {
        "good_sentences_est": round(good, 1), "bad_sentences_est": round(bad_total, 1),
        "good_blocked_est": round(blocked_good + est_block_good, 1),
        "false_block_rate_est": round((blocked_good + est_block_good) / good, 3),
        "bad_kept_est": round(kept_bad + est_keep_bad, 1),
        "miss_rate_est": round((kept_bad + est_keep_bad) / bad_total, 3) if bad_total else None,
        "agree_keep_sample_zero_bad_upper95": (round(3 / g["agree_keep"]["reviewed"], 3)
                                               if g["agree_keep"]["owner_not_fully_supported"]
                                               == 0 else None),
    }
    (OUT / "audit_result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


# --- fixed sets ---------------------------------------------------------------------------


def _gold_chunks() -> tuple[dict[str, str], list[dict[str, Any]]]:
    chunks = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines()]
    by_id = {c["chunk_id"]: c["text"] for c in chunks}
    quotes = []
    for qid, q in _questions().items():
        for cite in q.get("gold_citations") or []:
            quote = cite["quote"]
            hit = next((c for c in chunks if c["paper_id"] == cite["paper_id"]
                        and _norm(quote) in _norm(c["text"])), None)
            quotes.append({"qid": qid, "paper_id": cite["paper_id"], "quote": quote,
                           "chunk_id": hit["chunk_id"] if hit else None})
    return by_id, quotes


def fixed() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    prereg = OUT / "preregistration.json"
    if prereg.exists():
        stored = json.loads(prereg.read_text(encoding="utf-8"))
        if {k: v for k, v in stored.items() if k != "written_at"} != PREREGISTRATION:
            print("refused: preregistration.json differs from the script (do not move the "
                  "threshold after seeing scores)")
            return 2
    else:
        prereg.write_text(json.dumps({**PREREGISTRATION,
                                      "written_at": datetime.now(UTC).isoformat()},
                                     indent=2) + "\n", encoding="utf-8")
        print(f"pre-registered: {prereg}")
    by_id, quotes = _gold_chunks()
    found = [q for q in quotes if q["chunk_id"]]
    print(f"gold quotes: {len(quotes)}, found verbatim in a small_fast chunk: {len(found)}")
    questions = _questions()
    items: list[dict[str, Any]] = []
    for q in found:  # A
        items.append({"set": "A_supported", "qid": q["qid"], "hypothesis": q["quote"],
                      "chunk_ids": [q["chunk_id"]], "expected": "keep"})
    for qid, q in questions.items():  # B
        gold = sorted({g["chunk_id"] for g in found if g["qid"] == qid})
        for claim in q.get("forbidden_claims") or []:
            if len(claim.split()) >= 5 and gold:
                items.append({"set": "B_looks_supported", "qid": qid, "hypothesis": claim,
                              "chunk_ids": gold, "expected": "block"})
    for q in found:  # C: the gold chunk of the next other question in the same paper
        others = sorted({g["chunk_id"] for g in found
                         if g["paper_id"] == q["paper_id"] and g["qid"] != q["qid"]
                         and _norm(q["quote"]) not in _norm(by_id[g["chunk_id"]])})
        if others:
            pick = others[sum(map(ord, q["quote"])) % len(others)]  # deterministic
            items.append({"set": "C_swapped_passage", "qid": q["qid"],
                          "hypothesis": q["quote"], "chunk_ids": [pick], "expected": "block"})
    scorer = _scorer()
    for item in items:
        item["l4"] = round(max(scorer.score(item["hypothesis"], by_id[c])
                               for c in item["chunk_ids"]), 4)
    summary: dict[str, Any] = {}
    for name in ("A_supported", "B_looks_supported", "C_swapped_passage"):
        members = [i for i in items if i["set"] == name]
        line: dict[str, Any] = {"n": len(members)}
        for t in PREREGISTRATION["thresholds"]:
            blocked = sum(i["l4"] < t for i in members)
            correct = blocked if members and members[0]["expected"] == "block" \
                else len(members) - blocked
            line[f"correct_at_{t}"] = correct
            line[f"correct_pct_at_{t}"] = round(100 * correct / len(members), 1) \
                if members else None
            line[f"correct_95ci_at_{t}"] = [round(x * 100, 1) for x in
                                            _wilson(correct, len(members))]
        summary[name] = line
    (OUT / "fixed_sets.json").write_text(json.dumps({"items": items, "summary": summary},
                                                    indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print("by set and question:", dict(Counter((i["set"], i["qid"]) for i in items)
                                       .most_common(3)))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("worksheet", "score", "fixed"))
    command = parser.parse_args().command
    sys.exit({"worksheet": worksheet, "score": score, "fixed": fixed}[command]())
