# BRIEF: Hermes self-managed work deadline ("18時まで開発してて")

Origin: user (2026-09-30: "そういうツール類をHermes側が管理して欲しい"; morning run: told "18時まで開発してて", Hermes
ended its turn at 08:49 saying there is no way to keep going). Design: Claude. Implement: Muse. Review: Sol.
Repo: <root>\hermes-dev (git worktree of hermes-agent, branch local-single-slot-dev). Production hermes-agent
(%HERMES_HOME%\hermes-agent) must NOT be touched.

Note: `agent.run_budget_seconds` / `--run-budget` already exists but is a CEILING (stops the turn, 80% wrap-up notice,
see agent/agent_init.py, conversation_loop._maybe_inject_run_budget_wrapup). This feature is the opposite: a FLOOR.

## Feature
1. Tool `set_work_deadline(until: str, goal: str)` — `until` is local wall-clock "HH:MM" (today, or tomorrow if
   already past) or ISO datetime. The model calls it when the user asks it to keep working until a time. Stores
   deadline + goal on the agent (per session; persisted with the session so `--resume` keeps it). Returns the
   parsed deadline and minutes left. Calling again replaces it; `until: "none"` clears it.
2. Tool `finish_work(reason: str)` — the ONLY way to end early while a deadline is active. Reason is logged and shown
   to the user (e.g. blocked on a human decision, environment broken, goal fully met with nothing sensible left).
3. Conversation loop: when the model produces a final answer (no tool calls) while a deadline is active, is in the
   future, and finish_work was not called in this turn: do not end the turn. Append the assistant message, then a
   user-role continuation message:
   `[deadline {HH:MM}, {N} min left] Record progress where your task expects it, then choose the next most valuable
   improvement toward "{goal}" yourself and continue. Do not ask the user. To stop before the deadline, call
   finish_work(reason).` Keep it short and byte-stable apart from the numbers (single-slot KV cache friendliness).
4. Stop conditions (whichever first): deadline reached (let the current model call finish, then end normally with a
   one-line "deadline reached" note), finish_work called, existing run_budget / max-turns / iteration limits,
   user interrupt. Existing limits keep priority over the deadline.
5. Guard against spin: if N consecutive continuations (config `agent.deadline_max_idle_continuations`, default 3)
   produce no tool calls at all, end the turn with a note.
6. Config: `agent.deadline_enabled` (default true). Tools available in CLI chat and gateway; not to delegated
   children (children inherit nothing; the parent owns the deadline).
7. System prompt: one sentence in the STABLE tier listing when to use set_work_deadline (keep prefix byte-identical
   across sessions; nothing time-dependent in the stable tier).

## Tests (tests/agent/test_work_deadline.py, mock model, no network)
- parse "18:00" before/after now, ISO, "none"; final answer before deadline -> continuation injected; after deadline
  -> turn ends; finish_work ends early with reason; idle-continuation guard; run_budget ceiling still wins;
  children do not get the tools. Run existing loop tests too.
Run with the dev venv the same way as the previous fork-mode work (see briefs/BRIEF-HERMES-FORK.md notes).

## Rules
Commit on local-single-slot-dev with explicit files; no push. Report to briefs/REPORT-HERMES-DEADLINE.md: files,
tests + results, known gaps. Attribution: origin user / design Claude / implemented Muse / reviewed Sol.
