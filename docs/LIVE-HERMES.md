# Live Hermes runs on the cache engine (2026-09-30)

Production engine = cache-fix-1 f621c84, real Hermes (`hermes -p flashnext chat -q ... --oneshot`), 11.8K-token
system prompt + tools. Script: `scripts/hermes_fork_test.ps1`. Same small task delegated once as an isolated child
and once as a forked child (`"fork": true`).

## Correction (same day)
The first version of this page said a forked child started from the parent's tip in 5.2 s. That was wrong: the
`flashnext` profile had the `delegation` toolset disabled, `delegate_task` was not available, and both runs had
**zero child sessions** — the model did the work itself with the terminal. The resume lines were the parent's own
turns. Fork mode has **not** been exercised with live Hermes yet (it is covered by unit tests and by the engine-level
acceptance in docs/ACCEPTANCE.md, case 3).

## What the runs did show
- Inside one session almost every turn resumed from the chain in 1.5-4 s.
## Two holes found (both on the Hermes side, not the engine)
1. **Every new session started cold (35 s).** The root is taken at the end of the system message, and the system
   message ends with a volatile workspace block. Two sessions differed by one line
   (`- Status: 2 modified, 75 untracked` vs `77 untracked`), so every session produced a same-length root with a new
   hash (several such roots on disk). Moving volatile parts "to the tail of the system prompt" is not enough; they must
   not be inside the cached system message at all.
2. **A later turn in the same session started cold (50 s).** Hermes sent a shorter history (17.6K tokens) than
   its previous request (19.5K): earlier messages were rewritten (not related to delegation). Leading explanation (Sol's code reading):
   threshold-triggered preflight compaction rewriting history in place; tool-result pruning and micro compaction are
   other candidates (off by default).

## Response: prefix keeper (patch strata/0017, design docs/PREFIX-KEEPER.md; idea by the owner)
`serve/server.py --prefix-keeper` (env `STRATA_PREFIX_KEEPER=1`) keeps every request append-only for the engine:
a system message that differs only in a few volatile lines is replaced by the cached version and the change is
appended as `[context update]` at the end; pruned/shortened earlier messages are restored from the cached request
when the result still fits the context; anything else passes through and is logged
(`prefix-keeper.jsonl`). Opt-in, one flag to remove.

Live result (2026-09-30 20:40, `scripts/hermes_keeper_accept.ps1`): after the git status changed between sessions,
the new session resumed from the ROOT (11743 tokens) instead of re-reading 11.8K tokens (35 s); the keeper logged a
`system-delta` with the one changed line and the model answered from the updated status. The history-rewrite case
has not recurred yet in a live run.

## Also: work deadline for Hermes (patch hermes-agent/0003, docs/WORK-DEADLINE.md; experimental)
Told "work until 18:00", Hermes ended its turn after ~1.5 h ("no way to keep going"). `--run-budget` is a ceiling,
not a floor. The patch adds `set_work_deadline(until, goal)` / `finish_work(reason)` tools: before the deadline a
final answer is turned into a short continuation message; stopping early needs an explicit reason; the existing run
budget / max turns still win; three idle continuations end the turn. 125 tests pass; not yet run live.

## 2026-10-01: delegation works live; ask_document
- With the `delegation` toolset enabled, a child ran and its result reached the parent (169 s) after the deadlock fix
  (patch 0004). Before it, the parent waited forever: the child's worker thread blocked in a process-wide
  `nemo_relay` subscriber flush that needed the parent's (blocked) event loop.
- `ask_document` on a 26K-token directory: 1st question read the document (root captured, persisted), 2nd question
  `resume from root at 26411, suffix 53` -> 165 s vs 43 s for the whole Hermes turn; both answers correct.

## 2026-10-01: a whole game from a spec (orchestration test)

Claude and Muse discussed what makes a roguelike fun (3 rounds; the owner asked for a Shiren-the-Wanderer feel), Muse
wrote a 325-line spec with 7 milestones, and Hermes + Qwen3.8-Flash-Next implemented it alone into one HTML file.

- Run 1 (10:13-12:37, deadline 13:00): all 7 milestones claimed done (1281 lines), with its own node checks that all
  pass. A real playtest found that walls and doors were never drawn: the spec said "visible = room tiles plus
  neighbours that are not wall/closed door", meaning "you cannot see through walls", and the model implemented it
  literally. Node checks could not see the screen.
- Run 2 (review fed back to the same session; 13:05-15:09): fixed, added a text screen dump + render checks, and,
  given the vision tool, took headless-browser screenshots of five scenes, looked at them, and found and fixed two
  more bugs on its own (sleeping monsters looked awake, repeated log lines). Lesson: for a literal-minded model,
  feeding back what the result looks like works better than refining the spec up front.
- Run 3 (fresh session): procedural chiptune music and sound effects, verified by rendering to WAV offline
  (no playback on the machine) and looking at spectrograms.
- The same model also drew 8-direction SVG sprites (5 directions drawn, 3 mirrored) for 10 characters, one
  conversation per character so earlier drawings stay in context.

Engine observations from the 2.4 h run: one cold prefill (13.5K tokens, 36 s) for the whole run; decode ~25 tok/s
at 140-160K context. Problems: a reasoning repetition loop (one planning paragraph 13 times, ~27 min); chain resume
falling back to the last MTP-repairable checkpoint on every request (5-18K tokens re-read, ~20-30% of wall time;
once 149K tokens / 380 s after a mid-history edit) -> the tail-resume fix above; a KeyError when two clients
overlapped -> patch 0015.
