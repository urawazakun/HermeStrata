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

## Upstream 0.1.29 port (v100-029), 2026-10-01 03:00 — PASS
c1 root after aux 0.49-0.69 s; c3 tip/chain/recall 3/3; root-disk after restart; needle at 1/3 depth of 39K tokens 3/3;
Japanese answers 25.3 (t=0) / 25.6 (t=0.7) tok/s vs 22.6 / 22.3 on the 0.1.21 build; 39K prompt 95 s vs 108 s.

## Upstream 0.1.30 port (v100-030), 2026-10-01
- first run FAIL: every saved-state restore "recurrent restore failed" (0.1.30 added `dead`/`block_pos` to the
  recurrent checkpoint; ours dropped them) -> fixed, disk schema v3.
- re-run 06:35 PASS: c1 0.53-0.72 s; c3 tip 0.69-0.73 s, fork 1.6-1.7 s, recall 2/2 shown, root-disk after restart
  2.0-2.4 s; needle 3/3; Japanese 24-29 tok/s; 8K-doc decode 25.1 vs 23.2 tok/s (0.1.29, 3 reps each).
  Regression noted: identical repeated request resumes in ~1.3 s (0.1.29: 0.1-0.5 s).

## 2026-10-01 readability refactor (patches/strata 0010-0014)

Same driver and settings as the 0.1.30 acceptance. CPU: 15 C++ tests (cache/layout/restore/codec) and 180 Python
tests pass after every step. GPU: root + aux (3/3 correct answers, root resume 0.5-0.7 s of prefill), parent /
isolated child / fork (fork child recalls the planted fact; one of three ran out of its token budget while thinking,
same pattern as the pre-refactor run), Japanese t=0 / t=0.7, a fact planted at 1/3 of 39K tokens found 3/3, bench x3:
identical repeated 11.7K request 0.48-0.56 s prompt time (pre-refactor 1.31-1.36 s), 8K-doc decode 28.9-29.2 tok/s.
Deployed to local production.
