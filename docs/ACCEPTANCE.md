# Acceptance ledger

One table for every GPU acceptance item: what is checked, how it is judged, last result, who acts next.
Judging rule (owner): answer quality and restore correctness, **not** token parity (quantized kernels + KV paging
drift run to run). Restore correctness = known-answer questions + recall of a fact planted deep in the prefix.

Build under test: Strata b6f6f0c (E1/E2/E4/E5), 2026-09-30. Driver: `scripts/run_accept.ps1` (`accept_cache.py`).
Config: V100 16 GB, ctx 262144, `--kv int8 --kv-resident 20480`, expert cache 3127, IQ3_S.

| # | Item | Pass rule | 2026-09-30 result | Next |
|---|---|---|---|---|
| 1 | New session after title (aux) request | 2nd session prefill <= 2 s | **PASS** 0.48 / 0.59 / 0.65 s (cold 34.0 s); aux 3-4 s, no eviction | - |
| 2 | Root from disk after restart (E3) | `root-disk`, <= 5 s | not run (E3 in progress) | Sol/Muse E3, then owner |
| 3a | Isolated child from its own root | suffix only | **PASS** 0.77-0.88 s after first run | - |
| 3b | Parent after child resumes from its tip | suffix = new messages | **FAIL** falls back to root (F2) | Sol/Muse fix F2 |
| 3c | Forked child resumes from parent | never cold | **FAIL** cold 36 s (F1) | Sol/Muse fix F1 |
| 3d | Identical request repeated | reuse | **FAIL** cold 33 s (F1) — regression vs production | Sol/Muse fix F1 |
| 4 | 20 mixed Hermes-shaped requests, cached vs cold | all sane | **PASS** 20/20 sane | add recall probe |
| 5a | Decode speed vs production binary | within noise | **PASS** 8K-doc 27.1 vs 26.5 tok/s, small 22.0 vs 21.2 (3 reps) | - |
| 5b | `--spec-adapt` on vs off | within noise or better | not measured (script bug, fixed) | owner re-run |
| 6 | Tip budget / eviction / `/v1/cache` (S2) | budget held, roots pinned | not run | after S2 |

Deploy gate: 1, 3b, 3c, 3d, 4, 5a pass (and 2 once E3 lands). Currently blocked by F1/F2.

## Re-run on cache-fix-1 f621c84 (E3+S1+S2 + F1/F2/F2b), 2026-09-30 12:30
| # | result |
|---|---|
| 1 | PASS 0.50-0.66 s from root after aux |
| 2 | PASS (incidental): after dev-engine restart, parent1 resumed `root-disk` at 11687, 2.46 s |
| 3a | PASS isolated child from its own tip 0.17 s |
| 3b | PASS parent after child: `tip` at 11724-11726 (turn boundary), read 26 tokens, 0.72-0.77 s |
| 3c | PASS forked child: `chain` at 11745-11747, 1.7 s; recall of planted fact 3/3 (青いペンギンN号) |
| 3d | PASS identical repeat: 0.45-0.53 s (run on fecd2a9) |
| 5a | decode 8K-doc 26.7 tok/s (prod 26.5) — unchanged |
| 5b | --spec-adapt: no measurable gain (24.2-25.9 vs 25.5-25.8) — keep off |
Deploy gate met (2 disk-root: basic pass; 6 capacity not run).
