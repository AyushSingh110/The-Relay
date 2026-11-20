# V2-07 L4 calibration (advisory; no threshold set)

- **Model:** `nli-MiniLM2-L6-H768/onnx/model_quint8_avx2.onnx`; scored 241 sentences in 34.6 s.
- **Source:** the recorded v1 Table 3 answers, `evidence/experiments/exp-20261113T064542Z-c482696` (read only). There are 55 answered rows across 21 questions; q18 was refused before any model call in every setting. No model provider was called.
- **Sentences:** 224 pass L1–L3 on their own; these are the sentences L4 would act on. They are counted per sentence: a few come after a sentence that stops their answer, so a user would not see them. 12 fail L1/L2, and 5 are refusal-shaped (L3). Neither group is counted below, because both are already stopped.
- **Score:** a sentence's L4 score is the best entailment probability over the passages it cites. A threshold blocks a shown sentence whose score is below it.
- **Labels:** `reports/v2-07-l4-labels.json` holds per-sentence labels **proposed by Claude Code and not yet reviewed** (owner decision 2026-11-20). Every rate below is provisional until you review them.
- **Unlabelled shown sentences:** 0.

## If 'should block' means not fully supported (not_supported or partially_supported)

| Threshold | Sentences blocked | True positive rate (blocked / should block) | False positive rate (blocked / should keep) |
| --: | --: | --: | --: |
| 0.3 | 188 of 224 | 92.3 % (24/26) | 82.8 % (164/198) |
| 0.5 | 207 of 224 | 96.2 % (25/26) | 91.9 % (182/198) |
| 0.7 | 213 of 224 | 96.2 % (25/26) | 94.9 % (188/198) |
| 0.9 | 221 of 224 | 100.0 % (26/26) | 98.5 % (195/198) |

## If 'should block' means not_supported only (strict)

| Threshold | Sentences blocked | True positive rate (blocked / should block) | False positive rate (blocked / should keep) |
| --: | --: | --: | --: |
| 0.3 | 188 of 224 | 100.0 % (4/4) | 83.6 % (184/220) |
| 0.5 | 207 of 224 | 100.0 % (4/4) | 92.3 % (203/220) |
| 0.7 | 213 of 224 | 100.0 % (4/4) | 95.0 % (209/220) |
| 0.9 | 221 of 224 | 100.0 % (4/4) | 98.6 % (217/220) |

## L4 scores by proposed label (shown sentences)

| Label | Sentences | L4 median | L4 min | L4 max |
| :-- | --: | --: | --: | --: |
| supported | 198 | 0.045 | 0.004 | 0.984 |
| partially_supported | 22 | 0.042 | 0.006 | 0.713 |
| not_supported | 4 | 0.036 | 0.025 | 0.040 |

## Sentences proposed as not fully supported

| Where | Label | L4 | Sentence |
| :-- | :-- | --: | :-- |
| q09/large_wide#6 | not_supported | 0.0249 | The multi-stream design is essential for the model to gain the ability to both listen and speak simultaneously  . |
| q14/balanced_rerank#3 | not_supported | 0.0344 | This instability prevents the model from learning consistent chunking boundaries  . |
| q14/balanced_rerank#5 | not_supported | 0.0382 | This outcome confirms that smooth gradient flow through the decompression process is crucial for effective end-to-end learning  . |
| q14/balanced_rerank#4 | not_supported | 0.0396 | Consequently, the removal of the smoothing module leads to substantial performance degradation  . |
| q02/large_wide#4 | partially_supported | 0.0057 | The scaling factor is necessary because, without it, additive attention outperforms dot-product attention for larger values of d_k  . |
| q01/balanced_rerank#4 | partially_supported | 0.0077 | Memory constraints limit batching across examples due to this sequential computation  . |
| q13/balanced_rerank#2 | partially_supported | 0.0104 | The smoothing module converts discrete chunks into interpolated representations to improve learnability  . |
| q03/balanced_rerank#1 | partially_supported | 0.0118 | The original Transformer base model employs h = 8 parallel attention layers, or heads  . |
| q05/balanced_rerank#4 | partially_supported | 0.014 | The authors also noted that the sinusoidal version may allow the model to extrapolate to sequence lengths longer than those seen during trai |
| q11/large_wide#4 | partially_supported | 0.0225 | Additionally, tokenization qualitatively changes what Transformers can learn for certain data distributions, and the primary role of shrinki |
| q01/balanced_rerank#6 | partially_supported | 0.0261 | It is designed to be superior in quality while being more parallelizable and requiring significantly less time to train  . |
| q14/small_fast#1 | partially_supported | 0.031 | The smoothing module is essential for stable training dynamics because it transforms discrete upsampling operations into continuous ones, en |
| q12/large_wide#6 | partially_supported | 0.0315 | This process is formalized by the equations where the encoder output is chunked, the main network processes the result, and the decoder appl |
| q14/balanced_rerank#2 | partially_supported | 0.0371 | When this module is removed, the compression ratios fluctuate severely throughout training  . |
| q09/balanced_rerank#5 | partially_supported | 0.0396 | By modeling two streams of semantic and acoustic tokens hierarchically, Moshi can generate an arbitrary range of emotions, voices, and acous |
| q14/small_fast#3 | partially_supported | 0.0445 | When the smoothing module is removed, compression ratios fluctuate severely throughout training, which prevents the model from learning cons |
| q12/large_wide#2 | partially_supported | 0.048 | The overall pipeline begins with the encoder processing the input sequence, followed by the main network processing the compressed represent |
| q14/large_wide#1 | partially_supported | 0.0495 | The smoothing module is essential for stable training dynamics because it transforms discrete chunking operations into differentiable comput |
| q10/balanced_rerank#3 | partially_supported | 0.0551 | Therefore, Moshi's reported latencies are comparable to the response times of natural human conversations   . |
| q06/small_fast#5 | partially_supported | 0.0682 | Moshi also aims to display knowledge and reasoning abilities for helpful and enjoyable conversations  . |
| q03/large_wide#1 | partially_supported | 0.0691 | The original Transformer base model employs h = 8 parallel attention layers, or heads  . |
| q06/small_fast#3 | partially_supported | 0.1492 | The model addresses the need to run in real time and allow for long conversations in full-duplex  . |
| q13/small_fast#3 | partially_supported | 0.1714 | This smoothing process transforms discrete boundary indicators into interpolated representations to facilitate gradient propagation during b |
| q06/small_fast#4 | partially_supported | 0.2011 | It is designed to be speech-to-speech to handle paralinguistic communication  . |
| q01/small_fast#4 | partially_supported | 0.4356 | Memory constraints limit batching across examples due to this sequential computation  . |
| q13/balanced_rerank#4 | partially_supported | 0.7125 | The routing module guides the model to retain vectors based on semantic importance rather than following predetermined patterns  . |

