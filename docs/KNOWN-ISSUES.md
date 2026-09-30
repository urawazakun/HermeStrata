# BRIEF: saved-state fixes from GPU acceptance (2026-09-30, found by Claude on build b6f6f0c)

Origin: Claude, GPU acceptance case 3 (scripts/accept_cache.py c3; logs <root>\logs\accept\).
Implement: Muse; review: Sol. Same rules as REPORT-HERMES-CACHE.md (no GPU, no prod, commit --only, no push).
Owner re-runs the GPU acceptance afterwards.

## F1. A failed restore must fall back to the next candidate, never to cold (bug, BLOCKS DEPLOY)

Worst case (step3_bench cached11k, engine-cached-102604.log): the SAME 11707-token request sent twice -> both
`MTP boundary at 11701 missing/unrepaired (no repair source): cold prefill`, 33.4 s each. Upstream reuses this.
So this build is a regression vs production until F1 is fixed. Also ask why the boundary at 11701 has no repair source
at all (a candidate whose MTP rows cannot be restored should not be saved/offered in the first place).

Observed (engine-cached-100449.log): fork-child request, prompt 11927 tokens. Best candidate ended at 11798; then
`MTP boundary at 11798 missing/unrepaired (no repair source): cold prefill` -> `resume from none at 0, suffix 11927`
(36 s). The pinned root at 11687 was a valid prefix of the same prompt and would have cost ~0.6 s.
Fix: collect all valid candidates (chain, tips, roots) sorted by k descending; try each; on any failure (MTP boundary,
byte-size/coverage check, anything the E2 "conservative cold fallback" covers) log it and try the next. Cold only when
no candidate restores. Add a CPU test: best candidate fails validation -> next (root) is used.

## F2. Tips must be usable when the client re-renders the assistant turn differently (design gap)

Observed: parent1 (11714 prompt + 96 generated incl. reasoning) -> isolated child -> parent2 (= parent1 messages +
assistant content + new user) resumed from the ROOT at 11687, not from parent1's tip; same for parent3 after the
fork child (chain at 11687). Cause: the tip's ids are prompt + the generated ids (reasoning/`<think>` included), but
the chat template re-renders the previous assistant turn without the reasoning (and possibly with different
whitespace/tool-call formatting), so the whole tip is never a prefix of the next prompt. The rule "full saved ids
must be a prefix of P" then rejects it and everything after the root is re-read — in a long conversation that is the
whole conversation (30-200 s), which is exactly what tips were meant to prevent.

Fix: a tip saves TWO restorable points of its line:
  (a) the prompt end (state + KV [0, len(prompt))) — always a prefix of the next turn of the same line, and of a
      forked child's prompt (parent prompt + re-rendered assistant + tool result);
  (b) the generation end (current behaviour) — useful only when the client echoes the generated ids verbatim.
Resume picks the longest valid point across both. (a) costs one extra recurrent-state snapshot (~113 MiB) per tip;
KV is shared (same pages, shorter range). Count both against --tip-cache-gib. The upstream chain may already hold a
checkpoint at the prompt end of the live line — reuse it if so, but it must survive an unrelated (child) request,
which the chain does not.
Test (CPU): tip with generated ids that differ from the re-rendered next prompt -> resume at (a), suffix = rendered
assistant + new user only.

## Acceptance targets after the fix (owner, GPU)
- c3 parent2_after_child / parent3_after_fork: resume from tip at ~len(parent prompt), suffix = the new messages only.
- c3 fork_child: resume from parent's tip at ~len(parent prompt), never cold.
- c1 unchanged (root reuse after aux, prefill <= 1 s measured 0.48-0.85 s).

## Acceptance policy change (user, 2026-09-30)
Token/greedy parity cached-vs-cold is NOT a criterion any more: quantized kernels + KV paging make run-to-run
drift normal (observed: 11/20 first-16-token mismatches in step3 even on aux requests with no restore; all 20 sane).
Restore correctness is judged by (1) known-answer questions answered correctly and (2) recall of a fact planted deep
in the restored prefix (a wrong restore fails recall). Do not add parity asserts; CPU unit tests of the store stay.

## Round 2 (Claude, GPU re-run of cache-fix-1 fecd2a9, 2026-09-30 11:40; logs/accept/engine-cached-11*.log, fix-c3.out)
- F1: FIXED on GPU. Fork child 2.35 s from root (was cold 36 s); log shows `chain candidate at 11747 ... trying shorter`.
- F2: NOT fixed. parent2 (prompt 11752) after parent1 (prompt 11731) still resumes from the root at 11687; the
  parent1 prompt-end point is never even offered as a candidate.
  Hypothesis: the prompt-end point includes the chat template's generation prompt (`<|im_start|>assistant\n` +
  `<think>\n` opener) at the end of the prompt; the next request re-renders that assistant turn differently
  (history form, empty/stripped reasoning), so the prompt-end ids are not a prefix of the next prompt.
### F2b
  Save point (a) at the LAST TURN BOUNDARY inside the prompt — right after the last message's end-of-turn
  (`<|im_end|>` + newline; the engine already has `--turn-token` / upstream turn boundaries for its chain) — not at
  the raw prompt end. That point is a prefix of: the same line's next turn, and a forked child's prompt.
  First verify on CPU with the tokenizer + chat template: render parent1 prompt and parent2 prompt (bodies in
  scripts/accept_cache.py case3) and print the longest common prefix vs. the two candidate positions.
  The saved point needs an MTP boundary repair source (the F1 log shows candidates rejected for lacking one) —
  make sure (a) stores whatever the restore needs at that exact position.
  Test (CPU): next-turn prompt that re-renders the previous assistant turn differently resumes at (a).
