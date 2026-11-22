# Evaluation questions: how to write and grade them

The owner writes the questions (at least 15, target 18) in `eval/questions.json`.
Claude provides the schema, validator and grading tooling only. It does not write,
rewrite or fill in questions; P04B checks the questions against the PDFs and *reports*
any discrepancies.

Start from `eval/questions.template.json`. Every `<...>` placeholder must be replaced:
the validator rejects any text that still contains one.

## Fields (ARCHITECTURE.md section 11)

| Field | Rule |
| :--- | :--- |
| `id` | `q01` … `q999`; unique |
| `author` | your name |
| `category` | `direct_factual`, `multi_hop`, `numeric_technical`, `cross_paper`, `unanswerable` |
| `question` | as a user would ask it |
| `answerable` | `false` exactly when `category` is `unanswerable` |
| `expected_answer` | required if answerable; `null` otherwise |
| `required_facts` | ≥ 1 if answerable, empty if not. Alternatives are separated by `\|`, e.g. `"512\|d_model = 512"` |
| `gold_citations` | ≥ 1 if answerable: `paper_id` (`1706.03762v7`, `2410.00037v1`, `2507.07955v2`), `page` (1-based PDF page), `section` (optional), `quote` (short and verbatim) |
| `forbidden_claims` | plausible wrong claims; any match makes the grade `wrong` |
| `trap_type` | `null` or one of `bibliography_match`, `shared_vocabulary`, `garbled_extraction`, `version_leak` |
| `notes` | optional |

Coverage required by `scripts/validate_questions.py` (strict mode):
- at least 15 questions;
- at least 3 unanswerable;
- at least 3 retriever traps covering at least 3 distinct `trap_type` values.

Missing categories and fewer than 18 questions produce warnings.

## Retriever-trap ideas grounded in P03 extraction findings (for your choice only)

The PDF-ingestion phase (P03) deliberately left these source-text
artefacts unchanged so they remain measurable failure cases:
- **`garbled_extraction`**:
  - flattened equations (Attention p.4: `softmax(QK^T / √d_k / )V (1)`; the `1/√d_k` numerator drifts);
  - table headers merged into the first rows (Attention Table 2, p.8);
  - questionable hyphen joins (`crossmatch`, `melspectrogram`, `bitsper`, `predecay`).
- **`bibliography_match`**: reference-list chunks share keywords with the body. They are down-weighted unless the question is about references.
- **`shared_vocabulary`**: "Transformer", "attention" and "chunk" occur in all three papers. The scope analyzer treats them as weak cues and never filters on them.
- **`version_leak`**: questions about content from versions other than v7 / v1 / v2.

## Commands

```powershell
python scripts/validate_questions.py                     # strict
python scripts/validate_questions.py --allow-incomplete  # while drafting

# After an evaluation run (P07) has produced answers.jsonl:
python scripts/grade_answers.py propose --run-id <run_id> --answers <answers.jsonl> --out <grades.json>
python scripts/grade_answers.py show --grades <grades.json> --pending
python scripts/grade_answers.py review --grades <grades.json> --request-id <id>                  # confirm
python scripts/grade_answers.py review --grades <grades.json> --request-id <id> --final wrong --note "<why>"
python scripts/grade_answers.py summary --grades <grades.json>
```

## Grading rules (deterministic proposal; the owner reviews every row)

| Question | Answer | Proposed grade |
| :--- | :--- | :--- |
| answerable | refusal (including a verifier safe refusal) | `refused_wrongly` |
| unanswerable | refusal | `refused_correctly` |
| unanswerable | an answer | `wrong` |
| answerable | all `required_facts` present **and** ≥ 1 citation on a gold page **and** no `forbidden_claims` | `correct` |
| answerable | anything else (a correct fact with a wrong or missing citation is `wrong`) | `wrong` |

Fact matching is a normalised substring test (case-, width- and whitespace-insensitive).
Proposals start as `pending`. Only owner-confirmed or owner-overridden grades are final,
and an override must carry a note.
