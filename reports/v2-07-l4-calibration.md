# V2-07 L4 calibration (advisory; no threshold set)

- **Model:** `nli-MiniLM2-L6-H768/onnx/model_quint8_avx2.onnx`; scored 241 sentences in 40.8 s.
- **Source:** the recorded v1 Table 3 answers, `evidence/experiments/exp-20261113T064542Z-c482696` (read only). There are 55 answered rows across 21 questions; q18 was refused before any model call in every setting. No model provider was called.
- **Sentences:** 224 pass L1–L3 on their own; these are the sentences L4 would act on. They are counted per sentence: a few come after a sentence that stops their answer, so a user would not see them. 12 fail L1/L2, and 5 are refusal-shaped (L3). Neither group is counted below, because both are already stopped.
- **Score:** a sentence's L4 score is the best entailment probability over the passages it cites. A threshold blocks a shown sentence whose score is below it.
- **Labels:** `reports/v2-07-l4-labels.json` holds per-sentence labels **proposed by Claude Code and not yet reviewed** (owner decision 2026-11-20). Every rate below is provisional until you review them.
- **Unlabelled shown sentences:** 0.

## If 'should block' means not fully supported (not_supported or partially_supported)

| Threshold | Sentences blocked | True positive rate (blocked / should block) | False positive rate (blocked / should keep) |
| --: | --: | --: | --: |
| 0.3 | 42 of 224 | 61.5 % (16/26) | 13.1 % (26/198) |
| 0.5 | 59 of 224 | 61.5 % (16/26) | 21.7 % (43/198) |
| 0.7 | 74 of 224 | 73.1 % (19/26) | 27.8 % (55/198) |
| 0.9 | 131 of 224 | 84.6 % (22/26) | 55.1 % (109/198) |

## If 'should block' means not_supported only (strict)

| Threshold | Sentences blocked | True positive rate (blocked / should block) | False positive rate (blocked / should keep) |
| --: | --: | --: | --: |
| 0.3 | 42 of 224 | 75.0 % (3/4) | 17.7 % (39/220) |
| 0.5 | 59 of 224 | 75.0 % (3/4) | 25.5 % (56/220) |
| 0.7 | 74 of 224 | 75.0 % (3/4) | 32.3 % (71/220) |
| 0.9 | 131 of 224 | 100.0 % (4/4) | 57.7 % (127/220) |

## L4 scores by proposed label (shown sentences)

| Label | Sentences | L4 median | L4 min | L4 max |
| :-- | --: | --: | --: | --: |
| supported | 198 | 0.887 | 0.001 | 0.991 |
| partially_supported | 22 | 0.176 | 0.002 | 0.979 |
| not_supported | 4 | 0.004 | 0.004 | 0.837 |

## Sentences proposed as not fully supported

| Where | Label | L4 | Sentence |
| :-- | :-- | --: | :-- |
| q14/balanced_rerank#4 | not_supported | 0.0035 | Consequently, the removal of the smoothing module leads to substantial performance degradation  . |
| q14/balanced_rerank#3 | not_supported | 0.0044 | This instability prevents the model from learning consistent chunking boundaries  . |
| q14/balanced_rerank#5 | not_supported | 0.0044 | This outcome confirms that smooth gradient flow through the decompression process is crucial for effective end-to-end learning  . |
| q09/large_wide#6 | not_supported | 0.8367 | The multi-stream design is essential for the model to gain the ability to both listen and speak simultaneously  . |
| q13/small_fast#3 | partially_supported | 0.0024 | This smoothing process transforms discrete boundary indicators into interpolated representations to facilitate gradient propagation during b |
| q12/large_wide#6 | partially_supported | 0.0043 | This process is formalized by the equations where the encoder output is chunked, the main network processes the result, and the decoder appl |
| q11/large_wide#4 | partially_supported | 0.0055 | Additionally, tokenization qualitatively changes what Transformers can learn for certain data distributions, and the primary role of shrinki |
| q13/balanced_rerank#2 | partially_supported | 0.0119 | The smoothing module converts discrete chunks into interpolated representations to improve learnability  . |
| q03/balanced_rerank#1 | partially_supported | 0.0121 | The original Transformer base model employs h = 8 parallel attention layers, or heads  . |
| q10/balanced_rerank#3 | partially_supported | 0.0123 | Therefore, Moshi's reported latencies are comparable to the response times of natural human conversations   . |
| q05/balanced_rerank#4 | partially_supported | 0.0186 | The authors also noted that the sinusoidal version may allow the model to extrapolate to sequence lengths longer than those seen during trai |
| q02/large_wide#4 | partially_supported | 0.05 | The scaling factor is necessary because, without it, additive attention outperforms dot-product attention for larger values of d_k  . |
| q03/large_wide#1 | partially_supported | 0.0756 | The original Transformer base model employs h = 8 parallel attention layers, or heads  . |
| q14/balanced_rerank#2 | partially_supported | 0.0927 | When this module is removed, the compression ratios fluctuate severely throughout training  . |
| q14/small_fast#1 | partially_supported | 0.1268 | The smoothing module is essential for stable training dynamics because it transforms discrete upsampling operations into continuous ones, en |
| q14/large_wide#1 | partially_supported | 0.2253 | The smoothing module is essential for stable training dynamics because it transforms discrete chunking operations into differentiable comput |
| q06/small_fast#5 | partially_supported | 0.2289 | Moshi also aims to display knowledge and reasoning abilities for helpful and enjoyable conversations  . |
| q12/large_wide#2 | partially_supported | 0.5741 | The overall pipeline begins with the encoder processing the input sequence, followed by the main network processing the compressed represent |
| q09/balanced_rerank#5 | partially_supported | 0.6622 | By modeling two streams of semantic and acoustic tokens hierarchically, Moshi can generate an arbitrary range of emotions, voices, and acous |
| q14/small_fast#3 | partially_supported | 0.6777 | When the smoothing module is removed, compression ratios fluctuate severely throughout training, which prevents the model from learning cons |
| q01/balanced_rerank#6 | partially_supported | 0.8316 | It is designed to be superior in quality while being more parallelizable and requiring significantly less time to train  . |
| q06/small_fast#4 | partially_supported | 0.8552 | It is designed to be speech-to-speech to handle paralinguistic communication  . |
| q01/small_fast#4 | partially_supported | 0.9601 | Memory constraints limit batching across examples due to this sequential computation  . |
| q01/balanced_rerank#4 | partially_supported | 0.9601 | Memory constraints limit batching across examples due to this sequential computation  . |
| q13/balanced_rerank#4 | partially_supported | 0.9603 | The routing module guides the model to retain vectors based on semantic importance rather than following predetermined patterns  . |
| q06/small_fast#3 | partially_supported | 0.9793 | The model addresses the need to run in real time and allow for long conversations in full-duplex  . |

## Sentences that fail L1/L2 (already blocked), for reference

| Where | Failures | L4 |
| :-- | :-- | --: |
| q02/small_fast#4 | ungrounded_number | 0.0103 |
| q02/small_fast#5 | ungrounded_number | 0.0051 |
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
