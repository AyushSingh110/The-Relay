# Superseded: do not use these numbers

This re-run was started on 2026-11-20 with the first version of V2-14 fix A. A code review then
found that it also gave back the provider's own (header-based) headroom for streams cut off
mid-answer, which the stand-in does not refund. That would make tpm_halved and overload look
better than they are. The run was stopped before it finished, and the fixed code was re-run in
a new `runs/v2-load-rerun-*` directory. See reports/phase-v2-14-load-revalidation.md.
