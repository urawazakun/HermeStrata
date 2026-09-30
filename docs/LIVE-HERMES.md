# Live Hermes runs on the cache engine (2026-09-30)

Production engine = cache-fix-1 f621c84, real Hermes (`hermes -p flashnext chat -q ... --oneshot`), 11.8K-token
system prompt + tools. Script: `scripts/hermes_fork_test.ps1`. Same small task delegated once as an isolated child
and once as a forked child (`"fork": true`).

## What worked
- Forked child started from the parent's saved tip: `resume from tip at 19516`, 5.2 s (a full re-read of the
  20K prompt is ~60 s).
- Parent after an isolated child resumed from its tip: `resume from tip at 18786`, ~6 s.
- Inside one session almost every turn resumed from the chain in 1.5-4 s.

## Two holes found (both on the Hermes side, not the engine)
1. **Every new session started cold (35 s).** The root is taken at the end of the system message, and the system
   message ends with a volatile workspace block. Two sessions differed by one line
   (`- Status: 2 modified, 75 untracked` vs `77 untracked`), so every session produced a same-length root with a new
   hash (several such roots on disk). Moving volatile parts "to the tail of the system prompt" is not enough; they must
   not be inside the cached system message at all.
2. **Parent after a forked child started cold (50 s).** Hermes sent a shorter history (17.6K tokens) than the
   parent's previous request (19.5K): earlier messages were rewritten. Leading explanation (Sol's code reading):
   threshold-triggered preflight compaction rewriting history in place; tool-result pruning and micro compaction are
   other candidates (off by default).

## Response: prefix keeper (patch strata/0017, design docs/PREFIX-KEEPER.md; idea by the owner)
`serve/server.py --prefix-keeper` (env `STRATA_PREFIX_KEEPER=1`) keeps every request append-only for the engine:
a system message that differs only in a few volatile lines is replaced by the cached version and the change is
appended as `[context update]` at the end; pruned/shortened earlier messages are restored from the cached request
when the result still fits the context; anything else passes through and is logged
(`prefix-keeper.jsonl`). Opt-in, one flag to remove.

Status: implemented and unit-tested (102 server tests); live acceptance in progress
(`scripts/hermes_keeper_accept.ps1`: new session after a git-status change must resume the root in <= 2 s and still
report the new status; parent after a forked child must never start cold).

## Also: work deadline for Hermes (patch hermes-agent/0003, docs/WORK-DEADLINE.md; experimental)
Told "work until 18:00", Hermes ended its turn after ~1.5 h ("no way to keep going"). `--run-budget` is a ceiling,
not a floor. The patch adds `set_work_deadline(until, goal)` / `finish_work(reason)` tools: before the deadline a
final answer is turned into a short continuation message; stopping early needs an explicit reason; the existing run
budget / max turns still win; three idle continuations end the turn. 125 tests pass; not yet run live.