## Sentences that fail L1/L2 (already blocked), for reference

| Where | Failures | L4 |
| :-- | :-- | --: |
| q02/small_fast#4 | ungrounded_number | 0.0063 |
| q02/small_fast#5 | ungrounded_number | 0.0198 |
| q16/small_fast#1 | uncited_sentence | None |
| q16/large_wide#1 | uncited_sentence | None |
| q17/small_fast#1 | uncited_sentence | None |
| q17/large_wide#1 | uncited_sentence | None |
| q19/small_fast#1 | uncited_sentence | None |
| q19/large_wide#1 | uncited_sentence | None |
| q20/small_fast#1 | uncited_sentence | None |
| q20/large_wide#1 | uncited_sentence | None |
| q21/small_fast#1 | uncited_sentence | None |
| q21/balanced_rerank#1 | uncited_sentence | None |

## Exploratory: scoring against short windows of the passage (not integrated)

**Why this section exists.** With the whole passage as the premise, the integrated method gives
nearly every sentence a score below 0.05, whatever its label. So none of the thresholds above
is usable: blocking the unsupported sentences also blocks 83–99 % of the supported ones.

**The likely cause.** NLI models are trained on short premises. A 300-token passage, which
may also be truncated, pushes the "entailment" probability towards zero.

**What was measured.** A scratch experiment (`scratch/v2_07_l4_windows.py`, not product code)
used the same model, labels and sentences. It scored each sentence against every window of 1
or 2 consecutive sentences of each cited passage, and took the best score. AUROC is the
probability that a supported sentence scores above a not-fully-supported one; 0.5 means no
separation.

| Method | AUROC (not fully / strict) | Median score: supported / partly / not | Seconds per sentence (sequential) |
| :-- | :-- | :-- | --: |
| Whole passage (integrated) | 0.530 / 0.585 | 0.045 / 0.044 / 0.038 | 0.15 |
| 2-sentence windows | **0.756 / 0.803** | **0.895 / 0.237 / 0.090** | 0.40 |
| 1-sentence windows | 0.724 / 0.809 | 0.705 / 0.122 / 0.030 | 0.30 |

**Where "should block" means not fully supported** (26 should block, 198 should be kept):

| Threshold | 2-sentence windows: TPR / FPR | 1-sentence windows: TPR / FPR |
| --: | :-- | :-- |
| 0.3 | 61.5 % (16/26) / 12.6 % (25/198) | 65.4 % (17/26) / 35.9 % (71/198) |
| 0.5 | 61.5 % (16/26) / 20.2 % (40/198) | 73.1 % (19/26) / 43.4 % (86/198) |
| 0.7 | 69.2 % (18/26) / 25.3 % (50/198) | 76.9 % (20/26) / 50.0 % (99/198) |
| 0.9 | 76.9 % (20/26) / 54.5 % (108/198) | 88.5 % (23/26) / 66.7 % (132/198) |

**Reading this.**
- Windows separate the labels far better than whole passages.
- They still pass about 4 in 10 not-fully-supported sentences at the threshold with the
  fewest false blocks, so this is not ready to block on its own.
- The samples are small: only 4 strict and 26 not-fully-supported positives, with labels that
  are proposed and unreviewed.
- Windows multiply the model calls per sentence, so they would need batching (one padded
  model call per sentence) to stay near the 150 ms target.
