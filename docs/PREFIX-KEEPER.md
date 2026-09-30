# BRIEF: prefix keeper in serve/server.py (keep every Hermes request append-only for the engine cache)

Origin: user idea (2026-09-30: "Hermes と LLM の間で、キャッシュにある話題を見て残りをその上にポン置き";
"すべてを壊せ" = be aggressive, it's a personal project). Design: Claude. Implement: Muse. Review: Sol.
Repo/worktree: <root>\strata-fix (branch cache-fix-1 @ f621c84, = production engine). Python only
(serve/server.py + serve tests); no C++ change needed.

## Evidence (Claude, live Hermes on production f621c84, logs/accept/hermes-fork-test.txt)
- Every new Hermes session starts cold (35 s, `resume from none`): the root is taken at the end of the system message,
  and the system message's volatile tail differs between sessions by one line (`- Status: 2 modified, 75 untracked`
  vs `77 untracked`, workspace git snapshot). Six same-length, different-hash roots on disk prove it.
- After a forked child returned, the parent's next request was SHORTER (17603 tokens) than its previous one (19521):
  Hermes rewrote/pruned earlier history -> `resume from none`, 50 s. Find out which Hermes code path did this and
  report it (dump both requests: HERMES_DUMP_REQUESTS=1 or log them in the keeper) — do not change Hermes.

## Feature: `--prefix-keeper` (server flag; also env STRATA_PREFIX_KEEPER=1), default OFF, kill switch = drop the flag
The server keeps the last N (default 32) chat requests it forwarded (normalized messages + tools, LRU, in memory).
For each new chat request (not aux):
1. Find the stored request with the longest common prefix at MESSAGE level (same role, same tool_call ids).
2. System message: if it differs from a stored system message of the same tools[] only in a small number of lines
   (<= 8 changed lines and <= 5% of lines), send the STORED system message unchanged and append the change to the END
   of the last message's content as:
   `\n\n[context update]\n- <old line>\n+ <new line>` (one pair per changed line).
   Also applies across sessions (new session, same tools, system differs only in volatile lines): this is what makes
   the root hit.
3. History: if the new request = stored request with some earlier messages removed or shortened (content is a
   truncation/prefix, or replaced by a shorter summary of the same role/tool_call_id), plus appended messages:
   re-insert the stored versions so the new request is exactly stored + appended. Only if the estimated total stays
   under max_context - 8192 tokens (use the server tokenizer); otherwise forward Hermes' version unchanged.
4. Anything else (reordered messages, changed assistant tool calls, changed tools[]): forward unchanged.
5. Every repair and every detected non-append mutation (repaired or not) is logged one line per event to stderr as
   `strata serve: keeper <kind> msg=<i> ...` (kinds: system-delta, history-restore, mutation-unrepaired, skip-budget)
   and to a jsonl file next to the engine log (`prefix-keeper.jsonl`: request id, kind, index, bytes before/after,
   short diff). No full prompt text in logs except the short diff.
6. Streaming and non-streaming, OpenAI + Anthropic-style endpoints if the server has both; tool-call parsing unchanged.

## Tests (CPU, serve test suite)
- system volatile line change -> stored system + [context update] appended; >8 lines changed -> passthrough.
- history truncation of a tool result -> restored; reordered/changed assistant tool_call -> passthrough + mutation
  logged; budget overflow -> passthrough + skip-budget.
- aux requests untouched; flag off -> byte-identical passthrough.
Run the full server suite (72 tests currently).

## GPU acceptance (Claude runs after your commit; do not run): scripts/hermes_keeper_accept.ps1
- new session after git-status change: first request resumes ROOT, prefill <= 2 s; the model reports the NEW
  untracked count (it sees the [context update]).
- parent after forked child: never `resume from none`.

## Rules
Commit on cache-fix-1 with explicit files (strata-fix git dir is writable for you). No GPU, no production, never touch
strata-prod*, deploy-strata, port 18100, GPU clocks; no push. Report: briefs/REPORT-PREFIX-KEEPER.md (files, tests,
the Hermes history-rewrite cause, gaps; attribution origin user / design Claude / implemented Muse / reviewed Sol).
