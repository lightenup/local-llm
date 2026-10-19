# local-llm — Qwen3.8-27B-8bit profile setup for oMLX

A measured, reproducible profile setup for `mlx-community/Qwen3.8-27B-8bit`
served by oMLX on an M5 Max / 128 GB MacBook Pro.

Full measurements and rationale: **[docs/FINDINGS.md](docs/FINDINGS.md)**.
What is planned next, across the whole local stack: **[docs/backlog.md](docs/backlog.md)**.

## What you get

Four behaviours, each served as its own model ID. Point any OpenAI-compatible
client at `http://127.0.0.1:8000/v1` and pick behaviour by model name — no
config switching, no engine reload.

| model id | use it for | temp | thinking | context |
|---|---|---|---|---|
| `Qwen3.8-27B-8bit:qwen-code` | writing and editing code | 0.7 | off | 131k |
| `Qwen3.8-27B-8bit:qwen-agent` | agentic tool use, low latency | 0.3 | off | 131k |
| `Qwen3.8-27B-8bit:qwen-reason` | hard reasoning, architecture | 0.6 | on (8k budget) | 131k |
| `Qwen3.8-27B-8bit:qwen-long` | whole-repo / long-document analysis | 0.3 | off | **262k** |

All four share one resident engine with MTP speculative decoding enabled, so
switching between them costs nothing and every one of them decodes at
**~36–40 tok/s** instead of the stock ~18.

### Headline results

| | baseline | shipped |
|---|---|---|
| decode @ 16k prompt | 17.3 t/s | **34.0 t/s** (1.97×) |
| max usable context | 32,768 (config cap) | **262,144** (verified) |
| `top_k` | 0 (disabled — a defect) | 20 (per checkpoint) |

Validated end to end by ~40 hours of hands-off benchmarking (docs/FINDINGS.md §8):

- **MTP changes nothing about the output.** On four of six eval suites, 1,664
  questions produced identical verdicts question for question; the other two
  diverged well inside chance and in MTP's favour.
- **The 1.8× is single-stream only.** It is 1.74× at batch 1 and 0.99× at batch 2
  and above. Correct for how a coding client actually calls it, worthless for
  batch serving.
- **`qwen-reason`'s thinking budget stays on.** With it off, one hard prompt in
  eight burned all 32,768 tokens and returned no answer at all.
- **Long-context retrieval is clean**: 15/15 needles at five depths and 3/3
  multi-hop, verified to 136k tokens.
- **Dead ends, so you can skip them:** ANE prefill, TurboQuant KV, the in-memory
  hot cache, `chunked_prefill`, `specprefill`, and raising the concurrency limit.

## Quick start

```bash
# sanity check the server, model and profiles
python3 bench/omlx_client.py

# confirm the four profiles are stored and served
python3 profiles/apply.py --verify-only

# behavioural check of all four (code / tool call / reasoning / 46k needle)
python3 bench/smoke.py

# whole-stack self-check: host, oMLX, queue, LiteLLM, opencode (manual, not scheduled)
python3 bench/doctor.py            # --quick for config + cheap probes only
```

The doctor is run by hand. Nothing schedules it or watches its result, so run
it after any change and before long jobs. See [docs/DOCTOR.md](docs/DOCTOR.md).

Use it:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $OMLX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.8-27B-8bit:qwen-agent",
       "messages":[{"role":"user","content":"hello"}]}'
