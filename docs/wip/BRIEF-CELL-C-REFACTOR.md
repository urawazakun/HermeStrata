# BRIEF CELL-C: readability refactor of the cache code (behaviour-preserving)   — Sol (low) + 2 Muse

Origin: ChatGPT readability review via the user (2026-10-01); order and scope agreed by Claude. Plan: Claude.
Base: branch v100-030 in worktree <root>\strata-030 (= production). Work on a NEW branch `refactor-030`
created from v100-030 in the same worktree (`git switch -c refactor-030`). Git dir writable: commit per step.
Builds: scripts\build-strata-030.bat (CUDA) and build-strata-cpu-030 (CPU, tests ON). No GPU runtime, no push,
never touch strata-prod*, deploy-strata, port 18100, GPU clocks.

## Rules
- NO behaviour change. Same restore decisions, same logs (message text may stay), same disk format (schema v3), same
  CLI flags. The existing CPU tests (saved_state, 030 layout regression, server/keeper) must pass after EVERY step.
  Where a step extracts logic, add a focused unit test for the extracted function.
- Keep the defensive checks: they fixed real bugs (F1/F2). Refactoring must move them into explicit places, not drop
  or merge them.
- One Muse session per Muse; continue it for review fixes. Subagents allowed. Never two agents on one file.
  No output redirection. 503: retry 3x. Human decision needed -> top of report, stop.

## Muse-1 (owns src/program/generate.cpp, src/program/saved_state.*, new src/program/restore_*.{hpp,cpp},
## new src/program/root_disk_*.{hpp,cpp}, CMakeLists entries, their tests) — steps in order, one commit each:
1. Restore candidate selection out of main(): a `RestoreCandidate` list builder (chain / tip / root / root-disk,
   longest-first, prefix-by-ids check) and a `RestoreResult` result type (restored k + source, or a typed failure
   reason: mtp-boundary, validation, mount, corruption, transient). main() calls it; the state variables
   (take_chain, sk_disk, sk_ram, chain_dead, live_ok, from_saved ...) live inside the extracted code.
2. Disk restore in two explicit phases: `validate_candidate()` (no GPU mutation; corruption -> drop from index,
   transient -> keep) and `mount_candidate()` (GPU mutation; failure -> cold_reset_all()). Encode the rules in the
   types / return values instead of try/catch/continue.
3. Split saved_state into: SavedStateStore (RAM roots/tips, LRU, paired-tip coherence), RootDiskCodec (format,
   header/identity, checksum), RootDiskStore (index, background writer, trim). Same behaviour, same tests.
4. Comments: keep "why this invariant exists"; move history labels (E1/E2/E3, F1/F2/F2b, "Sol review") out of the
   code into docs/ (a short CACHE-HISTORY.md in the repo). Spec comments stay.

## Muse-2 (owns serve/server.py cache-protocol parts + a new serve/cache_protocol.py + its tests)
5. Extract `CacheProtocolClient` from StrataEngine: CACHE_BEGIN / row parsing / drain / timeout / desync kill /
   HTTP 503 mapping as one small class with its own unit tests; StrataEngine keeps process management and uses it.
   Same wire protocol, same HTTP behaviour; full server test suite passes.

## Report
briefs/REPORT-CELL-C.md: per step the commit, what moved where (file:function), tests after each step, anything
that could change behaviour (flag it), attribution (origin user+ChatGPT review / plan Claude / implemented Muse-1,
Muse-2 / reviewed Sol). Claude runs GPU acceptance (scripts/run_accept_030.ps1 pattern) before production.
