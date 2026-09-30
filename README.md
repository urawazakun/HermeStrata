# HermeStrata

A local agent stack for **one user, one GPU, one engine slot**:
[Hermes Agent](https://github.com/NousResearch/hermes-agent) (planner/implementer agents, `delegate_task`) driving
[Strata](https://github.com/Niko1221/Strata) (hybrid-attention MoE inference engine) serving Qwen3.8-Flash-Next
IQ3_S at 262K context on a single **Tesla V100 16 GB** (Windows, CUDA 11.8, sm_70).

This repo is not a fork. It contains the patches, the cache design that ties the two together, the launch/acceptance
scripts, and the measurements. Both upstreams are MIT-licensed; the patches keep their licenses.

> Status: experimental, personal setup. Nothing here is upstreamed.

## Why

Hermes is a multi-agent harness: a parent turn, then a child task, then the parent again, plus small side requests
(session titles). Upstream Strata keeps **one live conversation** of saved state; any unrelated request prunes it.
With a 12K-token Hermes system prompt + tools, and conversations that grow to 100K tokens, every switch between
parent, child and title meant re-reading the whole prompt (34 s for 12K, minutes for 100K on this GPU).

Why upstream keeps a single line (our reading): the model's recurrent state (GDN + PLE + QSA tails) cannot be
truncated like a KV cache — you can only return to points where a ~113 MiB snapshot was taken — and each extra
line costs GBs of host RAM and adds ways to restore the wrong state silently. For a single chat on a 32-64 GB box,
one line is the right trade-off. With 128 GB host RAM and an agent harness, several lines pay off.

## What is in it

| Layer | Change | Where |
|---|---|---|
| Strata | V100 / CUDA 11.8 port (sm_70 floor, Windows fixes) | `patches/strata/0001-0002` |
| Strata server | auxiliary requests (titles: no tools + `response_format`) wait for the main turn and never evict its state | `0003` |
| Strata | adaptive MTP draft window (`--spec-adapt`) from measured acceptance and cost | `0004` |
| Strata | **saved states**: pinned *roots* (fixed prefix = tools + system prompt) and LRU *tips* (end of each conversation line) in host RAM, restore of the longest validated prefix, per-request resume/snapshot stats | `0005-0008` |
| Strata | roots persisted to disk (lazy load, identity check, size cap, `--no-root-disk`), aux composition tests, `/v1/cache` inventory + DELETE of non-pinned tips | `0009-0011` |
| Strata | fixes from GPU acceptance: fall back to the next valid restore point instead of cold (F1); tips keep a restore point at the last turn boundary of the prompt, not only the generation end (F2/F2b) | `0012-0016` |
| Strata server | **prefix keeper** (`--prefix-keeper`): keeps Hermes requests append-only for the cache — volatile system lines become a trailing `[context update]`, pruned history is restored; idea by the owner | `0017` (live: new session after a workspace change resumes the root) |
| Hermes | fixed prefix discipline: git workspace snapshot moved to the volatile tail (`HERMES_WORKSPACE_LATE=1`), title generated after the turn (`HERMES_TITLE_AFTER_TURN=1`) | `patches/hermes-agent/0001` |
| Hermes | `delegate_task` **fork mode**: a child starts from the parent's exact prompt (+ its tool call + a tool result carrying the task), so the engine resumes it from the parent's saved state; blocked tools and depth limit are refused at call time instead of being removed from `tools[]` | `patches/hermes-agent/0002` |
| Hermes | **work deadline** tools (`set_work_deadline` / `finish_work`): "work until 18:00" keeps the agent going until the deadline; run budget stays the ceiling | `patches/hermes-agent/0003` (experimental) |

Design: [docs/DESIGN.md](docs/DESIGN.md). How the fixes were found: [docs/KNOWN-ISSUES.md](docs/KNOWN-ISSUES.md).
Acceptance ledger: [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md). Live Hermes runs and what they exposed: [docs/LIVE-HERMES.md](docs/LIVE-HERMES.md).

Memory cost per saved line (measured unit costs): KV int8 12.7 KB/token, MTP rows 1.06 KB/token, recurrent state
113 MiB per snapshot. A 12K root ≈ 0.27 GB; a 100K conversation tip ≈ 1.5 GB. Default tip budget 8 GiB.

## Results (2026-09-30, V100 16 GB, ctx 262144, `--kv int8 --kv-resident 20480`, expert cache 3127, 3 repeats)

Hermes system prompt + tools = 11.7K tokens. Cold prefill of that on this GPU: **34 s**.

| situation | before (upstream behaviour) | HermeStrata |
|---|---|---|
| new session after a title (aux) request | 34 s | **0.50-0.66 s** (root) |
| parent turn after an isolated child ran | full re-read | **0.72-0.77 s** (tip at the turn boundary, 26 tokens read) |
| forked child | full re-read | **1.7 s** (parent's chain point, 64 tokens read) |
| isolated child, 2nd time | full re-read | **0.17 s** (its own tip) |
| identical request repeated | reuse | 0.45-0.53 s |
| first session after an engine restart | 34 s | **2.5 s** (root from disk) |

Restore correctness: a fact planted in the first user message is recalled by the forked child 3/3. 20 mixed
Hermes-shaped requests cached vs truly cold: all sane. Token parity is deliberately not a criterion (quantized
kernels + KV paging drift run to run). Decode speed unchanged: 26.7 vs 26.5 tok/s (8K doc). `--spec-adapt`
showed no measurable gain here and stays off.

Not yet measured on GPU: tip-budget eviction under pressure (item 6 of the ledger).
## Layout

```
patches/strata/         git format-patch from Niko1221/Strata 0.1.21 base (f1b1d961...)
patches/hermes-agent/   git format-patch against the Hermes Agent snapshot used by this project
docs/                   design, fix history, acceptance ledger
scripts/                server launcher (PowerShell), Hermes launcher (.cmd), GPU acceptance driver
```

Scripts resolve the project root from `HERMESTRATA_ROOT` (default: the parent of `scripts/`); model and pack paths come from the launcher's config section. Paths only matter at startup, not for speed.

## Patch bases

The Strata series is pinned to upstream commit
`f1b1d961537fd66d37fee68a60015701375b7b5a` (Strata 0.1.21). The preimage blob IDs in
`patches/strata/0001-*.patch` match that commit for every file it changes. Do not assume the series applies
cleanly to current Strata `main`; upstream has moved since this snapshot.

The Hermes patches likewise carry their exact preimage blob IDs in the format-patch headers, but this repository
does not currently record a single upstream commit SHA for that snapshot. Treat a rebase onto current Hermes Agent
as a separate compatibility task rather than silently applying with rejects.

## Credits

Strata: Niko1221 and contributors. Hermes Agent: Nous Research. Design and review in this repo: the owner with
Claude (Anthropic); implementation of the cache patches: Muse (via OpenCode) orchestrated and reviewed by Sol (Codex).