```

## From opencode

All four profiles are wired up, with each agent pinned to the profile that fits
its job, so normal use needs no switching:

| agent | model | why |
|---|---|---|
| `build` (default) | `Qwen3.8-27B-8bit:qwen-code` | code quality at N=1 (FINDINGS §14) |
| `plan` | `Qwen3.8-27B-8bit:qwen-reason` | reasoning; oQ4e loses it at p=0.0095 |
| `explore`, `general` | `oQ4e:oq-agent` | 1.73× decode, resident alongside the 8-bit |
| `title`, `summary`, `compaction` | `oQ4e:oq-agent` | quality-insensitive; frees the 8-bit |
| `speckit` | `Qwen3.8-27B-8bit:qwen-reason` | reasoning |
| `speckit-wide` | `Qwen3.8-27B-8bit:qwen-long` | 262k; `oq-long` also reaches it without eviction |

Or address them by **role** through the proxy — `coder`, `reasoner`, `fast` —
so the model behind a role changes in one line. See `proxy/`.

The split is quality against speed: `oQ4e` is 1.73× the 8-bit's decode and
measurably worse on both code (p = 0.00119) and reasoning (p = 0.0095), so it
takes the speed-dominated, quality-insensitive work while the 8-bit keeps
everything whose correctness is not independently checked.

### Coder-Next was retired on 2026-09-12

It held `explore`/`general`/`title`/`summary` for its 3× prefill. Two things it
had never been measured against took the job away:

- **Quality.** It is **10.8 livecodebench points worse** than the 4-bit sibling
  now in the role — 43.2 against 54.0, p = 0.00006.
- **Residency.** At 64.7 GB it could not stay loaded beside the 8-bit once the
  memory guard reserved KV, so **every alternation cost ~20 s** of evict and
  reload — on a routing table that alternates on almost every call. At a 16k
  prompt its prefill advantage saves 13.9 s, so break-even was ~1.4 prefills per
  alternation. It was losing.

`oQ4e` is 16.0 GB, and alternates with the 8-bit at **2.6 s and zero evictions**
(measured, 8 consecutive swaps). It has *no* prefill advantage — same
architecture as the 8-bit, 798 vs 793 tok/s — so the one thing Coder-Next was
genuinely better at is simply gone from the lineup. Keep it on disk for a
long-context scan that stays on one model across many large prefills; nothing
here does that. Details: docs/backlog.md §A2c.

> **Memory, for when a third model is considered.** The guard's per-request KV
> projection is prompt-linear at ~45 KB/token plus ~0.42 GB fixed. It is *not*
> the "~9× over-estimate" an earlier draft of this paragraph claimed — FINDINGS
> §18 retracted that, and in the opposite direction. Budget against the guard's
> projection, not the weights: that is what made a 94 GB pair unworkable on a
> 128 GB machine, and what makes the current 44 GB pair comfortable. But do not
> read every refusal as a residency problem. §35 caught the preflight guard
> refusing an 18-token prompt on frozen numbers. §36 traced a large-prompt
> refusal to pinning: the guard could not evict the idle model, which would
> have freed enough. No model is pinned now. See docs/FINDINGS.md §18, §35, §36
> and docs/backlog.md §A3b.

### Qwen3.8-Flash-Next is on trial, not in the lineup (2026-09-13)

`Qwen3.8-Flash-Next-oQ4e-mtp` (125B/6B-active, 69.4 GB resident) is downloaded and
profiled as `fn-code`/`fn-agent`/`fn-reason`, and is deliberately wired to **no
role alias** — nothing routes to it. Measured so far: MTP works and is worth
1.87×, decode is 74.57 tok/s (the 16 GB fast tier's speed at 4.3× its memory), and
it cannot be used concurrently with the lineup at 63.5 s per swap. The axis it was
fetched for, agentic work, is still unmeasured and needs the proxy restarted.

**To pick this up: docs/backlog.md §A6, "How to resume this arm"** — blockers,
exact invocations, and which result files exist. Full measurements in
docs/FINDINGS.md §26, which also corrects §7's presence-penalty rule and records
that oMLX's own pool already does LRU eviction, pinning and idle TTL.

Sampling is owned by the profile, not the client: all four set `force_sampling`,
so oMLX ignores any `temperature`/`top_p`/`top_k` a client sends. Without it
opencode's own default (0.55 for Qwen models) silently replaced every profile's
temperature. See docs/FINDINGS.md §7.

```bash
opencode models omlx                                  # list the four
opencode run -m omlx/Qwen3.8-27B-8bit:qwen-long "…"   # force one
opencode run --agent plan "…"                         # uses qwen-reason
```

The config lives in **`~/Development/private/dotfiles/opencode/opencode.json`**
(symlinked to `~/.config/opencode/opencode.json`), so change it there, not in
`~/.config`. Note that `omlx launch opencode` overwrites that file, following
the symlink into the dotfiles repo and dropping every profile entry — the
dotfiles README warns about this too. Don't run it.

## Layout

```
bench/omlx_client.py     API client — auth, bench polling, profiles, timed chat
bench/throughput.py      prompt-length throughput sweep, labelled per variant
bench/context.py         probe the real max context under the memory guard
bench/accuracy.py        queue the bundled eval suites (16 available)
bench/paired_compare.py  two runs compared question by question (McNemar), no GPU
bench/ane_tune.py        ANE prefill auto-tuner (+ --apply)
bench/set_variant.py     switch engine variants: plain/ane/mtp/tqkv/specprefill/…
bench/smoke.py           per-profile behavioural check + reload detection
bench/thinking_budget_ab.py  reason profile: budget throughput vs runaway risk
bench/cache_ab.py        prefix-cache measurement + in-memory hot cache A/B
bench/concurrency.py     multi-client concurrency: scaling and profile mixing
bench/longctx_quality.py multi-needle retrieval and multi-hop at 65k/131k/262k
bench/quant_compare.py   4-bit sibling: download, throughput, accuracy vs 8-bit
bench/quant_id.py        classify the quant build of any MLX repo, local or Hub
bench/model_scout.py     watch the Hub for new MLX models that fit (docs/SCOUT.md)
bench/kv_shape.py        what the prefill memory guard reserves, and why
bench/cache_probe.py     prefix-cache hit rate across a real session (B1a)
bench/proxy_check.py     tool calls survive the proxy hop; proxy overhead
bench/verifier.py        verifier runner: command/syntax/XML, chained
bench/taskbench.py       task-shaped metrics: render/XML/tool-call/behaviour
bench/bon_gate.py        best-of-N gate (closed; kept as the record)
bench/c9_decide.py       best-of-N vs the strong model, head to head
bench/memory_probe.py    provoke the prefill guard and read its arithmetic
bench/oq4e_overnight.sh  the oQ4e arm: residency, throughput, accuracy
bench/codernext_*.sh     the retired Coder-Next runs, kept as the record
proxy/                   LiteLLM + Postgres, role aliases, local-only key
proxy/launchd/           LaunchAgent templates + install.sh (llm_queue, inference_awake)
scripts/check_public.sh  pre-push check: no secrets, no machine-specific values
bench/lcb_audit.py       sort livecodebench failures into causes, no GPU
bench/lcb_io_fix.py      retry I/O-shaped failures with the contract stated
bench/best_of_n.py       best-of-N cost and pass@N against a verifier
profiles/oq4e.py         the three oQ4e profiles (the fast tier)
profiles/codernext.py    the retired Coder-Next profiles, kept for reversal
bench/report.py          summarise results/ as a table
bench/unattended.sh      the long hands-off chain (phases A-E, see below)
bench/followup_safety.sh safety suites, chained after the overnight run
bench/overnight.sh       the long validation run (see below)
profiles/definitions.py  the four profiles + invariant checks
profiles/apply.py        create/refresh/verify profiles
results/                 every measurement, as JSON (raw *.log stay local)
results/backup/          original oMLX settings before any change (local only)
docs/FINDINGS.md         what was measured and why each lever was chosen
docs/backlog.md          what is not done yet, across the whole local stack
docs/SCOUT.md            model scout runbook, for the agent that schedules it
scout/                   scout policy (scout.toml) and the Mac's exported setup.json
```

## The one design rule

Profiles carry **only** universal fields (sampling, thinking, context, token
caps). Engine-level fields (ANE prefill, TurboQuant KV, MTP, DFlash) live once on
the base model.

Reason: engine-level fields are part of oMLX's load-time variant signature, so a
profile differing on any of them forces a ~28 GB engine reload every time you
switch. `profiles/definitions.py::validate()` and `profiles/apply.py` both check
this, and `bench/smoke.py` counts engine loads to catch a regression.

The corollary constraint: the shared variant uses MTP, which oMLX disables for
any request needing a logits processor. So no profile may set
`repetition_penalty` ≠ 1.0, `presence_penalty` ≠ 0.0, or `guided_grammar_enabled`.
`thinking_budget` is fine. This is enforced in `validate()`.

## Changing something

```bash
# edit profiles/definitions.py, then
python3 profiles/apply.py --dry-run
python3 profiles/apply.py
python3 bench/smoke.py

