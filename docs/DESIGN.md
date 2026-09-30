# DESIGN: Hermes × Strata — fixed prefixes, persistent KV, parent/child agents (2026-09-30)

Origin: user ("Hermes の初期プロンプト固定 & KV 恒久保存化を一気通貫で"). Author: Claude. Implementers: Sol→Muse.
Replaces BRIEF-0121-ROUND2 T1. Keeps T2 (done, cd2030e) and T4 (implemented, needs review).

## 0. Goal and numbers

One user, one GPU, one engine slot. Traffic is Hermes: a parent agent (planner) and, via `delegate_task`, child
agents (implementer) that alternate on the same engine, plus small aux requests (titles).

| Situation | Today (0.1.21 + kv-resident) | Target |
|---|---|---|
| New Hermes session, server warm | ~34 s (11.7K prefix re-read; a small request before it pruned the cache) | ≤ 2 s |
| New session right after server restart | ~34 s | ≤ 5 s (prefix from disk) |
| Next turn of the same session | ~1-2 s | unchanged |
| Parent turn after a child ran (and vice versa) | full re-read of the whole conversation (30-200 s) | suffix only (≤ 3 s + suffix) |
| Title / aux request | prunes everything | never costs the main path anything |

Memory facts (measured): QSA KV int8 ≈ 12.7 KB/token (12 layers × 1056 B), MTP draft rows ≈ 1.06 KB/token,
recurrent state (GDN + PLE + QSA tails) ≈ 113 MiB per snapshot, independent of length. So a 12K-token prefix ≈
150 MiB KV + 113 MiB state; a 40K conversation tip ≈ 550 MiB. Host RAM 128 GB; H: HDD ~150 MB/s.

## 1. Model: three kinds of saved state

1. **Root** — the state at the end of a fixed prefix (tools + system prompt). Keyed by the hash of its token ids.
   Pinned, never evicted by traffic, persisted to disk. Hermes has one root per role (parent, child) — a handful.
2. **Tip** — the state at the end of the last request of one conversation *line* (a line = a root + the ids after
   it). One tip per recent line, host RAM, LRU with a byte budget (default 8 GiB). This is what makes parent↔child
   alternation cheap: the parent's tip survives while the child runs.
3. **Chain** — upstream's in-process `ConvCheckpoint` chain (turn boundaries of the *live* line). Unchanged.

Resume rule for a request with ids P: take the longest of {live chain match, any tip that is a prefix of P, any
root that is a prefix of P}; restore it (upload KV cells [0, k) + state), prefill P[k:]. Ties → prefer live chain
(no upload). A saved item is valid only if its full ids are a prefix of P (not just the hash) — compare ids.

A saved item contains: ids, recurrent state snapshot (existing ConvCheckpoint payload), QSA KV cells [0, k) for all
12 layers (int8 pages, host copy; with kv-resident the authoritative host KV already exists — copy the page range,
no D2H needed), MTP draft rows [0, k) (or re-derive by re-reading the last `window` cells if cheaper; measure).

## 2. Engine work (strata-up, generate.cpp + a new `saved_state.{hpp,cpp}`)

- E1. `SavedState` store: roots (pinned) + tips (LRU, `--tip-cache-gib`, default 8). Snapshot at request end
  (tip) and when the upstream root rule fires (root, PR #65: system-prompt end ≥ `--prompt-cache-root` tokens).
  Must not block the reply: take the snapshot after the last token is sent (the server already streamed it), or
  copy asynchronously on a side stream; the next request waits only if it needs that exact item.
- E2. Restore path: upload KV page range + state, re-point page table (kv-resident) or copy into the resident pool;
  keep upstream's pruning semantics for the live chain only.
- E3. Disk persistence for roots: `--root-dir <root>\strata-cache\roots`. File = header (engine version,
  model file sizes+mtimes or hash, pack path, kv type, flags that change numerics: --kv, --native, MTP dir) + ids +
  state + KV + MTP rows. Load lazily on first match (index file of id-hashes in RAM at startup). Write on root
  creation (background thread). Mismatched header → ignore file. Cap directory size (default 20 GiB, LRU by mtime).
- E4. Stats line per request: `resume from {chain|tip|root|root-disk} at k, suffix n, restore ms, snapshot ms`.
- E5. Tests: CPU unit tests for the store (LRU, budget, pin, prefix-by-ids check, header mismatch); GPU recipe for
  the owner (below).

## 3. Server work (strata-up/serve/server.py)

- S1. Aux yield: done in cd2030e — verify it composes with tips (aux request must not snapshot a tip that evicts a
  main line; aux lines get no tip or the lowest priority).
- S2. Expose `/v1/cache` (GET: roots/tips with ids length, bytes, last use; DELETE: drop all non-pinned) for
  debugging.

## 4. Hermes side (profile flashnext; owner/Claude does this, not Muse)

- H1. Prefix discipline: tools + system prompt byte-identical across sessions and turns; every volatile part
  (date, cwd listing, memory recall, session notes) goes after the fixed prefix — audit with
  `HERMES_DUMP_REQUESTS=1` and a diff of two sessions' prompts.
- H2. Roles: parent = planner (decides, predicts, accepts/rejects); child = implementer via `delegate_task`
  (isolated context, fixed child prefix = its own root). `delegation.max_concurrent_children: 1`, child timeout,
  output_schema for the implementer's report (files, tests, numbers, unfinished — no recommendations).
- H3. Implementer backends: (A) the same local Qwen (self-delegation), (B) the child calls Muse through
  `worker-oc.ps1` with a no-advice contractor agent. A/B these on the roguelike.

## 5. Acceptance (owner runs on GPU, 3 repeats each)

1. Two new Hermes sessions back to back, with a title request between them: second session prefill ≤ 2 s.
2. Restart server, new session: ≤ 5 s, log shows `root-disk`.
3. Parent turn → child task (different prefix) → parent turn: parent resumes from tip, suffix only.
4. 20-request mixed validation (dev-18101/step3_validate.py) cached vs cold: all sane.
5. Decode speed unchanged within noise (code / 12K / 40K shapes).
6. RAM: tips respect budget; disk dir respects cap.

## 6. Order

T4 review+commit → E1+E2 (+E4) → acceptance 1,3,4,5 → E3 → acceptance 2 → S2. Hermes H1-H3 in parallel by Claude.

## 7. Addendum 2026-09-30: forked children (origin: user idea "子が親のコンテキストをその場で分岐"; design: Claude)

- Engine requirement (E1): a tip MUST include the tokens the engine generated in that request (the assistant turn),
  not only the prompt — a forked child's prompt = parent's last prompt + the parent's generated assistant message
  (the delegate_task call) + a short tool-result, so it resumes from the parent's tip with a tiny suffix. After the
  child finishes, the parent resumes from its own tip (+ the child's report). Tip budget must hold ≥ depth+1 lines.
- Hermes side (H4, implemented by Muse, reviewed by Claude): `delegate_task` fork mode —
  child system prompt and tools[] byte-identical to the parent (no tool removal; blocked tools and the depth limit
  are refused at call time with a tool error), child messages = parent's messages + the assistant tool-call message +
  a tool result carrying the fork instruction (role, depth d/max, "do it yourself; delegate only clearly separable
  sub-work"). Child transcript is not merged into the parent. Depth via existing `delegation.max_spawn_depth`.
- Measure: how often forked children re-delegate (fork-bomb tendency) vs isolated children — decision-quality data.