# A/B an engine-level change (this DOES reload the engine)
python3 bench/set_variant.py apply plain
python3 bench/throughput.py my_experiment --lengths 4096 16384
python3 bench/set_variant.py apply mtp        # back to shipped
```

## Overnight validation

```bash
bash bench/overnight.sh 2>&1 | tee results/overnight_$(date +%F).log
```

Settles the accuracy floor, whether MTP is output-neutral (the one unvalidated
assumption), the `qwen-code` temperature choice, the throughput curve including
batching, and whether `qwen-reason`'s thinking budget is worth the 1.8× decode it
costs.

**Budget ~33 hours at `SAMPLES=500`, not the 6–8 this section used to claim.**
`livecodebench` costs ~50 s per question against ~4 s for `mmlu_pro`, and the code
group runs four times, so that one suite is ~25 h of the total. If you change the
suite mix, size it from the per-suite `time_s` in `results/acc_*.json` rather than
from question counts.

`SAMPLES` defaults to **500 per suite**, not the full datasets. Full datasets
are ~101k questions across these passes, which measured out at 36–44 hours;
500 gives a standard error near ±2.2%, so ±4.4% at 95% on a single score and
about ±6.2% when comparing two. That is coarse: it cannot resolve a
1-point sampling difference, so judge small changes with `bench/paired_compare.py`,
which compares two runs question by question and needs no GPU. Override with
`SAMPLES=0` for the full run if you have days to spare.

Two things the earlier draft of this script ran and this one deliberately skips,
because they were the bulk of that 36–44 hours: the thinking-enabled reasoning
pass (15–20 h alone) and the TurboQuant 4-bit KV A/B (`bench/context.py` already
reached the full 262,144 ceiling without it, so it has no problem left to solve).

The run leaves MTP off during step 2, writes base-model sampling during step 3
and toggles `qwen-reason`'s thinking budget during step 5, so it installs an
`EXIT` trap that clears sampling and re-applies both the shipped variant and the
profiles if it dies partway through.

## Unattended chain

`bench/unattended.sh` runs the experiments that need hours and no supervision.
It waits on a predecessor pid, runs phases in ascending cost, and guarantees the
shipped setup is handed back.

| phase | what | est. |
|---|---|---|
| A | prefix-cache measurement, then hot cache at 20 GB | ~45 min |
| B | MTP across batch 2/4/8, then real multi-client concurrency | ~2 h |
| C | `chunked_prefill` and `specprefill` A/Bs at long prompts | ~1.5 h |
| D | long-context quality: multi-needle and multi-hop to 262k | ~4 h |
| E | 4-bit sibling: download, throughput, code+reason accuracy | ~9 h |

Phases A to D have run; their results are in docs/FINDINGS.md §8. Phase E is fixed
but still unrun.

```bash
WAIT_PID=<pid> DEADLINE="2026-09-10 06:00" bash bench/unattended.sh
PHASES=AB bash bench/unattended.sh          # subset
```

Safety properties, because this touches global settings and engine variants:

- Each phase script restores what it changed in its own `finally`.
- The `EXIT` trap re-applies the shipped variant, the shipped global settings
  (`hot_cache_max_size: 0`, `chunked_prefill: false`) and all four profiles,
  then verifies and smoke-tests, then prints a state report.
- `DEADLINE` stops new phases starting; a watchdog hard-stops a running phase
  30 minutes past it, so the restore always happens before you are back.
- Phase A leaves the hot cache **off** even if it wins. Enabling it is a
  deliberate decision, not something a benchmark should do to a live setup.

> **Heads-up:** oMLX uploads any accuracy suite result with ≥100 questions to
> `omlx.ai` community benchmarks, including a machine hash and your model
> settings. There is no opt-out setting. Run `SAMPLES=99 bash bench/overnight.sh`
> to stay fully local. See docs/FINDINGS.md §11.

## Restoring

```bash
# profiles only
python3 -c "import sys;sys.path.insert(0,'bench');from omlx_client import Omlx;\
o=Omlx();[o.delete_profile(p['name']) for p in o.list_profiles()]"

# full original oMLX config (then restart oMLX)
cp results/backup/settings.json.orig ~/.omlx/settings.json
cp results/backup/model_settings.json.orig ~/.omlx/model_settings.json
```

## Environment

Overridable: `OMLX_BASE` (default `http://127.0.0.1:8000`), `OMLX_MODEL`
(default `Qwen3.8-27B-8bit`), `OMLX_API_KEY`.

The API key is never hardcoded: if `OMLX_API_KEY` is unset the client reads
`auth.api_key` from `~/.omlx/settings.json`.

Machine-specific settings go in two untracked files, each with a committed
example: `.env.local` (LaunchAgent label prefix and log directory, read by
`proxy/launchd/install.sh` and `bench/doctor.py`) and `proxy/.env`
(proxy secrets, optional corporate root CA). Where the docs need a LAN address
or host they use a placeholder such as `<mac-lan-ip>`; an untracked `LOCAL.md`
resolves them on the author's machine.

## License

MIT, see [LICENSE](LICENSE).
