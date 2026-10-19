# Qwen3.8-27B-8bit on oMLX — measured findings

Host: MacBook Pro, **M5 Max**, 40 GPU cores, 128 GB, macOS 26.6.2
Model: `mlx-community/Qwen3.8-27B-8bit` — arch `qwen3_5`, 64 layers, hybrid
attention (3× `linear_attention` + 1× `full_attention`, `full_attention_interval: 4`),
head_dim 256, native context **262,144**, VLM engine, tool parser `qwen3_coder`
Resident: 27.74 GB. Memory-guard ceiling: 115.4 GB.
Date: 2026-09-05, with sections 4 and 8 to 10 revised 2026-09-10 after the
overnight and unattended validation runs. All numbers produced by the scripts in
`bench/`.

---

## 1. Starting state was untuned

| Setting | Was | Problem |
|---|---|---|
| `sampling.max_context_window` | 32,768 | discarded 87% of the model's 262k context |
| `sampling.top_k` | **0** (disabled) | checkpoint's own `generation_config` asks for `top_k: 20` |
| profiles / templates | none | no way to vary behaviour per workload |
| every accelerator | off | ANE prefill, MTP, DFlash, TurboQuant KV all disabled |

Both global defects are fixed (`results/backup/settings.json.orig` holds the original).

---

## 2. Baseline — all accelerators off

`results/tp_baseline.json`, corpus `code_python`, 128 generated tokens.

| prompt | TTFT | prefill t/s | decode t/s | peak GB | GPU |
|---|---|---|---|---|---|
| 1,024 | 1.2 s | 831 | 18.2 | 28.3 | 99.8% |
| 4,096 | 4.6 s | 900 | 18.0 | 29.8 | 99.8% |
| 16,384 | 20.8 s | 788 | 17.3 | 31.7 | 99.7% |
| 65,536 | **112.3 s** | 584 | 15.7 | 39.2 | 99.7% |

GPU was pinned at ~99.8% throughout, i.e. the GPU is the bottleneck and the
ANE was idle.

---

## 3. ANE prefill offload — rejected, with evidence

The GPU being saturated made ANE offload the obvious hypothesis. The bundled
auto-tuner (`bench/ane_tune.py`, `results/ane_tune.json`) swept the split
candidates at seq 2048:

| candidate | prefill t/s |
|---|---|
| **GPU only** | **855.6** |
| Predicted optimum (ANE) | 799.9 / 741.6 |
| Profile-refined optimum (ANE) | 788.5 / 773.8 |

Verdict: `{"enabled": false, "speedup_percent": 0.0, "processing_tps": 833.4}`.

**ANE prefill stays off.** On M5 Max the GPU beats every ANE/CPU split for this
model. This is consistent with oMLX's own startup decision:
`Qwen FA-256 steel patch skipped: NAX GPU, stock SDPA is faster`.

The `ane` variant is kept in `bench/set_variant.py` so the finding stays
re-checkable on future hardware or oMLX versions.

---

## 4. MTP speculative decode — the big win, shipped

`mlx-community/Qwen3.8-27B-MTP-8bit` is the **MTP drafter for this exact
checkpoint**: 425M params, 430 MB of weights, `model_type: qwen3_5_mtp`,
block size 3. oMLX registers it as a helper model and attaches it via
`vlm_mtp_enabled` (the VLM engine path).

`results/tp_mtp.json` vs baseline:

| prompt | decode baseline | decode + MTP | gain |
|---|---|---|---|
| 1,024 | 18.2 | **32.3** | 1.77× |
| 4,096 | 18.0 | **33.2** | 1.84× |
| 16,384 | 17.3 | **34.0** | 1.97× |
| 65,536 | 15.7 | **25.7** | 1.64× |

Prefill is unchanged (expected — drafting only affects decode). Peak memory
+5 GB. GPU utilisation drops to 80–97%, i.e. the drafter fills previously
serialised decode time.

Steady-state check through the served profiles (400-token generation, warmed):

| model id | decode |
|---|---|
| `Qwen3.8-27B-8bit` | 36.2 t/s |
| `Qwen3.8-27B-8bit:qwen-agent` | 39.9 t/s |
| `Qwen3.8-27B-8bit:qwen-code` | 40.2 t/s |

**MTP is enabled on the base model and inherited by all four profiles.**

### The 1.8x is a single-stream effect and vanishes under batching

`results/tp_mtp_batchsweep.json` against `results/tp_plain_batchsweep.json`,
same corpus, 256 generated tokens. Aggregate decode is the sum across streams;
per-stream is what a single caller experiences.

| batch | MTP aggregate | plain aggregate | MTP per stream | plain per stream | MTP gain |
| --- | --- | --- | --- | --- | --- |
| 1 | 31.3 | 18.0 | 31.3 | 18.0 | **1.74x** |
| 2 | 36.0 | 36.5 | 18.0 | 18.25 | 0.99x |
| 4 | 65.4 | 67.2 | 16.35 | 16.8 | 0.97x |
| 8 | 90.9 | 92.2 | 11.36 | 11.53 | 0.99x |

The drafter's entire advantage disappears at batch 2 and never returns. This is
the expected shape rather than a defect: speculative decoding wins by filling
decode slots that a single sequence leaves idle, and a second concurrent sequence
fills them for free. Once the batch does that job, drafting adds verification
work for no gain, which is why the ratio sits marginally below 1.0.

Confirmed independently by the accuracy suites, which ran identical workloads at
batch 8 with MTP on and off: wall-clock ratios of 0.99x to 1.07x across six
suites, median 1.02x.

Two consequences:

- **The shipped configuration is correct for its actual use.** opencode issues
  one request at a time, so the profiles really do get 1.74x.
- **Do not expect MTP to help throughput work.** For batch serving it is neutral
  at best. It is also not worth disabling, since 0.99x is inside noise and
  switching would cost an engine reload.

Batching itself is a genuine aggregate win the drafter cannot provide: plain
decode goes from 18.0 t/s single-stream to 92.2 t/s at batch 8, a 5.1x aggregate,
at the cost of per-stream decode falling to 11.5 t/s. See §8 for why that
aggregate does not survive contact with real HTTP concurrency.

### Why base-model MTP (`mtp_enabled`) is not usable
The 8-bit checkpoint reports:
> Config declares MTP layers but the weight files contain neither `mtp.*`
> tensors nor native nextn layers.

So the in-checkpoint MTP path is unavailable and `import-mtplx` does not apply
(that needs an MTPLX side-car with `mtp.safetensors` + `mtplx_runtime.json`,
which this repo does not ship). The separate drafter is the correct route here.

### The exclusivity constraint that shapes every profile
`omlx/model_settings.py::vlm_mtp_processor_conflicts` disables `vlm_mtp` for any
request needing a logits processor. To keep the 1.8× decode, every profile must
leave these unset or neutral:

- `repetition_penalty` — must be `None` or `1.0`
- `presence_penalty` — must be `None` or `0.0`
- `guided_grammar_enabled` — must be `False`

`thinking_budget_enabled` is **safe** — the MTP path applies it at verify time
via `MTPProcessingSampler`. `min_p: 0.0` was verified safe empirically (the
`qwen-code` measurement above sets it and still gets 40 t/s).

`profiles/definitions.py::validate()` enforces this so it cannot regress.

---

## 5. Context ceiling — full 262,144 confirmed on plain KV

`results/ctx_mtp.json`:

```json
{"measured_tokens": 262144, "verified_tokens": 262144, "applied_tokens": 262144,
 "applied": true, "capped_by": "target", "attempts": 1,
 "prefill_tps": 257.7, "duration_s": 1029.1}
```

`capped_by: "target"` means the probe hit the requested ceiling, **not a memory
limit** — the machine has headroom beyond the model's own native maximum.

Consequence: **TurboQuant 4-bit KV is not required for long context.** That
matters, because `turboquant_kv_enabled` is a load-time variant field; putting it
in only the long-context profile would force a ~28 GB engine reload on every
switch. It stays off, and long context runs on lossless KV.

Honest cost of very long prompts — prefill degrades with length:

| filled to | prefill t/s |
|---|---|
| 6k | 811 |
| 35k | 608 |
| 121k | 284 |
| 262k | 137 (258 avg over the whole fill) |

A full 262k prefill takes ~17 minutes. Long context is for analysis runs, not
interactive use; the prefix cache is what makes repeat queries usable.

---

## 6. Profile architecture

oMLX has two distinct merge paths (`omlx/model_settings.py`):

- `_settings_with_profile_locked` → **universal** fields only, merged per request
- `_runtime_settings_with_profile_locked` → universal **+ model-specific**, and
  model-specific fields are part of the engine load-time variant signature
  (`omlx/engine_pool.py::_variant_signature`)

**Therefore the design rule:** one shared engine variant on the base model,
profiles carry universal fields only. All four profiles report
`has_engine_fields: false`, and a smoke run that exercises all four recorded
**1 engine load total** — confirmed zero-cost switching.

### Shared engine variant (on the base model)

```
qwen35_ane_prefill_enabled : false   (tuner: GPU-only is faster)
turboquant_kv_enabled      : false   (unnecessary; 262k fits on lossless KV)
mtp_enabled                : false   (checkpoint lacks mtp.* weights)
dflash_enabled             : false   (MTP drafter is the better fit)
specprefill_enabled        : false
vlm_mtp_enabled            : true
vlm_mtp_draft_model        : Qwen3.8-27B-MTP-8bit
vlm_mtp_draft_block_size   : 3
max_context_window         : 262144
```

### The four profiles

Sampling starts from Qwen's published guidance and the checkpoint's own
`generation_config` (top_k 20, top_p 0.95): thinking mode 0.6/0.95/20,
non-thinking 0.7/0.8/20. Qwen warns against greedy decoding in thinking mode, so
no profile uses temperature 0.

| served model id | temp | top_p | thinking | ctx | max_tok | for |
|---|---|---|---|---|---|---|
| `Qwen3.8-27B-8bit:qwen-code` | 0.7 | 0.8 | off | 131k | 32k | writing/editing code |
| `Qwen3.8-27B-8bit:qwen-agent` | 0.3 | 0.8 | off | 131k | 8k | tool use, low latency |
| `Qwen3.8-27B-8bit:qwen-reason` | 0.6 | 0.95 | **on**, 8k budget | 131k | 32k | hard reasoning |
| `Qwen3.8-27B-8bit:qwen-long` | 0.3 | 0.9 | off | **262k** | 32k | repo/document analysis |

`qwen-agent` also sets `max_tool_result_tokens: 16384` so one oversized tool
output cannot evict the working context.

Two of these numbers were revised after the profiles met real client use:

- **`qwen-reason` context 65k → 131k.** Agent clients subtract the whole output
  cap from the context window before deciding whether to compact, so a 65,536
  window against `max_tokens: 32768` left only ~33.5k usable and triggered a
  summarisation every few tool calls. 131k restores a working window without
  touching the shared engine variant, since `max_context_window` is universal.
- **`qwen-long` max_tokens 8k → 32k.** The original 8k assumed the output of a
  long-context run is always a summary or a citation list. It is not: the same
  profile is used to write a full document against a whole-repo prompt, and 8k
  truncated it. Prefill still dominates the cost at this prompt size, so a
  larger generation cap changes the total very little.

### Smoke results (`bench/smoke.py`)

| profile | result | evidence |
|---|---|---|
| code | PASS | emitted the requested function, no thinking block leaked |
| agent | PASS | `finish=tool_calls`, `{"city": "Oslo"}`, 1.3 s |
| reason | PASS | 466 reasoning tokens, correct answer (360 km) |
| longctx | PASS | 46,036-token needle-in-haystack retrieved (`84391`) |

---

## 7. Client wiring (opencode)

The profiles are consumed through opencode, whose config is owned by
`~/Development/private/dotfiles/opencode/opencode.json` (symlinked to
`~/.config/opencode/opencode.json`). That repo documents the wiring; the
measurements behind it are here.

- **Profile IDs survive opencode's parser.** opencode splits `provider/model`
  with `split("/", 2)`, so the colon in `Qwen3.8-27B-8bit:qwen-code` is safe. It
  reads only the config `models` map and does no `/v1/models` discovery, so every
  profile has to be listed explicitly.
- **Switching stays free through the client.** After exercising two profiles via
  opencode, the `VLMBatchedEngine loaded` count in `~/.omlx/logs/server.log` was
  unchanged, confirming the shared-variant design holds end to end and not just
  through the admin API.
- **Agents are pinned per profile** (`build`→code, `plan`→reason,
  `explore`/`general`→agent, `title`/`summary`/`compaction`→agent), so the right
  sampling is used without manual switching. Verified from opencode's own
  header: `plan · Qwen3.8-27B-8bit:qwen-reason`.
- **Client timeout had to be raised to 45 minutes.** Prefill at the 262k ceiling
  runs ~137 tok/s, so time-to-first-token on a full long-context prompt is about
  32 minutes. The previous 15-minute `headerTimeout` would have killed it
  mid-prefill, making `qwen-long` unusable at the context it was built for.
- **Vision is real and was switched on.** The checkpoint has
  `language_model_only: false` and 333 `vision_tower.*` tensors, so
  `attachment: true` and the `image` input modality are honest.

### `thinking_budget_enabled` costs the entire MTP win

oMLX's `vlm_mtp_processor_conflicts` docstring states that
`thinking_budget_enabled` is "intentionally absent" from the conflict list,
because the MTP path applies `ThinkingBudgetProcessor` at verify time via
`MTPProcessingSampler`, so "a thinking-budget default no longer forces the
BatchGenerator fallback."

Correct for correctness, wrong for throughput. Measured on the `reason` profile,
400 generated tokens per run, same prompt:

| reason profile setting | decode |
| --- | --- |
| as shipped (`thinking_budget_enabled: True`) | 17.6 t/s |
| `thinking_budget_enabled: False` | **31.9 t/s** |
| `preserve_thinking: False` | 17.7 t/s (no effect) |
| both off | 32.4 t/s |

For comparison, on the same prompt `qwen-code` and `qwen-agent` return 41.8 and
42.8 t/s. So `reason` was running at the plain-decode baseline while every other
profile got the 1.8x, and `thinking_budget_enabled` is the single cause.

The budget exists so a hard prompt cannot spend unbounded tokens thinking;
dropping it doubles throughput but the only remaining guard is
`max_tokens: 32768`, which a runaway thinking episode could consume entirely and
never reach an answer.

The throughput number alone could not decide it, so `bench/thinking_budget_ab.py`
measured the risk the budget covers: four hard prompts, both arms, two repeats,
recording reasoning tokens and whether each run terminated on `stop` rather than
`length`. The decision rule was fixed in advance: the budget comes off only if no
unbudgeted run exceeds 8,192 thinking tokens and none finishes on `length`.

**Result (`results/thinking_budget_ab.json`): the budget stays on.**

| arm | decode (median) | thinking tokens (median) | worst case | truncated | wrong |
| --- | --- | --- | --- | --- | --- |
| budget on | 17.79 t/s | 8,208 | 8,400 | 0 | 0 |
| budget off | 30.38 t/s | 8,641 | **30,149** | **1** | 0 |

Four of eight unbudgeted runs exceeded 8,192 thinking tokens. The failure the
budget exists to prevent happened on the first pass: `algorithm_design` repeat 1
generated all 32,768 tokens, finished on `length`, and never closed its thinking
block, so the response carried no answer at all. Its sibling repeat consumed
30,149 thinking tokens and closed only just inside the cap.

So the cost is real and known (1.7x decode) and it buys protection against a
roughly 1-in-8 total loss on hard prompts. The budget-on arm never came close to
trouble: worst case 8,400 thinking tokens against a 32,768 hard cap. Note that
the on-arm slightly overshoots its own 8,192 budget, because the MTP path applies
the limit at verify time and a whole draft block can land past the boundary.

Sizing note: 8,192 is well chosen rather than lucky. The median hard prompt wants
~8,200 to 8,600 thinking tokens either way, so the budget is not truncating
typical work; it clips only the tail.

### A client-sent temperature was silently overriding every profile

This was a real defect in the setup, found while wiring up Spec Kit, and it made
the sampling column in section 6 fiction for any request coming through opencode.

`_effective_sampling_params` in `server.py` resolves sampling as
**request > model settings > global**:

```python
if req_temperature is not None:
    temperature = req_temperature
elif model_settings and model_settings.temperature is not None:
    temperature = model_settings.temperature
```

opencode always sends a temperature — its documented default is 0 for most
models and 0.55 for Qwen models — so `qwen-code` was not running at 0.7, nor
`qwen-agent` at 0.3. Whatever the client picked won.

The fix is `force_sampling: True` on all four profiles, which takes the other
branch and pins temperature/top_p/top_k to the profile, ignoring the request. It
is a universal field, so it is free, and it is absent from
`vlm_mtp_processor_conflicts`, so MTP is unaffected.

Verified by sending `temperature: 0, top_k: 1, top_p: 1.0` (a greedy request)
three times to `qwen-code` and getting three different sentences. Under the old
behaviour all three would have been identical.

This is the intended contract: the profile *is* the sampling policy, and callers
choose behaviour by selecting a profile rather than by sending knobs. A side
effect worth knowing is that opencode's per-agent `temperature`/`top_p` are now
inert against this provider.

`force_sampling` is deliberately **not** set on the base model, because
`bench/accuracy.py` targets the base model and its `--sampling deterministic`
mode has to keep working.

### `reasoning_parser` is unnecessary on this checkpoint

Worth recording because the opposite was assumed. The concern was that with
thinking enabled, `<think>` blocks would land inline in `content`. Measured
instead:

| mode | `reasoning_content` | `content` | `<think>` in `content` |
| --- | --- | --- | --- |
| non-streaming | 216 chars | 87 chars, clean | no |
| streaming | 191 chars in deltas | 5 chars | no |

The Qwen3.5 chat template exposes `reasoning_content` natively, so oMLX
separates thinking without a parser configured. `reasoning_parser` was therefore
left unset.

Two related facts that were checked while investigating, and that matter if this
is ever revisited: `reasoning_parser` **is** a universal field, so it could be
set per profile at zero cost; and it is **not** an MTP conflict, because the
`xgr.get_builtin_structural_tag` path in `server.py` is gated on
`compiled_grammar is not None`, which only happens when a request asks for
structured output — and such a request already falls back off the MTP path
regardless.

---

## 8. What the validation runs settled

Two runs, both hands-off: `bench/overnight.sh` (~33 h, accuracy and the shipped
variant) and `bench/unattended.sh` (phases A to E, cache and scheduler levers,
concurrency, long-context quality, quantisation). All suites at 500 samples,
batch 8, `--sampling deterministic` unless stated.

The shipped configuration came out of both runs unchanged: `mtp` variant,
`hot_cache_max_size: "0"`, `chunked_prefill: false`, all four profiles verified,
all four smoke checks PASS, zero engine loads during the final check.

### Accuracy floor of the shipped variant

| group | suites |
| --- | --- |
| code | humaneval 93.9, mbpp 80.4, livecodebench 59.8 |
| reason | mmlu_pro 64.0, gsm8k 93.6, mathqa 43.8 |
| general | mmlu 80.0, arc 95.0, hellaswag 93.6, winogrande 80.2, truthfulqa 85.6 |
| safety | safetybench 86.2, bbq 94.8 |

### MTP is output-neutral: the last unvalidated assumption, now closed

Suite scores were never going to settle this. At 500 samples the standard error
on one score is about 2.2 points, so the 95% interval is ±4.4 and the interval on
a *difference* of two scores is about ±6.2 — wider than any regression worth
caring about. But both arms answer the *same* questions, so the arms are paired and the
only informative quantity is the set of questions whose verdict flipped.
`bench/paired_compare.py` does that (McNemar, exact two-sided).

| suite | MTP on | off | questions flipped | net | p |
| --- | --- | --- | --- | --- | --- |
| humaneval | 93.9 | 93.9 | **0** | 0 | 1.0 |
| mbpp | 80.4 | 80.4 | **0** | 0 | 1.0 |
| mmlu_pro | 64.0 | 64.0 | **0** | 0 | 1.0 |
| mathqa | 43.8 | 43.8 | **0** | 0 | 1.0 |
| gsm8k | 93.6 | 92.8 | 8 | +4 to MTP | 0.29 |
| livecodebench | 59.8 | 59.4 | 28 | +2 to MTP | 0.85 |

On four of six suites, 1,664 questions produced **identical verdicts, question for
question**. The two suites that diverged are the two with the longest generations,
both net in MTP's favour, both far from significance. That pattern is accumulated
numerical nondeterminism at batch 8, where batch composition changes reduction
order, and not a lossy decoder: a lossy drafter would have shifted the
short-answer suites too.

**MTP is safe. Shipped as-is.**

### `qwen-code` temperature 0.7 vs 0.3: no measurable difference, keep 0.7

The suite scores looked like a clean win for 0.3, moving the same direction on all
three suites: humaneval 93.9 vs 92.1, mbpp 79.6 vs 78.4, livecodebench 59.2 vs
58.0. The paired test says otherwise.

| suite | 0.7 | 0.3 | 0.7 only | 0.3 only | flipped | p |
| --- | --- | --- | --- | --- | --- | --- |
| humaneval | 92.07 | 93.9 | 2 | 5 | 7 | 0.45 |
| mbpp | 78.4 | 79.6 | 19 | 25 | 44 | 0.45 |
| livecodebench | 58.0 | 59.2 | 51 | 57 | **108** | 0.63 |
| pooled | | | 72 | 87 | 159 | **0.27** |

The important column is the flip count. Temperature churns 108 of 500
livecodebench verdicts, roughly 22% of the suite, and the split is nearly even
(51 against 57). The +1.2 point suite gap is the small net residue of a large
amount of noise, not a signal. Pooled p = 0.27.

**`qwen-code` stays at 0.7**, the Qwen-published default, because nothing was
measured that justifies moving it.

Caveat worth stating: both arms sampled at nonzero temperature, so these flips mix
the temperature effect with plain run-to-run variance and the two cannot be
separated from this data. A repeat of one suite at an unchanged temperature would
size the pure variance floor. It would not change the decision, since the observed
difference is already inside chance.

The wider lesson applies to any future sampling tweak here: a 500-sample suite
score cannot resolve anything smaller than about 2 points, so judge sampling
changes on paired per-question data or not at all.

### Prefix cache, hot cache, chunked prefill and spec-prefill: all dead ends

**In-memory hot cache (`results/cache_ab.json`) does not help and costs 20 GB.**

| arm | cold | repeat (median) | cache hit | after churn |
| --- | --- | --- | --- | --- |
| `hot_cache_max_size: "0"` | 12.58 s | 3.49 s | 0.884 | 3.68 s |
| `hot_cache_max_size: "20GB"` | 14.39 s | 3.96 s | 0.886 | 3.93 s |

Every number is slightly worse with the RAM tier on, and the cache hit fraction
does not move at all. The mechanism is clear once measured: repeats already reuse
88% of their blocks from the SSD tier, and the residual 3.5 s is prefill of the
uncached tail plus generation, not disk reads. There is no disk-read cost left for
a RAM tier to remove. §8's earlier claim that this was "the largest remaining win
for agentic loops" was a plausible hypothesis and it was wrong.

**`chunked_prefill` has no effect**, so it stays off: prefill 686.4 to 701.3 t/s
at 16k, 538.7 to 539.5 at 65k, 386.9 to 389.2 at 131k, TTFT unchanged.

**`specprefill_enabled` has no effect either**, and is marginally negative:
prefill 673.6 vs 686.4 at 16k, 530.4 vs 538.7 at 65k. It did not engage, which is
what was predicted for a dense checkpoint. Stays off.

### Concurrency is prefill-bound, and parallel agents are a bad trade

`results/concurrency.json`, same profile, 8 clients maximum because
`scheduler.max_concurrent_requests` is 8.

| concurrent | aggregate t/s | median latency | p95 latency | scaling efficiency |
| --- | --- | --- | --- | --- |
| 1 | 20.66 | 6.4 s | 6.4 s | 1.00 |
| 2 | 18.15 | 8.2 s | 8.2 s | 0.44 |
| 4 | 24.89 | 13.6 s | 16.2 s | 0.30 |
| 8 | 28.42 | 23.7 s | 31.2 s | 0.17 |

Eight concurrent requests buy 1.38x aggregate throughput and cost 4.9x p95
latency. Two are worse than one.

This looks like it contradicts §4, where the engine reached 5.1x aggregate decode
at batch 8. It does not, and the gap is the finding: **these requests were
prefill-bound, not decode-bound.** Each of the eight clients sent a distinct
~1,035-token prompt with `cached_tokens: 0`, so about 8,300 tokens of prefill at
~500 t/s consumed roughly half the 31 s wall, while the aggregate t/s figure counts
only generated tokens. The engine's decode batching is fine; feeding it eight cold
prompts is what does not scale.

So the lever for parallel agent work is prefix-cache hit rate, not the drafter and
not raising the concurrency limit.

Mixed profiles, one request per profile at once:

- **Zero engine loads.** The shared-variant design of §6 holds under real
  concurrency, not just sequential switching.
- Speedup over running them serially: 1.38x (16.8 s against 23.3 s).
- Per-request slowdown: `reason` 1.68x, `code` 2.87x, `agent` 2.99x,
  `longctx` 3.56x.

### Long-context quality is intact at the full 262k

`results/longctx_quality.json`: needles at five depths (0.05 to 0.95) plus a
multi-hop question at each length.

| target | verified prompt tokens | needles | multi-hop | cache reuse on repeats |
| --- | --- | --- | --- | --- |
| 65,536 | 65,224 | 5/5 | pass | 97% |
| 131,072 | 130,760 | 5/5 | pass | 99% |
| 262,144 | 261,826 | 5/5 | pass | 99% |

15/15 retrieval and 3/3 multi-hop, with no depth penalty anywhere: nothing was
lost in the middle. §5 established that the context *loads* to 262k; this
establishes that the model still *uses* what it loaded, at the full advertised
window rather than half of it.

**This run replaces an earlier one that never reached its targets**, kept as
`results/longctx_quality_uncalibrated.json`. That run assumed a flat 3.1
characters per token and landed at 35,396 / 68,996 / **136,196** tokens, so the
top row tested 136k while reporting 262k, and this section previously claimed
quality above 136k was unmeasured.

The assumption was wrong in shape, not merely in value. Fitting the three lengths
that run recorded gives, exactly to the token:

    tokens = 0.1653855 * chars + 1796

There is a fixed ~1,800-token overhead — 240 section headers, the instruction
text and the chat template — on top of a constant per-character rate, so no single
ratio can hold across lengths: the same build measures 5.74 characters per token
at 65k and 5.97 at 262k purely because that constant amortises. Picking a better
constant would have failed the same way somewhere else.

`bench/longctx_quality.py` therefore no longer assumes a ratio. It sends two small
probes of the real haystack construction, reads `prompt_tokens` back and fits the
line at run time, which costs ~75 s once per run and lands within 8 tokens of a
261,824-token target. That also makes the harness survive a tokenizer change,
which matters because backlog A1 puts a different model behind it. Two related
defects were fixed alongside: haystack length quantised in ~8,256-token steps
(the remainder is now padded to the character), and the console printed engine
knobs while filtering out the `*_enabled: False` switches that made them inert,
so a disabled accelerator read as an enabled one.

Cold prefill, measured on this run, which is the first real 262k timing here:

| prompt tokens | cold prefill | rate |
| --- | --- | --- |
| 65,214 | 118.4 s | 533 t/s |
| 130,750 | 351.7 s | 372 t/s |
| 261,816 | 1,118.9 s (18.7 min) | 234 t/s |

Doubling the prompt costs roughly 0.65–0.70x the prefill rate. Repeat questions at
262k were served in 15–19 s against that 18.7 minutes, at 99.3% cache reuse
(260,096 of 261,816 tokens), which is the strongest evidence yet for §10's
prefix-cache item.

### Sizing the run

The suites were originally going to run at full sample size. Measuring the
bundled datasets first showed that was never an overnight job:

| group | suites | questions at full sample |
| --- | --- | --- |
| code | humaneval 164, mbpp 500, livecodebench 1055 | 1,719 |
| reason | mmlu_pro 12,032, gsm8k 1,319, mathqa 2,985 | 16,336 |
| general | mmlu 14,042, arc 1,172, hellaswag 10,042, winogrande 1,267, truthfulqa 817 | 27,340 |

Across the passes above that is ~101k questions, or 36–44 hours, with the
thinking-enabled reasoning pass alone accounting for 15–20 h. The script now
defaults to `SAMPLES=500`, which gives a standard error near 2.2% — so ±4.4% at
95% on one score, and about ±6.2% on a difference of two — where the full run
buys roughly ±0.9% standard error for 30x the time.

**That estimate said 5–7 hours and the run took ~33.** The error is worth
recording because it will recur otherwise: the estimate scaled from question
counts and assumed suites cost roughly the same per question. They do not.
`livecodebench` averages ~50 s per question, against ~4 s for `mmlu_pro`, because
each question generates a whole program. At 500 samples it alone is ~6.2 h, and
the code group ran four times (shipped, MTP-off, temp 0.7, temp 0.3), so
`livecodebench` accounted for ~25 h of the 33. **Size accuracy runs by measured
seconds per question per suite, never by question count.** The per-suite `time_s`
in every `results/acc_*.json` now makes that possible.

Step 5 adds 1–2 hours on top. It is small in question count (4 prompts × 2 arms
× 2 repeats) but each run deliberately produces a long thinking trace, and the
budget-on arm generates those at the plain-decode 17.6 t/s. The repeats are
there because thinking length is noisy — a trial pair on the same prompt came
back at 411 and 192 tokens — and the decision rule turns on the maximum. It came
in on estimate.

TurboQuant 4-bit KV was dropped from the run entirely: `bench/context.py`
reached the full 262,144 ceiling without it and stopped at `capped_by:
"target"` rather than memory, so it has no problem left to solve here.

### What failed, and the harness bugs the runs exposed

- **Phase B1 lost both batch sweeps** on a bad argument: the script passed
  `--batch 1 2 4 8` and oMLX's `BenchmarkRequest` accepts only 2, 4 and 8. The
  single-stream row the harness always emits alongside is the batch-1 point, so
  there was never a reason to ask for batch 1. Fixed and re-run; the numbers are
  in §4.
- **`bench/throughput.py` was silently discarding every batch measurement.**
  Batch rows report `pp_tps`, `tg_tps` and `avg_ttft_ms`, while single rows report
  `processing_tps`, `gen_tps` and `ttft_ms`. The summariser read only the
  single-row names, so batch rows came out with `decode_tps: null` even though the
  values were sitting in the stored `raw` payload. Fixed, and existing result
  files were re-summarised from their own raw.
- **`bench/report.py` crashed** formatting those nulls, and picked whichever row
  matched a prompt length first, which could be a batch row. It now selects by
  prompt length *and* batch size, and renders missing values as `-`.
- **`bench/accuracy.py` was writing accumulated history into result files.** Its
  freshness filter identified new records by `(benchmark, model_id)`, which repeats
  on any re-run, so a repeat matched an older record, looked stale, and the script
  fell back to saving the entire server-side history. Records carry no id or
  timestamp, so the fix is positional: remember the history length before the run
  and take everything appended after it.
- **Phase E (4-bit quantisation) never ran.** Both checkpoints downloaded fine,
  then configuration died on `KeyError: 'settings'`: a model that has never been
  loaded is listed without a settings block at all, so there were no defaults to
  merge the variant patch onto. Fixed by loading once first, and `Omlx.settings()`
  now returns `{}` instead of raising. The comparison itself is still unrun.
- Of 87 attempted omlx.ai uploads, 9 succeeded and 78 failed with an internal
  server error, so most of the per-question data never left the machine. Not a
  problem here since uploads were approved, but do not rely on the upload
  succeeding as a record of a run.

---

## 9. Cache and scheduler levers are API-reachable

An earlier revision of §8 recorded the hot cache as blocked behind a hand-edited
`settings.json` and a server restart. It is not, in this oMLX build:

- `hot_cache_max_size` and `chunked_prefill` are both fields on the admin
  `GlobalSettingsRequest` (`omlx/admin/routes.py`, cache and scheduler blocks).
- `POST /admin/api/global-settings` persists them and then applies them at
  runtime. For cache fields it calls `_apply_cache_settings_runtime`, which
  updates the engine pool's scheduler config and unloads loaded models so the
  next load picks the new config up.
- Only `initial_cache_blocks`, host/port and the MCP config path are documented
  in that handler as needing a restart. `hot_cache_max_size` is not among them.

So the cost of testing the hot cache is a model reload, not a server restart or
a hand-edited config file. `Omlx.update_global_settings()` wraps it.

One asymmetry to know when verifying a change: the POST takes flat field names
while the GET returns them nested under sections, so `chunked_prefill` goes in
flat and comes back at `scheduler.chunked_prefill`, and `hot_cache_max_size`
comes back under `cache`. Reading back the name you wrote returns nothing and
looks like the write was ignored. `Omlx.global_setting(name)` searches both
levels.

Two related capabilities were found while confirming this, both of which the
earlier sessions worked around unnecessarily:

- **`POST /admin/api/cache/probe`** classifies a prompt's blocks as hot-SSD,
  disk-SSD or cold. That is a direct instrument for prefix-cache behaviour,
  where before there was only the `cached_tokens` field on a completed request.
  `bench/cache_ab.py` probes before every request.
- **Restarts are automatable if ever needed.** The server runs under the menubar
  supervisor, so `POST /admin/api/server/restart` returns 202 and the supervisor
  respawns it with about 7 s of downtime. `omlx restart` exists on PATH too. The
  endpoint is gated on `OMLX_SUPERVISED`, so it correctly refuses under a plain
  `omlx serve`.

---

## 10. Still open

Moved to **[backlog.md](backlog.md)**, which covers the whole local stack rather
than this model alone. **As of 2026-09-12 all six of the items that lived here
are closed**, and the answers are below rather than pending:

| was open here | answer | where |
|---|---|---|
| prefix-cache hit rate under a real agent loop | **86.7%** on real opencode traffic | §20 |
| the prefix audit it depended on | skills static; date and cwd roll daily, in the tail | backlog B1b |
| long-context quality above 136k | 15/15 needles and 3/3 multi-hop at 261,826 | §8 |
| 4-bit quantisation comparison | 8-bit wins both axes; build method is irrelevant | §19 |
| routing cheap calls to a small model | ~20 s per swap with a 64.7 GB partner, ~2.6 s with a 16 GB one | §18 |
| variance floor for sampling changes | **zero** — 0 of 500 flips, bit-exact at temperature 0 | §14 |

DFlash remains parked: it needs a third-party block-diffusion drafter and
promises an MTP-like single-stream win this setup already has.

backlog.md also carries the sequencing, in waves. Two items from this section were
re-ordered on dependency grounds: B2 moved early because its
characters-per-token bug is in the harness that would judge any new model's
long-context claim, and B1 was split because the cheap half can invalidate the
expensive half.

Explicitly closed, so they do not come back: ANE prefill (§3), TurboQuant KV (§5),
in-memory hot cache, `chunked_prefill`, `specprefill` (§8), raising the concurrency
limit (§8), and `qwen-code` temperature (§8).

---

## 11. Privacy note

`omlx/admin/accuracy_upload.py` uploads **every** accuracy suite result to
`https://omlx.ai/api/benchmarks/intelligence`. The payload includes a machine
`owner_hash` (derived from the IO platform UUID), hardware profile, your model
settings, and gzipped per-question results. **There is no opt-out setting.** The
only gate is `_MIN_UPLOAD_QUESTIONS = 100`: suites run with fewer than 100
samples skip the upload.

Uploads were explicitly approved for this work. To stay fully local, run
`bench/accuracy.py --samples 99` or `SAMPLES=99 bash bench/overnight.sh`.

---

## 12. Qwen3-Coder-Next re-baseline (backlog A1)

`mlx-community/Qwen3-Coder-Next-6bit`, 64.7 GB on the Hub and 60 GB on disk. 80B
total parameters, ~3B active, 512 experts with 10 routed, hybrid Gated DeltaNet
plus Gated Attention, `model_type: qwen3_next`, supported by the mlx_lm 0.31.3
that oMLX bundles. Build classification per §A2a: 6-bit RTN with the converter's
formulaic 8-bit bump on the 96 MoE router gates, and the mlx-community,
lmstudio-community and NexVeridian builds are byte-identical.

### It wins on speed without any speculative decoding

`results/tp_codernext_6bit.json` against `results/tp_baseline.json` (27B, no
accelerators) and `results/tp_mtp_full.json` (27B as shipped, MTP on).

| prompt | 27B plain | 27B shipped | Coder-Next | vs plain | vs shipped | CN prefill t/s |
| --- | --- | --- | --- | --- | --- | --- |
| 1,024 | 18.2 | 36.4 | **78.5** | 4.3x | 2.2x | 1,619 |
| 4,096 | 18.0 | 30.2 | **76.0** | 4.2x | 2.5x | 2,520 |
| 16,384 | 17.3 | 33.7 | **71.4** | 4.1x | 2.1x | 2,475 |
| 65,536 | 15.7 | 25.8 | **57.7** | 3.7x | 2.2x | 1,598 |
| 131,072 | - | 22.6 | **45.8** | - | 2.0x | 999 |
| 200,000 | - | - | **36.0** | - | - | 736 |

Decode is 2.0-2.5x the *fully accelerated* shipped setup and about 4x the
unaccelerated 27B, which is the fair architectural comparison. The mechanism is
the one §A of the backlog predicted: decode is memory-bandwidth-bound, and this
model reads ~3B active parameters per token instead of 27B.

Note the last row. Coder-Next at a 200,000-token prompt still decodes faster
(36.0 t/s) than the shipped 27B does at 16k (33.7 t/s).

**Prefill is the larger win and the one that matters most here.** Against the 27B's
686 t/s at 16k, 539 at 65k and 387 at 131k, Coder-Next runs 2,475 / 1,598 / 999 —
3.6x, 3.0x and 2.6x. §8 established that this workload is prefill-bound under
concurrency and that agentic loops pay cold prefill repeatedly, so this is worth
more than the decode number.

### There is no MTP path for this model, and that is now settled

The 1.97x decode win in §4 came from a separate `Qwen3.8-27B-MTP-8bit` drafter,
not from anything in the base checkpoint, so it was never going to transfer for
free. Three checks, all negative:

- the 6-bit build's `config.json` has no `nextn`/`mtp` keys;
- its `model.safetensors.index.json` holds 1,891 tensors and **zero** matching
  `mtp` or `nextn`;
- the upstream `Qwen/Qwen3-Coder-Next` config has no MTP block either, and a Hub
  search returns no MTP drafter for it.

So every number above is unaccelerated. Any future drafter is upside, not a
prerequisite. `specprefill` was predicted in §8 to need a sparse checkpoint and is
now worth its one re-test here; it has not been run yet.

### Long-context quality matches the 27B and costs less

`results/longctx_codernext.json`, same harness and seeds as §8's rerun.

| target | verified prompt tokens | needles | multi-hop | cold prefill | vs 27B |
| --- | --- | --- | --- | --- | --- |
| 65,536 | 65,221 | 5/5 | pass | 38.7 s | 3.1x faster |
| 131,072 | 130,755 | 5/5 | pass | 126.1 s | 2.8x faster |
| 262,144 | 261,813 | 5/5 | pass | 468.4 s | 2.4x faster |

15/15 and 3/3, no depth penalty, at the full window — identical quality to the 27B
at 2.4-3.1x the speed. Repeat questions at 262k came back in 7.5-16.1 s against
the 27B's 15.5-19.2 s, at the same 99% cache reuse.

This also validated B2's redesign: the harness re-fitted itself to a different
tokenizer with no code change, landing on 261,813 tokens against a 261,824 target.
The fitted intercept moved from 1,286 to 1,036, which is exactly the term a
hardcoded characters-per-token constant would have got wrong.

### Memory, and what it means for 6-bit versus 8-bit

Peak resident, from the throughput runs:

| prompt | peak GB |
| --- | --- |
| 1,024 | 61.6 |
| 131,072 | 73.3 |
| 200,000 | 79.2 |

That is ~88 KB per token of KV growth, linear, extrapolating to **~85 GB at the
full 262k**. Far cheaper per token than the 27B, whose Gated DeltaNet layers this
model has more of: during §8's 262k run the 27B server was sampled at 72 GB
against a 29.5 GB checkpoint, so ~40 GB of KV where this needs ~23 GB.

The consequence for the open 6-bit/8-bit question: 8-bit is 84.7 GB against 6-bit's
64.7 GB, so an 8-bit build at full context lands near **105 GB of 128**. Feasible,
but it leaves ~23 GB of headroom and forecloses keeping any second model resident —
which is what backlog A3 and E4 both need. 6-bit at ~85 GB leaves ~43 GB.

### Behaviour

Tool calls work in the standard OpenAI shape: `finish_reason: tool_calls`, correct
function name, and arguments that parse as JSON including optional parameters.
This was the flagged risk — Coder-Next has its own native function-call format —
and oMLX normalises it. Verifying the same through a LiteLLM hop remains open
(backlog C4). Code generation and exact instruction-following both behave.

### What is not measured yet

**Quality on hard tasks.** Everything above is speed, memory, retrieval and
format. No accuracy suite has been run against this checkpoint, so the claim
"better at this workload" is established on every axis except the one the whole
exercise is about. That is the remaining gate, and §8's sizing applies: judge it
with `bench/paired_compare.py` on per-question data, not on 500-sample suite
scores that cannot resolve under ~2 points.

---

## 13. Quality: Coder-Next loses, and the quantisation run says something else

§12 measured Coder-Next on speed, memory, retrieval and tool format and it won
every one. This is the axis that was missing, and it reverses the conclusion.

### The accuracy pass

500 samples per suite, batch 8, thinking off, deterministic sampling — identical
settings to `acc_ship_*`, because a different sample count or batch size would
have made the pair uncomparable. p-values are exact two-sided McNemar on the
paired per-question verdicts (`bench/paired_compare.py`), which is the only
instrument with the power to read differences this size.

| suite | 27B 8-bit | Coder-Next 6-bit | delta | flips A/B | p |
| --- | --- | --- | --- | --- | --- |
| humaneval | 93.9 | 90.2 | -3.7 | 9 / 3 | 0.146 |
| mbpp | 80.4 | 80.2 | -0.2 | 34 / 33 | 1.0 |
| **livecodebench** | **59.8** | **43.2** | **-16.6** | **95 / 12** | **0.0** |
| mmlu_pro | 64.0 | 58.0 | -6.0 | 74 / 44 | 0.0073 |
| gsm8k | 93.6 | 93.8 | +0.2 | 18 / 19 | 1.0 |
| mathqa | 43.8 | 46.8 | +3.0 | 62 / 77 | 0.235 |
| mmlu | 80.0 | 83.2 | +3.2 | 27 / 43 | 0.072 |
| arc_challenge | 95.0 | 93.6 | -1.4 | 18 / 11 | 0.265 |
| hellaswag | 93.6 | 89.2 | -4.4 | 37 / 15 | 0.0032 |
| winogrande | 80.2 | 76.6 | -3.6 | 50 / 32 | 0.060 |
| truthfulqa | 85.6 | 87.0 | +1.4 | 23 / 30 | 0.410 |
| safetybench | 86.2 | 80.8 | -5.4 | 44 / 17 | 0.00073 |
| bbq | 94.8 | 94.2 | -0.6 | 22 / 19 | 0.755 |

Pooled: code p = 0.00000 (27B better), reason p = 0.448 (no difference), general
p = 0.174 (no difference), safety p = 0.0039 (27B better).

**livecodebench carries the whole result**, and it is not subtle: 95 questions the
27B answered and Coder-Next did not, against 12 the other way. humaneval and mbpp
show nothing. The split is informative in itself — those two are function
completion, livecodebench is competitive programming against stdin/stdout.

**§14 explains where this gap lives without excusing it.** Most of it is failure
to produce a runnable, contract-conforming program rather than wrong algorithms —
but that failure survives being told the contract explicitly, so the -16.6 stands
as a real deficit. Read §14 before acting on this table.

### Three attempts to kill the livecodebench result

An 8:1 flip asymmetry is the shape of a systematic failure, not a gradual quality
gap, so it was worth trying to explain away. None of these did:

- **Storage truncation.** `question_results.predicted` is cut to 200 characters,
  but uniformly: 470 of 500 for the 27B, 475 of 500 for Coder-Next. Grading
  happened server-side on the full text.
- **Output format.** Failures do not cluster in one answer shape. Coder-Next fails
  more in every bucket — bare function 85% vs 53%, import-first 61% vs 42%,
  top-level 27% vs 14%.
- **Generation truncation.** Re-running a failed question directly gave a complete
  answer, `finish_reason: stop`, 150 tokens, nowhere near a cap.

**What did not resolve at the time:** that same re-run produced a *correct*
solution, hand-traced against the worked example, for a question the suite marked
wrong. That single thread, pulled in §14, unwound most of this section's
headline.

### The quantisation run, which lands the other way

`bench/quant_compare.py` ran for the first time (FINDINGS §10.3 had it fixed but
unrun). 27B 4-bit RTN against the shipped 8-bit, same suites and settings:

| | code pooled | reason pooled |
| --- | --- | --- |
| verdict | no difference, p = 0.130 | **8-bit better, p = 0.0028** |

Per suite, 4-bit costs -1.8 humaneval, -0.6 mbpp, **-2.4 livecodebench (p = 0.224,
not significant)**, and -4.2 mmlu_pro (p = 0.0055), -1.8 gsm8k (p = 0.049).

So **dropping the 27B to 4-bit costs reasoning but not coding**, and it is 1.4x
faster at 15.5 GB against 29.5.

### What the three arms say together

| | decode @16k | livecodebench | mmlu_pro | peak GB @65k |
| --- | --- | --- | --- | --- |
| 27B 8-bit | 33.7 | **59.8** | **64.0** | 44.5 |
| 27B 4-bit | 49.1 | 57.4 | 59.8 | 29.9 |
| Coder-Next 6-bit | **71.4** | 43.2 | 58.0 | 67.7 |

Coder-Next is twice the speed at materially lower end-to-end coding accuracy; §14
locates the deficit precisely rather than reducing it. The working notes called
dense-vs-MoE "the biggest single gap in the current setup" and it is, for
throughput. On quality it is mildly negative on code and clearly negative on
multi-domain reasoning, where mmlu_pro's -6.0 (p=0.007) survives §14 untouched
because that suite has no I/O contract to fail.

The option the data actually supports was not on the original list: **the 27B at
4-bit as a coding model**, 1.4x faster and 15 GB smaller with no measurable code
cost, with 8-bit retained for reasoning. Both fit resident together in 45 GB.

This also promotes best-of-N (backlog D2) from a curiosity to the central
question. Coder-Next's speed is only worth something if it can be converted back
into quality against a verifier, and that is now the cheapest untested route to
the quality ceiling.

---

## 14. What the variability check found, and why §13 needed correcting

Two questions were outstanding: how much of the measured difference is run-to-run
noise, and whether livecodebench measures what its score implies.

### The harness is exactly reproducible, so none of it is noise

`bench/accuracy.py reason --suites mathqa --samples 500` re-run against the same
model, same settings, compared to the original arm:

| | |
| --- | --- |
| score | 43.8% both runs |
| discordant questions | **0 of 500** |

Zero flips. Not "within chance" — identical, question for question. The engine is
bit-exact too: six identical requests at temperature 0 produced six identical
SHA-256 hashes.

Three consequences. Every cross-model flip in §13 is genuine model difference, so
the McNemar p-values stand as computed. The variance floor that backlog B3 was
parked against does not exist at these settings, so B3 can be closed rather than
merely parked. And **re-running a suite to confirm a result is pointless here** —
it would return the same numbers by construction. The planned 3.5-hour
livecodebench re-run was cancelled on this basis.

### livecodebench mostly measures I/O-contract compliance

The suite does not grade reasoning about code. `omlx/eval/livecodebench.py`
extracts the last fenced block from the response, writes it to a temp file, runs
it under `python3` with a 30 s timeout and a 256 MB cap, and compares stdout
exactly against the first three public test cases. A model loses the point for
writing a function instead of a script, for guessing the input serialisation
wrong, for printing `true` where the case wants `True`, or for being slow.

The prompt hands over a LeetCode-style description — *"Return the maximum
distance"* — with an instruction to read stdin and write stdout, and **never
states how the inputs are serialised**. Question 2857's test input is `'5\n10'`,
two values on separate lines. Coder-Next wrote `map(int, input().split())`, which
reads one line, and died on the unpack. The algorithm was right; the guess was
not.

`bench/lcb_audit.py` replays that extraction and execution over the stored
generations and sorts every failure into a bucket. It needs no GPU: oMLX truncates
`predicted` to 200 characters but keeps the full text in `raw_response`.

| bucket | 27B | Coder-Next |
| --- | --- | --- |
| **wrong_output** (ran, produced an answer, answer wrong) | **79** | **71** |
| crash_ValueError (`invalid literal for int()`) | 85 | 137 |
| no_output (ran, printed nothing) | 8 | 44 |
| stdin unpack / EOF | 1 | 7 |
| IndentationError / SyntaxError | 2 | 6 |
| case_mismatch | 0 | 3 |
| IndexError / NameError / TypeError / UnboundLocal | 22 | 8 |
| timeout | 3 | 4 |
| total failures | 201 | 284 |

**Coder-Next produces fewer genuinely wrong answers than the 27B, 71 against 79.**
The entire 83-failure gap is I/O: 201 I/O-shaped failures against 97.

Scoring only the attempts that ran and printed something:

| | raw | conditional on running |
| --- | --- | --- |
| 27B 8-bit | 59.8% | 79.1% |
| Coder-Next 6-bit | 43.2% | 75.3% |
| gap | **-16.6** | **-3.8** |

That -3.8 is the number to believe, and it is corroborated by the two coding
suites with no I/O contract at all: humaneval -3.7 and mbpp -0.2. Three
independent measurements agree on a 0–4 point coding deficit. The 16.6 was a
benchmark artifact.

**Stated honestly:** conditioning on "the code ran" conditions on an outcome the
model partly controls, so this is a decomposition, not an unbiased estimate.

### The obvious implication, tested, and false

The decomposition above suggested the failures were cheap: if the suite never
states its input serialisation, then stating it should recover most of them. That
is a testable claim and `bench/lcb_io_fix.py` tested it. Sixty of Coder-Next's
I/O-shaped failures were regenerated with a system prompt that spells out the
contract — read all input from stdin robustly, the program must run and print,
print only the answer, match boolean capitalisation, one code block — then
extracted and executed through the identical path.

**2 of 60 recovered. 3%.** The failures came back in the same buckets: 42
`crash_ValueError`, 8 `no_output`. Projected across all 200 I/O-shaped failures in
the suite, the score moves 0.432 to 0.445 against the 27B's 0.598.

A first attempt at this test capped generation at 4,096 tokens where the suite
uses 16,384, which truncated long answers into `no_output` and would have
overstated the failure. Re-run at the correct cap, `no_output` fell from 16 to 8
and `ValueError` rose from 37 to 42; the recovery rate barely moved. Seven of the
sixty hit even the 16,384 cap, so Coder-Next writes very long answers on hard
problems — a real cost of its own.

### So §13's headline was right and this section's first draft was wrong

The taxonomy stands: Coder-Next's failures really are dominated by crashes rather
than wrong answers, and it really does produce fewer wrong answers than the 27B,
71 against 79. What does not stand is the inference drawn from it. These are not
convention mistakes a clearer prompt fixes. They reproduce under explicit
instruction, deterministically, in the same buckets.

The honest characterisation is narrower and worse for Coder-Next: **it is roughly
as good at writing a correct function body and substantially worse at producing a
complete, runnable program that honours an I/O contract.** humaneval (-3.7) and
mbpp (-0.2) ask for the former and show almost nothing. livecodebench asks for the
latter and shows -16.6. That gap is real, not a scoring artifact.

For agentic coding this is the wrong weakness to have. Producing complete runnable
artifacts is the job, not a benchmark conceit.

### What the lineup becomes

Coder-Next is 2.0-2.5x the decode speed and 2.6-3.6x the prefill, at a real cost
in end-to-end program correctness and a clear deficit in multi-domain reasoning
(mmlu_pro -6.0, p=0.007, with no I/O contract to blame, as with hellaswag -4.4 and
safetybench -5.4).

It is therefore a latency instrument, not a quality one: worth using where a
verifier checks the output, where the task is bounded edits rather than
whole-program generation, or where 262k of cheap context is the point. The 27B
stays the default for anything whose correctness is not independently checked.

### The lesson worth keeping

Three conclusions in one day, each overturning the last: the score said
Coder-Next was far worse; the taxonomy said the score was an artifact; the
remediation test said the taxonomy's explanation was wrong. Only the last was a
prediction that could fail. Decomposing a result explains where it comes from but
cannot tell you what it means — for that the explanation has to be turned into a
claim about what should happen next, and then checked.

---

## 15. Best-of-N converts the speed into quality, where a verifier exists — SUPERSEDED BY §17

> **This section's conclusion was reversed by measurement on 2026-09-12.** Run
> head to head against the alternative it was supposed to beat, best-of-4
> recovered **2 of 12** problems where the 27B alone recovered **6 of 12**, in
> the same wall-clock. The error was not in the numbers below but in what they
> were compared against: every figure here is a model against *itself* at a
> different N, which is not the decision anyone faces. See §17.
>
> Kept in full because the reasoning is sound given what was measured, and
> because the way it failed is the useful part.

Backlog D2, and after §14 the only route left by which Coder-Next's speed could
buy back its accuracy. The working notes claimed continuous batching makes
best-of-N nearly free; §8 measured the opposite, 1.38x aggregate throughput for
4.9x p95 latency at 8 concurrent requests. Neither settled it, because §8's test
sent *different* prompts and was prefill-bound, where best-of-N sends one prompt N
times — the batching best case.

### Cost: N=4 costs 1.6x the wall-clock, not 4x

`bench/best_of_n.py --cost`, one identical prompt fanned out N ways, measuring
wall-clock to *all* N complete. Aggregate tokens/sec is the wrong statistic here:
nobody waits for the average, they wait for the last stream to finish.

| N | wall to all-N | vs N=1 | aggregate | per stream |
| --- | --- | --- | --- | --- |
| 1 | 5.16 s | 1.00x | 63.2 t/s | 63.2 |
| 2 | 5.90 s | **1.14x** | 115.7 t/s | 57.9 |
| 4 | 8.24 s | **1.60x** | 171.3 t/s | 42.8 |
| 8 | 13.29 s | 2.58x | 228.3 t/s | 28.5 |

§8 was right about its own workload and wrong as a general claim. Four candidates
cost 1.6x the time of one.

### Value: pass@4 recovers 22% of the failures

`bench/best_of_n.py --value`, on 40 livecodebench problems Coder-Next got wrong,
sampled at temperature 0.7 and verified through the same execute-and-compare path
the suite uses.

| | |
| --- | --- |
| greedy re-check | **1 of 40** |
| pass@4 | **9 of 40 (22%)** |
| median wall per problem at N=4 | 28.2 s |

The greedy re-check matters: re-running the same problems greedily recovered
almost nothing, which independently confirms the suite's verdicts and rules out
the chat-path discrepancy §14 flagged as an explanation for the deficit. The gain
is from sampling diversity, not from a different code path.

### What that does to the lineup

Projecting 22% recovery across all 284 failures:

| | livecodebench | effective decode at 16k |
| --- | --- | --- |
| 27B 8-bit, greedy | 59.8% | 33.7 t/s |
| Coder-Next, greedy | 43.2% | 71.4 t/s |
| **Coder-Next, best-of-4** | **~56.0%** | **44.6 t/s** |

Best-of-4 closes the 16.6-point gap to **3.8 points** while still running **1.32x
faster than the 27B does at N=1**. The speed is convertible after all — §14's
conclusion that Coder-Next is "a latency instrument, not a quality one" holds only
at N=1.

**The precondition is a verifier, and it is not optional.** livecodebench supplies
one by executing the code. Best-of-N is worth nothing without something that can
reject bad candidates: in real work that means tests, a compiler, a type checker
or a renderer. Where those exist — which is most of this setup's coding and all of
its diagram work — Coder-Next plus best-of-4 dominates the 27B on both axes. Where
they do not, §14 stands and the 27B remains the default.

This also revives the diagram lane (backlog E) on a stronger footing than it had.
Its whole premise was a deterministic verifier, and best-of-N is a way to exploit
one that needs no fine-tuning at all.

### Prefix-cache audit (backlog B1b)

The cheap half of B1: check whether the shared prefixes opencode sends are
byte-identical across calls, since one nondeterministic field silently converts a
cache hit into a full prefill.

- **Skills are static.** Nine `SKILL.md` files under `~/.agents/skills`, no
  runtime interpolation; `{{.MARP}}` in ey-marp is documentation, not a template
  evaluated at send time.
- **The injected environment block carries a date.** opencode prepends working
  directory, platform and *today's date* to the system prompt. The date changes
  the prefix once per day and the working directory changes it per project.
  Neither is per-request, so within a session the prefix is stable.
- **Placement limits the damage.** The environment block sits after the bulk of
  the static instructions, so a date rollover invalidates the tail rather than
  the whole prefix.
- **One open risk:** the available-skills list is injected after the environment
  block and is assembled from two directories. If that enumeration is not sorted
  deterministically it would reorder between sessions, which is exactly the
  silent cache-buster the working notes warned about. Not confirmed either way
  here; it needs `POST /admin/api/cache/probe` against a live session (B1a).

### Client wiring, and the trap in reading §15 too eagerly

`~/.config/opencode/opencode.json` (symlinked into the dotfiles repo) now carries
all seven model ids, and five agents moved to Coder-Next: `title`, `summary`,
`compaction`, `explore`, `general`, plus `small_model`. `build`, `plan`, `speckit`
and `speckit-wide` stay on the 27B.

The reasoning for that split is the thing worth recording, because §15 invites the
opposite conclusion. **opencode never does best-of-N** — every call is N=1, and at
N=1 §14 applies rather than §15. So Coder-Next takes the work that is
speed-dominated and quality-insensitive, and the 27B keeps everything whose
correctness nothing else checks. Moving `build` would have bought 2x decode on the
main edit loop at exactly the weakness §14 identified.

Two details the model entries have to get right: Coder-Next is
`Qwen3NextForCausalLM`, text only, so `attachment` is false and `modalities.input`
is `["text"]` — the 27B is a VLM and declares image input, and copying its entry
across would advertise a modality this checkpoint does not have. `reasoning` is
false for the same reason there is no `cn-reason` profile.

Residency was already correct and needed no change: the 27B reports
`is_pinned: true` and Coder-Next does not, so Coder-Next is the one that pages out
under pressure — which is the right way round, since the 27B is the quality
default and carries the four profiles everything else depends on.

Verified end to end: `opencode models omlx` lists all seven, and `opencode run
--agent explore` completed a real tool-calling task on `cn-agent` in 21.6 s.


---

## 16. The routing layer, and three things it does not do

LiteLLM behind Postgres, pinned **by digest** rather than tag — a tag can be
repointed at a new image, which is what the 1.82.x malware advisory actually
describes. One virtual key scoped to a `local` access group; a call to anything
outside it is refused 403 before reaching any router, which pre-verifies the
guardrail the cloud work will need.

**Overhead is +21.1 ms median** (direct 295.3 ms, proxy 316.4 ms, 9 interleaved
repeats at `max_tokens=1` so the hop is not drowned by decode). Reported as a
fixed delta rather than a ratio, because a ratio taken on a 1 s request says
nothing about a 20 min one. It is 0.167% of a 16k cold prefill and **2.1% of a
1 s request** — so long-prompt work can move behind the proxy and short-prompt
throughput measurement cannot. One request in nine cost +373 ms rather than
+21 ms, almost certainly the spend-log flush.

Three things that do not work the way the plan assumed:

**`max_budget` does not enforce for local models, even with Postgres.** The
database is necessary and not sufficient: local deployments carry no per-token
pricing, so `spend` stays exactly 0.0 against a 5.0 cap no matter how much
traffic runs. Token counts *are* logged per request, so usage tracking works;
only the budget mechanism is dormant, and it stays dormant until real pricing
arrives with a cloud provider.

**`context_window_fallbacks` is reactive, not proactive.** It fires only when
the backend raises a context-window error. `max_input_tokens` in `model_info` is
otherwise inert metadata that the router never pre-checks: a 4,059-token request
against a deployment declaring 2,048 was served anyway, no fallback, no log line.
The fix is `router_settings.enable_pre_call_checks: true`, after which the same
request routed correctly. Found by shrinking the declared window rather than by
paying a real 131k prefill — the mechanism is identical and the cost is 1/50th.

**The corporate TLS proxy was a non-event.** The root CA was already extracted and `docker pull`
works through the inspection proxy untouched. The CA is mounted into the
container's trust store but is **inert**: nothing in a local-only configuration
makes an outbound TLS call, since oMLX is plain HTTP on the host. Do not record
it as verified until a cloud provider exercises it.

Tool calls survive the hop intact, including Coder-Next's own function-call
format, verified both as a single graded call and as a full `opencode run` task
with file edits.

---

## 17. Best-of-N does not work here, and §15 says why it looked like it did

§15 concluded that sampling N against a verifier converts Coder-Next's speed
into quality. Measured directly, it does not.

**The instrument was wrong, not the arithmetic.** Every cost figure in §15 — and
the 1.75x / 2.40x / 4.84x figures produced while building the gate — compares a
model against *itself* at a different N. Nobody faces that decision. The decision
is between two things a request could be sent to, so both were run head to head
on 12 livecodebench problems the sampler had actually failed, same verifier, same
warm ~3.4k shared prefix:

| arm | recovered | wall-clock | correct answers/min |
|---|---|---|---|
| Coder-Next best-of-4 + verifier | **2/12** | 368.8 s | 0.33 |
| 27B 8-bit greedy, N=1 | **6/12** | 390.9 s | **0.92** |

Three times the correct answers in the same wall-clock. §15 projected best-of-4
landing 1.32x faster at ~56.0 against 59.8; the measured reality is *the same
speed and a third of the answers*.

**Why it fails, mechanically: best-of-N and MTP compete for the same silicon.**
The speculative-decoding win that makes the sampler fast is single-stream only —
1.74x at batch 1, 0.99x at batch 2+ (§8) — so sampling N destroys the very
advantage that was supposed to pay for the extra samples. Measured directly,
per-stream decode collapses **63 → 26 tok/s** from N=1 to N=4 while aggregate
throughput rises only 1.67x. On hardware without speculative decoding the trade
would look completely different. **This is a property of this setup, not of
best-of-N.**

Two further mechanisms worth keeping, both found while costing it:

- **Prefix sharing inside one burst is partial and racy.** N=4 cold on a 6k
  prefix returned `cached=[0, 4096, 0, 4096]` — two of four raced to prefill the
  same blocks and duplicated the work. §15's "a shared prompt is the batching
  best case" holds *across* calls, not *within* one.
- **The straggler dominates.** Wall-to-all-complete is set by the slowest
  candidate; at temperature 0.7 output length varies enough that free-running
  generation cost 4.84x where an equal token budget cost 2.40x. Verifying
  candidates as they arrive and returning on the first pass recovers part of
  that, but cuts time-to-answer only — abandoned candidates keep decoding
  server-side, so the machine still pays for N.

**What survives.** `bench/verifier.py` — the runner, with `ok` and `ran` kept
separate so "verifier rejected it" and "verifier never ran" can never be
confused. What is closed is *sampling N to pick a winner*, not *verification*.
Re-open only if the lineup loses MTP, or if a verifier appears whose pass@N
recovery is far above livecodebench's 22%.

---

## 18. The prefill memory guard, and two retractions

This section was written wrong twice before it was written right. Both wrong
readings are recorded, because both were plausible and one is the obvious guess.

**What the guard actually projects.** For a 27B request with a single model
resident, the KV+SDPA projection is linear in prompt length:

| prompt | projection |
|---|---|
| 4,000 tok | 0.599 GB |
| 32,000 tok | 1.870 GB |
| 128,000 tok | 6.210 GB |

Slopes of 45.4 and 45.2 KB/token — so **~45 KB/token plus ~0.42 GB fixed**. It
is neither a fixed arena nor sized to the configured context.

**Retraction 1: "KV is sized to the configured context."** Inferred from a 43 GB
figure seen against an 8k prompt on a 131k profile, and it predicted `qwen-long`
could no longer reach its verified ceiling. **Tested and false** — a ~240,000
token prompt was accepted and completed in 907 s.

**Retraction 2: "the guard over-estimates KV by ~9x."** Also wrong, and in the
opposite direction. The per-request projection above is *lower* than the ~129
KB/token of host memory growth measured during prefill. Those measure different
things — host growth includes allocator slack and more than the KV tensor — but
there is no 9x over-estimate to fix. **The 42–43 GB figures that started all this
were observed only during concurrent load/eviction transitions and remain
unexplained.** They are real and reproducible in that condition and do not
describe steady-state behaviour.

**The real constraint is residency, and it is simple arithmetic.** Under the
balanced tier the ceiling is 102.14 GB:

| | GB |
|---|---|
| 27B weights + Coder-Next weights | 96.24 (oMLX's own figure) |
| headroom left for KV | **5.90** |
| 27B weights + oQ4e weights | 44.2 |
| headroom left for KV | **57.9** |

With Coder-Next as the partner there is no room for a second model's KV, so any
substantial request evicts it — measured at **~20–21 s per alternation, on every
alternation**, with an outright `prefill_memory_exceeded` refusal for requests
landing mid-transition. With oQ4e the same alternation runs at **2.6 s with zero
evictions**. That is the whole of §19's practical case.

**Guard configuration, now established.** Tier accepts exactly `aggressive`,
`balanced` and `custom`. `memory_guard_custom_ceiling_gb` **does take effect at
runtime** — an earlier note calling it inert was wrong; it is simply *ignored
unless the tier is `custom`*, and setting `custom` without a positive ceiling is
rejected outright. Model loading is checked separately from prefill, against
`current + model size`. None of these levers were needed once the lineup fell to
44 GB, and they are the first thing to reach for if a third model is ever added.

---

## 19. The lineup: 8-bit for quality, oQ4e for speed

**Build method does not rival bit count.** The one genuine data-driven mixed-up
4-bit build on the Hub performs the same as stock RTN 4-bit — p = 0.582
reasoning, p = 0.096 code — and both lose to the shipped 8-bit:

| comparison | reasoning | code |
|---|---|---|
| oQ4e vs RTN 4-bit | p = 0.582, no difference | p = 0.096, no difference |
| oQ4e vs 8-bit | **8-bit better, p = 0.0095** | **8-bit better, p = 0.00119** |

That closes the quantisation lane barring a DWQ checkpoint, which is a different
procedure and does not exist for either model. It also **fails to replicate §13's
one encouraging result**: RTN 4-bit matched the 8-bit on code at p = 0.130, oQ4e
does not (livecodebench 59.8 → 54.0, p = 0.00193). "4-bit is safe for code" was
the more favourable of two samples, not a fact.

**But oQ4e replaces Coder-Next**, which is where its value turned out to be:

| oQ4e vs Coder-Next | |
|---|---|
| code | **oQ4e better, p = 0.00006** — livecodebench **43.2 → 54.0** |
| reasoning | no difference, p = 0.383 |
| weights | **16.0 GB** vs 64.7 |
| residency with the 8-bit | **no eviction, 2.6 s swaps** vs eviction every time, ~20 s |
| decode @16k | 58.8 vs 71.4 tok/s |
| prefill @16k | 798 vs 2,475 tok/s — **Coder-Next 3.10x** |

Coder-Next keeps one durable advantage, 3.1x prefill from its hybrid attention.
It does not survive the arithmetic: at 16k that saves 13.9 s per call against a
~20 s alternation charge, break-even at ~1.4 prefills per alternation, on a
routing table that alternates on almost every call. It was losing before quality
was counted.

Note oQ4e has **no prefill advantage over the 8-bit** — 798 vs 793 tok/s, same
architecture. Fewer bits buys decode (1.73x) and memory, not prefill. Its
in-checkpoint MTP is also better-behaved than the 8-bit's: `mtp_compatible=true`
with no sidecar drafter needed, measured 2.12x against the 8-bit's externally
drafted 1.97x.

---

## 20. Prefix-cache hit rate under a real agent loop: 86.7%

§10 ranked this the highest-value unmeasured lever. Measured on a real `opencode`
task — glob, read, edit — across 5 requests averaging 7,088 prompt tokens:
**30,720 of 35,440 tokens reused**.

That 30,720 is exactly **15 × 2,048**, and the coincidence is the finding: **the
cache works in whole 2,048-token blocks**. A 4,523-token prompt reuses exactly
4,096 on repeat; a 22-token prompt reuses **zero, always, however often it is
sent**. So the achievable hit rate is bounded by
`floor(tokens/2048)*2048/tokens`, and short calls are not slow to cache, they are
structurally uncacheable.

The practical answer is therefore favourable: agentic loops are **not** paying
cold prefill repeatedly, because at ~7k tokens a request clears three blocks
comfortably. The date and cwd that opencode injects roll over daily but sit in
the tail, where they invalidate at most one block.

Measure it with `bench/cache_probe.py`, which reports the **delta** between two
marks. The cumulative `cache_efficiency` on `/admin/api/stats` is not usable on
its own — it read 81.4% over 729 requests on a server that had served ~40 h of
pathologically repetitive benchmark traffic.

---

## 21. Task-shaped metrics, and the three ways they mislead

`bench/taskbench.py` scores what is actually wanted from these models against
deterministic verifiers rather than a judge: PlantUML renders, draw.io XML has a
real `mxGraphModel`, Python behaves as asserted, tool calls are usable. It never
touches the accuracy queue, so §11's upload does not apply.

**The first version measured nothing.** Sixteen tasks, both tiers scored 16/16,
and the tool correctly reported "no significant difference" — a statement about
the tasks, not the models. A task set everything passes is exactly as
uninformative as one nothing passes. Difficulty is now verified by writing a
reference answer for every hard task rather than assumed.

Three traps, each of which produced a confident wrong answer first:

**Temperature confound.** Comparing role aliases `coder` (temp 0.7) against
`fast` (temp 0.3) showed the **4-bit model winning 27/27 to 24/27**. That is
sampling, not capability: `force_sampling` means the profile's temperature
applies and cannot be overridden per request, so comparing aliases compares
*profiles*. Like-for-like at 0.7 the direction reverses to **98% vs 91%**.
Compare profiles when choosing a profile; compare equal sampling when choosing a
model.

**Network-dependent verdicts.** A model emitting
`!include https://raw.githubusercontent.com/...` makes the verdict depend on
GitHub; one task failed on a **404** with perfectly good diagram source. Those
are now inconclusive rather than failures.

**The locally installed PlantUML rejects `par ... and ... end`** (1.2026.7, exit
200) while accepting `par ... end`, `group`, `opt` and `break`. Textbook-correct
source that will not render on this machine — worth knowing before trusting any
PlantUML output here.

**Capability and reliability are reported separately** because they disagree:
8-bit and oQ4e are identical on "passed at least once" (27/27 vs 26/27) and
**98% vs 91%** per attempt. And the harness prints its own power limit — 27 tasks
resolve differences of roughly 15 points and up, so that 7-point gap is
**unmeasured, not absent**. Removing the upload and the suite-score blindness
does not manufacture statistical power.

---

## 22. Parameter count is not the lever on 128 GB — the GGUF probe, closed

Backlog B5 and A4. The question was whether a much larger model at lower
precision beats the shipped dense 27B, and whether that justifies maintaining a
second serving stack. Both answers are no.

**The probe as specified could not run, and would have confirmed its own
premise.** DeepSeek V4-Flash at IQ2 is 110–125 GB against a 107.52 GB Metal cap,
exceeded before any KV; fitting it means raising `iogpu.wired_limit_mb` to ~120
and leaving macOS ~8 GB, which §H already documents as thrashing and kernel-panic
territory. And at 284B, *anything* that fits is ~2.5 bits/param — the degraded
regime the rule already predicts loses.

**There is no larger Qwen3.8 to substitute.** The family is 27B dense plus 2B/4B/9B
distills. `Qwen3.8-Flash-Next` is 180B but its architecture is
`Qwen4ExpForConditionalGeneration` (`model_type: qwen4_exp`) — a different
architecture, not a scaled-up Qwen3.8. So **every candidate confounds size with
generation or architecture**; there was no clean version of this experiment
available.

`unsloth/Qwen3.5-122B-A10B` was chosen as the least-bad: 10B active sits between
Coder-Next's 3B and the 27B's dense, both quants clear the degraded regime, and
it is an Unsloth dynamic quant — the one thing B5 named as a reason to build the
lane at all. Served by llama.cpp; the same 150 livecodebench questions the
shipped 8-bit answered, replayed and graded through the same extract-and-execute
path (`bench/gguf_probe.py`).

| | livecodebench (150q) | vs 27B | p |
|---|---|---|---|
| **Qwen3.8-27B-8bit** (shipped) | **63.3%** | — | — |
| Qwen3.5-122B Q5_K_S, 86.4 GB | 56.7% | −6.7 | 0.087 |
| Qwen3.5-122B Q4_K_M, 76.5 GB | 55.3% | −8.0 | **0.043** |

**The extra bit bought nothing**, and that is the load-bearing part: Q5 against
Q4 is p = 0.790, +1.3 points, 6 flips each way. Precision was therefore *not* the
limiting factor — at 4 bits the model was already expressing what it has — so the
size result cannot be dismissed as quantisation damage.

**Three independent measurements now converge:**

| | delta vs the dense 27B |
|---|---|
| 80B MoE, 6-bit (Coder-Next, §13) | −16.6 |
| 122B MoE, 4–5 bit (here) | −6.7 to −8.0 |
| same model at 4-bit vs 8-bit (§19) | worse on both axes |

Parameter count is not the lever on this machine. **B5 closes with it:** nothing
in the GGUF lane justified a second model store, and llama.cpp was uninstalled
after the run.

### Three things worth keeping beyond the verdict

**Thermal drift is real, and it is ours too.** Per-question time climbed
**12.1 → 22.3 s within a single run**, ~45% degradation over ~50 minutes. This
independently reproduces a third-party M5 Max measurement of −23% over 3 minutes
of sustained decode. Interleaved A/Bs are unaffected — drift hits both arms — but
**any absolute throughput figure taken late in a long run is understated**, which
includes the tail of every multi-hour accuracy suite in this document.

**105.9 GB of a 107.52 GB cap, with 3.5 GB of swap in use.** Q5_K_S completed,
but that is the edge of what this machine does. Reducing llama.cpp's slots from
4 to 1 recovered only 0.6 GB — KV is small beside an 86 GB model, so there is no
configuration trick that makes something bigger fit.

**The probe nearly produced a garbage result.** Qwen3.5 defaults thinking **on**
in llama.cpp's chat template: the first test returned an **empty** `content`
after 200 tokens of `reasoning_content`, finish_reason `length`. Unnoticed, all
150 questions would have scored near zero for a reason with nothing to do with
the model. The reference arm ran `enable_thinking: false`, so matching it via
`chat_template_kwargs` was a fairness requirement rather than a tweak. Any
cross-stack comparison has to verify that the *chat template* agrees, not just
that the endpoint answers.

---

## 23. DFlash2: the drafter now exists, and it is a silent no-op in oMLX

Backlog B4 was parked because "`dflash_draft_model` needs a purpose-built
block-diffusion draft checkpoint and no local model qualifies". That is no longer
true — `z-lab` publishes a DFlash2 drafter for **exactly** `Qwen/Qwen3.8-27B`, and
MLX builds of it exist. So the stated blocker lifted. The result is still no.

**Assembling it takes three repos**, none of which is sufficient alone:
`ProCreations/Qwen3.8-27B-DFlash2-MLXFast-Q4` has 4-bit weights and **no
config.json**; `KaedeTai/dflash2-mlx-quantized-draft` has the config and **no
weights**; `cfontes/qwen3.8-27b-mlx-dflash2` is an MLX **port of the code**. The
assembled drafter is architecturally sound: 175 tensors, 5 layers, 94 quantised
tensors at 4-bit/group-64, hooking target layers 5–61 against the 27B's 64, with
`hidden_size` 5120 matching on both sides.

**oMLX only scans models at startup.** The drafter was invisible until a restart
via `POST /admin/api/server/restart`, after which oMLX registered it correctly as
`is_helper: True`, `engine_type: batched`, 1.24 GB. An earlier conclusion that
oMLX "cannot consume it" was premature and wrong — it simply had not looked.

**It configures cleanly and does nothing.** oMLX accepted
`dflash_enabled: true` with the drafter (it enforces exclusivity with MTP —
"choose one speculative-decoding path"), reloaded without error, and then decoded
at dense speed:

| configuration | tok/s |
|---|---|
| dense baseline (§2) | ~17.3 |
| **DFlash2-Q4 drafter** | **17.9** |
| DFlash2-Q4 with `dflash_block_size: 8` | 17.6 |
| **shipped MTP** | **31.1** |

**0.58× MTP, and indistinguishable from no speculative decoding at all.** Setting
`dflash_block_size` to the drafter's own 8 changed nothing. oMLX evidently falls
back to dense when the drafter is not in the format its DFlash path expects —
`DFlash2DraftModel` / `model_type: qwen3` is the mlx-lm and sglang shape, whereas
oMLX's working MTP drafter declares the native `model_type: qwen3_5_mtp` with
`text_config`/`vision_config`. No oMLX-native DFlash drafter exists publicly for
any model.

**This is the worst failure mode there is: it looks like success.** No error at
configuration, no error at load, no warning in the settings readback — only a
throughput number that anyone not measuring would never see. Anything switched on
here has to be verified by measurement, not by the absence of an error.

**Where the attractive number came from.** A third-party benchmark on this exact
hardware (M5 Max, 128 GB, mlx-lm 0.31.3) reports DFlash2 at **76.1 tok/s** with a
quantised drafter, 1.53–1.87× over dense across three 4-bit 27B targets. Those
runs are raw `mlx-lm` plus the community MLX port — **a third serving stack**.
That puts DFlash2 exactly where the GGUF lane ended up in §22: a real speedup,
reachable only by maintaining another stack. The same answer applies.

**B4 closes.** Not "no drafter exists" any more, but "oMLX cannot use the
drafters that exist, and the shipped MTP is 1.7× faster than what we can
actually run". Re-open only if oMLX ships native DFlash drafter support, at which
point the assembled drafter is already on disk at
`~/.omlx/models/ProCreations/Qwen3.8-27B-DFlash2-Q4`.

**Byproduct worth keeping:** `POST /admin/api/server/restart` exists and is
graceful — the server was back in ~30 s and re-registered every model and
profile. That is the only way to make oMLX notice a hand-placed model directory.

---

## 24. What a full agentic session revealed about the setup

A complete Spec Kit run — constitution through implement, 32 tasks, 12 modules,
52 tests, ~28 min on `qwen-code` — used as a load test of the stack rather than
as a project. Three findings generalise beyond it.

### The dominant failure mode of this stack is "configured and inert"

Five instances now, none of which raised an error:

| | what was configured | what actually happened |
|---|---|---|
| §7 | profile temperature | client default silently replaced it |
| §16 | `context_window_fallbacks` | inert without `enable_pre_call_checks` |
| §23 | DFlash2 drafter | loaded, registered, decoded at dense speed |
| §24 | speckit commands → `speckit` agent | ran on whichever agent was current |
| §24 | `LITELLM_LOCAL_KEY` in `zshrc` | invisible to non-interactive shells |

Every one presented as success, or as an unrelated symptom, and every one was
found by measuring rather than by reading config. **On this stack, enabling
something and observing no error is not evidence that it is on.** That is now
the single most repeated lesson in this document and it deserves tooling rather
than vigilance.

### Tests passed and both real defects still shipped

52 tests, 0 failures, and the two bugs a human found in under a minute of play
both survived:

- **Non-uniform canvas scale** — `setTransform` scaled X and Y independently, so
  every circle rendered as a ~2.8:1 ellipse. No test asserted the transform was
  uniform, because nothing in the spec said it had to be.
- **A units mismatch** — constants documented `px/step` were tuned for 60 Hz
  while the loop ran at 120 Hz, making everything move at double speed. This
  passed every test *because the tests asserted per-step values that were
  consistently wrong together.*

The second is the instructive one. The suite verified **internal consistency**,
not **external correctness**: it never asked how long a pipe takes to cross the
screen in seconds. A model will happily produce a self-consistent world with the
wrong constant in it, and a test suite written by the same model in the same
context will agree with it. **At least one assertion per subsystem should be in
human-meaningful units** — seconds, pixels of travel, real-world rates — because
those are the ones a self-consistent error cannot satisfy.

Neither bug was an implementation slip. Both were specification gaps, which is
also why they were invisible to a phase (`analyze`) that checks artifacts
against each other rather than against reality.

### The verifier that was built for exactly this never ran

`tools/visual_critic.py` — screenshot each state, critique against a written
standard — was built, tested end to end, and then **never invoked once** during
the session. `visual/reports/` was empty. The oval bird was found by eye.

This is the same shape as §17's result about best-of-N, arriving from the
opposite direction: there, a verifier existed and the economics did not justify
using it; here, the economics were excellent and the loop was simply not part of
the workflow. **A verifier that is not a gate is a verifier that does not run.**

### Token shape, for capacity planning

| phase | model | calls | prompt tok | completion tok |
|---|---|---|---|---|
| spec / plan / tasks | `qwen-reason` | 52 | 2,824,527 | **99,889** |
| implement | `qwen-code` | 256 | **8,470,981** | 61,477 |

The reasoning phases produced *more* output in a fifth of the calls — thinking
tokens doing real work. Implement is prompt-dominated at a 138:1 ratio, which is
the agentic loop resending context and is exactly the regime §20's 86.7%
prefix-cache hit rate makes affordable. Without that cache this session would
have been dominated by re-prefill.

### A doctor for it — and the doctor had the bug

`bench/doctor.py` asserts the stack is doing what it is configured to do, in
three layers matching the rates at which they change: **harness** (opencode
today, something else later), **proxy** (the LiteLLM role aliases — the constant
every harness and engine meets), **engine** (oMLX today, perhaps Ollama
alongside). Engine checks are registered rather than hardcoded so a second
engine is an addition.

Each result is labelled `obs` or `cfg`, because "I measured this" and "I read
this in a file" are not equally good evidence and the whole §24 pattern is
config that reads correctly and behaves wrongly. 16 checks, 11 observed, ~16 s.

Validated by **fault injection**, not by passing: disabling MTP drops the 8-bit
from 37.7 to 17.8 tok/s and the check fails; pinning an agent into a Spec Kit
command file is caught as upgrade-fragile.

**The third injection exposed the tool repeating the bug it was written for.**
The env check ran `zsh -c 'printf %s "$VAR"'` and inherited `$VAR` from the
calling process, so it passed even with the variable deleted from `shell/env` —
a check that could never fail, reporting success. Fixed with `env -u VAR` so the
child must re-derive it from a startup file. That a tool built specifically to
detect inert configuration shipped inert is the strongest available argument for
the rule it enforces: **a check that has never been observed to fail has not
been tested, it has only been run.**

---

## 25. Spec Kit against no Spec Kit: one run each, and the unaided one was cleaner

An A/B on whether the GitHub Spec Kit workflow earns its cost in this setup.
Same game, same models, same standards, same verification tooling; only the
workflow differed.

**Controls, stated because they decide whether the result means anything.** Run
B got run A's constitution verbatim as `AGENTS.md` and the same
`visual_critic.py`, so standards and verification were held constant rather than
being things Spec Kit uniquely supplies. B's brief was A's `/speckit.specify`
and `/speckit.plan` text verbatim, carrying **no knowledge of A's defects** — a
brief that warned about them would have made B win for a reason unrelated to
process. B was "ad-hoc plan-then-build in chat": the reasoner planned, the coder
implemented, no phases and no artifacts.

| | A — Spec Kit | B — ad-hoc |
|---|---|---|
| modules / source lines | 13 / 901 | **7 / 613** |
| tests | 60 | **29** |
| spec artifacts | 979 lines | 0 |
| defects reaching a human | **2** | **0** |
| rework rounds | 2 | **none** |

**B avoided both of A's defects outright**, and they were the two this document
spent §24 on:

    A   ctx.setTransform(sx, 0, 0, sy, 0, 0)      every circle a 2.8:1 ellipse
    B   ctx.setTransform(dpr, 0, 0, dpr, 0, 0)    uniform

    A   gravity: 0.25    px/step, tuned for 60 Hz, loop ran at 120
    B   gravity: 1800,   px/s^2, integrated with an explicit dt

B's config header states the principle unprompted: *"Physics is expressed in
per-second units and integrated with an explicit dt, so logic is independent of
frame rate."* That is the fix A had to be told to make, expressed as *fix the
bug class, not the numbers* — and B arrived at it without being asked.

**The one mechanism difference that is directly observable**, as opposed to
inferred: B wrote `test/constitution.test.js`, asserting against the standards
document — five colour properties declared, accent not reused for hazards,
spacing on the 4 px scale, and *"physics is expressed in per-second units, not
per-frame"*. B wrote fewer than half as many tests as A and spent some of them
on **external correctness against the written standard**, which is precisely the
gap §24 identified in A, where 60 tests verified internal consistency and caught
neither real bug.

### What this does not establish

**Causation. One run against one run.** §8's lesson applies with far more force
here than it did to a 500-question suite: if that could not resolve two points,
a single build each cannot resolve a workflow. Zero-versus-two is a wide gap and
it is still one sample, at temperature 0.7, from a model measured at 63.3% on
livecodebench. **Sampling is a live explanation and has not been excluded.**

Three other hypotheses, none tested: that Spec Kit's artifacts act as a lossy
compression step in front of a 131k-context model that did not need compressing;
that 32 tasks across 7 phases create 32 narrow contexts in which cross-cutting
decisions like "uniform scale" belong to no single task; or that making the
standard executable is the whole of the effect and is available to either
workflow. Settling this needs repeat runs each way, which is affordable and has
not been done.

**A prediction of mine was falsified, which is the useful part.** Before the run
I predicted B would hit the same two defect classes, reasoning that both were
specification gaps and an ad-hoc plan was no more likely to close them than
Spec Kit's phases. It hit neither. Recording that matters more than the result,
because the reasoning felt sound and was not.

**The cost comparison was lost, through my own error.** A spend marker was set
on the LiteLLM log so B's tokens would be attributable, but B ran against the
direct `omlx/` provider rather than the proxy, and oMLX's own counters had been
reset by an unrelated server restart. Session span was 1 h 50 m wall-clock with
147 `qwen-code` and 11 `qwen-reason` calls, but that includes idle time and is
not comparable to A's measured ~28 min of implement. **An instrument that only
covers one of two possible paths does not measure the thing it was pointed at** —
the same shape of error as the doctor's env check in §24.

### Disposition

**Use Spec Kit selectively rather than by default.** It earns its cost where the
artifacts are the deliverable, or where several people must agree on a spec
before code exists. For solo build-and-iterate against a written standard, one
run each says it cost 979 lines of artifacts and 31 extra tests without buying
defect protection — and that is a weak claim on one sample, so the disposition
is "stop defaulting to it", not "stop using it".

**The transferable habit is B's, not the workflow's**: tests that assert against
the standards document. That is available in either workflow and belongs in the
constitution regardless of which one is chosen.

## 26. Qwen3.8-Flash-Next: fast, genuinely accelerated, and it cannot share the machine

§22 closed "parameter count is not the lever on 128 GB" against GGUF MoEs and
dismissed this checkpoint in passing, on the grounds that `qwen4_exp` is "a
different architecture, not a scaled-up Qwen3.8". That was the right reason to
test it rather than to skip it: three of every four layers are Gated DeltaNet
with a fixed recurrent state, so it does not obey §22's arithmetic. Measured with
`Jundot/Qwen3.8-Flash-Next-oQ4e-mtp` (106.3 GB on disk, 125B total / 6B active,
plus a 51.2B hashed n-gram table and a 4B Lightning MTP head).

**Two of the three questions came back positive, and the third disqualifies it
from the lineup as it stands.**

### MTP is real here, and it is worth 1.87x

Unlike the 8-bit — whose declared MTP layers have no tensors, so its win comes
from a separate `Qwen3.8-27B-MTP-8bit` drafter (§H) — this checkpoint's
in-checkpoint Lightning head works. Crossed against the vendor's presence
penalty, 5 repeats per cell, 400 generated tokens
(`results/fn_mtp_check.json`):

| `mtp_enabled` | `presence_penalty` | tok/s | stdev |
|---|---|---|---|
| True | 0.0 | **74.57** | 4.02 |
| True | 1.5 | 74.07 | 2.44 |
| False | 0.0 | 39.97 | 1.24 |
| False | 1.5 | 39.43 | 0.58 |

**§7's rule needs narrowing.** §7 established that a non-zero presence penalty
costs MTP its entire win, and that no profile may set one. That holds for
drafter-based speculation and does **not** hold for an in-checkpoint Lightning
head: the vendor's `presence_penalty: 1.5` costs 1% here, inside the noise. The
`fn-*` profiles still ship 0.0 for comparability with every other arm, but
`profiles/flashnext.py` no longer rejects the vendor value, and `repetition_`
and `frequency_penalty` stay rejected because they were not the ones measured.

### Decode is the fast tier's, not the quality tier's

Same instrument, same prompt, 400 tokens, 5 repeats
(`results/fn_decode_baselines.json`):

| model | decode tok/s | resident |
|---|---|---|
| Qwen3.8-27B-8bit (drafter MTP) | 43.67 | 28.2 GB |
| Qwen3.8-27B-oQ4e-mtp | 71.29 | 16.0 GB |
| **Qwen3.8-Flash-Next-oQ4e-mtp** | **74.57** | 69.4 GB |

So it decodes 1.71x the shipped quality tier and 4.6% faster than the existing
speed tier — for 4.3x the speed tier's memory. On throughput alone it buys
nothing the lineup does not already have.

**oMLX's own throughput harness cannot measure it single-stream.** Every
`bench/throughput.py` run with `batch_sizes=[]` returned a bare `error` with no
message in ~6 s, while the same request with `batch_sizes=[2,4,8]` completed.
The batch request's own single-stream row is unusable as a comparison: it
reported 40.0 tok/s and then 19.5 tok/s for identical configuration on two
consecutive runs. The numbers above come from `timed_chat` instead, which is why
they are not comparable to `tp_mtp`'s.

### Three memory ceilings, and the two that matter are not the documented one

| ceiling | value | what it governs |
|---|---|---|
| `final_ceiling` | 115.45 GB | what `force_ssd_offload` consults |
| `metal_cap` | 107.52 GB | what actually admits a load |
| dynamic | 91.29 GB | moves with reclaimable host RAM |

An earlier draft of `profiles/flashnext.py` claimed §H's 107.52 GB was stale
because `health()["engine_pool"]["final_ceiling"]` reports 115.45 GB. **§H was
right and that draft was wrong.** `final_ceiling` is not exposed as the binding
figure and is not the one the guard enforces; `metal_cap` is, and the dynamic
ceiling can sit 16 GB below it depending on what else is running. The only place
any of them is visible is a refusal message.

**The PLE offload has to be asked for, and the default is unusable.** oMLX only
forces the n-gram table to SSD when the resident figure exceeds `final_ceiling`
and the mmap figure does not. At 111.6 GB estimated it fits, so nothing fires.
Left alone the model loads with the table resident (99.21 GB, 103.1 s) and then
oMLX's *prefill* guard rejects even a trivial prompt, because nothing is left for
KV. Offloaded it is 69.37 GB, loads in 30.9 s cold, and works. There is no
configuration in between.

### It does not coexist — and this is what closes it

`results/flashnext_residency.json`, alternating 6000-token prompts against the
8-bit for 3 rounds:

- **An unload does not release synchronously.** Loading immediately after one was
  refused against the dynamic ceiling while the pool still reported the old
  figure. Every script here now waits for `current_model_memory` to drain.
- **3 evictions across 6 calls.** Both models can be held (69.4 + 28.2 GB fits
  under the 107.52 GB cap), but a call that lands on Flash-Next costs a median
  **63.5 s** against the 27B's 3.9 s, and the reload dominates that, not the
  prefill. That is 3x the ~20 s alternation that retired Coder-Next (A2c).
- **The weights are not what fails to fit — the KV is.** Two resident models
  leave ~10 GB, and a 6000-token prompt does not fit in it. This is §18's prefill
  guard, in the shape §18 predicted.

### The refusal was a pinning artifact, and oMLX is already a llama-swap

A first draft of this section read the failed load as "the admission guard does
not evict to make room, it refuses". **That is wrong, and the correction matters
more than the original claim.** All three lineup models carry `is_pinned: true`;
`engine_pool._find_lru_victim` skips pinned, in-use, and actively-generating
entries; so with the whole lineup pinned there was nothing evictable and the load
raised `InsufficientMemoryError` — whose message ends "or unload another model".

(The field is spelled two ways, which costs a query to discover: `pinned` in
`/admin/api/models`, `is_pinned` in a model's settings and in the PATCH body.)

Everything a model swapper needs is already in the pool:

| capability | where | state on this machine |
|---|---|---|
| LRU eviction, skipping pinned / in-use / active | `engine_pool._find_lru_victim` | active |
| evicts to the soft watermark *before* new weights allocate | admission path | active |
| per-model pinning, preloaded at startup | `is_pinned`, `preload_pinned_models()` | all 3 lineup models pinned |
| per-model and global idle TTL | `ttl_seconds`, `idle_timeout_seconds` | **both unset — never auto-unloads** |
| guard aggressiveness | `memory.memory_guard_tier` | `balanced` (safe/balanced/aggressive) |

**So the 3-of-6 evictions were that mechanism running, not a failure of it.** The
8-bit is pinned and Flash-Next is not, which made Flash-Next the only possible
victim: reloaded on its turn, evicted on the 27B's. The measurement is therefore
better read as **the price of automatic swapping on this hardware, 63.5 s versus
3.9 s resident** — not as evidence that swapping is missing.

**Unpinning the lineup would not buy coexistence.** It converts a loud refusal
into quiet thrashing at the same 63.5 s. And an external swapper (llama-swap and
kin) is a step backwards here: it supervises one *process* per model, so it either
duplicates the 69 GB or drops the oQ quant, in-checkpoint MTP and PLE offload that
make this checkpoint viable at all, and it cannot reach inside oMLX's pool.

**The circularity worth naming:** cold load 30.9 s, warm 12 s. A fast swap needs
~69 GB to stay in the page cache, which is the memory the swap was freeing.
Swapping here can be made deliberate, not cheap.

### Disposition

**Not a tier, on the evidence so far.** It is a replacement-only model that
decodes like the 16 GB fast tier while occupying 69.4 GB and evicting everything
else. The `fn-*` proxy entries exist for benchmarking and are deliberately wired
to no role alias.

**What is still unmeasured, and could still change the verdict:** the axis the
vendor actually claims. DeepSWE 58.7 vs 42.2 and JobBench 55.7 vs 33.4 are
agentic, and §14 found livecodebench blind to exactly that. `task_flashnext`
(needs the proxy restarted to pick up the `fn-*` entries) and the paired code
suite against `acc_ship_code` are both still to run. If the agentic gap is real
and large, a 63.5 s model swap on an explicit "hand this to the big model"
gesture is a different proposition from a 63.5 s swap on every routing decision —
which is the mechanism the backlog now carries as A6c.

**The cheapest lever found is `ttl_seconds`, and it is unset.** The waste is not
the swap, it is holding 69.4 GB after the last agentic call; a per-model TTL makes
the model self-evicting and costs nothing while it is idle. Pinning policy then
decides who the victim is, which is a routing decision wearing a memory setting's
clothes. Raising `memory_guard_tier` from `balanced` to `aggressive` is the third
lever and the one to reach for last, because it buys headroom by shrinking exactly
the prefill margin §18 exists to defend.

**Thinking works and costs 3.1x the tokens.** `results/fn_thinking.json`, 4 tasks
x 5 repeats x 3 arms: thinking-off 48.91 tok/s at 77 median completion tokens,
thinking-on 44.23 tok/s at 171, no cap hits and no empty completions in 60 calls.
The template defaults thinking ON at `reasoning_effort: xhigh`, the inverse of
every other model here, and `bench/accuracy.py` has no `chat_template_kwargs`
path — so the preflight in `bench/flashnext_thinking_ab.py` gates the code suite
rather than trusting it. It passes: thinking-off emits 0 reasoning characters
against thinking-on's 393.

---

## 27. Flash-Next, closed: it works, and it cannot be wired

§26 measured three of four axes and left the agentic one open behind a proxy
restart. A reboot resolved that, the axis ran, and the run turned up enough
defects in the instruments that §26's headline has to be withdrawn. The
conclusion survives; almost none of the numbers under it do.

**The one-line answer: it is 1.41x faster than the shipped 8-bit at
indistinguishable quality, and it can only serve when nothing else is resident,
so it earns no tier.**

### The agentic axis: no gap, on a saturated instrument

`bench/taskbench.py` at 3 repeats through `fn-agent` (`results/task_flashnext.json`):

| | drawio | puml | pycode | toolcall | total |
|---|---|---|---|---|---|
| flashnext | 100% | 100% | 100% | 80% | 78/81 = 96% |

McNemar against every arm on disk: **p = 1.00000 for all three** — `coder` (the
shipped 8-bit), `fast` (oQ4e) and `oqcode`. The vendor's claimed agentic gaps are
DeepSWE +16.5 and JobBench +22.3, both above this task set's stated ~15-point
resolution, so if they transferred the instrument should have seen them.

It did not, and the honest reason is that **taskbench is saturated**: drawio,
puml and pycode are 100% for all four arms, so 22 of 27 tasks carry no signal and
only the 5 toolcall tasks discriminate. This is "no evidence of the claimed gap",
not "the gap is disproven". §21 already warned this set mainly measures whether a
profile is wired correctly; that limit is now load-bearing rather than academic.

The single failure is consistent and informative: `htool-escape` failed 3/3 with
`wrong_function: read_file` — identical reason, identical 52 completion tokens,
identical 1.7 s. With only `edit_file` in the schema it invented a tool to read
the file first. `tool-edit` and `tool-edit2` pass with the same tool and the same
verifier, so this is not general tool-call weakness; it is triggered by a prompt
that stresses exactness ("quotes and backslashes that must survive exactly").
The same bias showed up unprompted during the truncation probe below: handed 16k
of source with no instruction and no need for tools, one of six replies came back
`finish_reason: tool_calls` — "I'll start by exploring the repository structure".
**A model that reaches for tools unbidden, including ones not in its schema, is a
specific hazard in an agent loop**, and it is the one quality question left open.

### Three corrections to §26

**1. The decode figures were measured on generations that never happened.**
`bench/throughput.py` reported 21.0 tok/s for Flash-Next at pp=16384. The
underlying payload says `completion_tokens: 2` against a requested 128. A rate
over two tokens is 1/TPOT with first-token settling folded in, not a decode rate.
Across 11 runs at 16k the model returned 1, 2, 3, 47, 48, 50 or 128 tokens; every
other model on this machine returns the full 128 every time. Split on that:

```
truncated (<10 tok)   18.8  19.4  19.8  21.0  21.3
real (>=47 tok)       36.3  37.9  41.0  43.7  45.3  52.5     median 42.4
27B 8-bit control           32.4  33.2  33.3                 median 33.2
oQ4e fast tier              58.8
```

The apparent "decode collapses at long context" was five truncated runs. The real
figure is ~42 against the 8-bit's 33.2 — a win, but a smaller one than §26's
74.57, which came from `timed_chat` and was never comparable to `tp_*` anyway.
The guard added to `bench/throughput.py` flags exactly these rows and no others
(7 flagged, 50 passed, across every `results/tp_*.json` on disk).

**2. There are two ceilings, not three, and the third was a unit error.**
§26 reported "final_ceiling (115.45) is what force_ssd_offload consults,
metal_cap (107.52) is what admits a load". Those are the same number:

```
115,448,725,504 bytes = 115.45 GB (decimal)  = 107.52 GiB (binary)
```

`health()` reports it in decimal GB; the guard prints it in GiB and labels it
"GB". oMLX mixes the two units in its own messages, so any figure read off a
guard string has to be checked against `health()` in bytes before it is compared
to anything.

**3. The 63.5 s swap is a cold-PLE number.** The PLE's SSD cache survives unload
and reload, so only the first use after a boot pays for it: prefill at 16k is
395-515 tok/s cold against 1070-1244 warm, and TTFT 31.8-41.5 s against
13.2-15.3 s. Nine reload-and-prefill cycles ran 26-31 s apart end to end. This is
inferred from wall-clock rather than instrumented, so treat ~31 s as an upper
bound on the warm cycle rather than a measurement.

### There is no truncation defect — that was the harness

The 1-3 token generations appear only inside oMLX's bench task. Driven through
LiteLLM at the same 16k prompt (`results/fn_truncation2.json`), `fn-agent`
returned **512, 512, 81, 512, 512, 512** bare and **512 x 6** instructed: 0 of 12
short. The control rules out prompt shape as well — the shipped 8-bit on the
*same* bare corpus returned 331-512, also never short. So Flash-Next generates
normally on the serving path, and §26's decode numbers were the only casualty.

That probe also produced the cleanest speed comparison of the exercise, on the
path a client actually uses, 16k prompt to 512 tokens:

```
fn-agent    12.2  12.2  12.4  12.5  12.6  12.9   median 12.45 s
qwen-agent  17.4  17.5  18.0                     median 17.50 s   -> 1.41x
```

Caveat: Flash-Next served alone and the 8-bit served with the lineup resident, so
the memory pressure differs and 1.41x is indicative rather than controlled.

### Why it cannot be wired: admitted, then unable to serve

§26 said "the weights of both models fit; the two KV caches do not". That is
right, and the mechanism is worse than it sounds. `is_pinned` and `ttl_seconds`
turn out to be ordinary per-model settings, so the lineup can be recomposed
without a restart — which makes the pairing §26 never tested cheap to test:

```
Qwen3.8-27B-8bit  28.29 GiB + Flash-Next 72.65 GiB = 101.50 GiB loaded
                                       vs 107.52 GiB cap -> 6.02 GiB headroom
```

Both models load. Then **all 8 alternating 16k calls were rejected, for both
models** (`results/fn_coexist.json`):

```
qwen-agent  HTTP 400  predicted peak ~98.99 GB (current 97.97 + KV 1.01)
fn-agent    HTTP 400  predicted peak ~98.42 GB (current 97.99 + KV 0.44)
```

Note what is rejected: **the shipped 8-bit stops working too.** Co-residency does
not make Flash-Next slow, it takes production down with it. And note what does
the rejecting — a predicted peak near 99 GB is comfortably under the 115.45 GB
admission cap, so the binding constraint is the dynamic ceiling of 91.29 recorded
in §18, not the cap that let the model load:

```
dynamic ceiling    91.29 GB   <- rejects a prefill
admission cap     115.45 GB   <- admits a load
```

**A model can be admitted and then be unable to do anything.** That gap is the
trap, and it is the general lesson rather than a Flash-Next one: any future
>70 GB candidate has to be tested by serving a realistic prompt while the lineup
is resident, not by checking that it loads.

With the lineup pinned as shipped, every `fn-agent` call returns HTTP 507 before
the model is even loaded — `projected memory 124.35GB would exceed the metal_cap
memory ceiling 107.52GB`. That message names `iogpu.wired_limit_mb` as the lever.
It is not one here: the kernel limit is currently `0` (default, 84% of 128 GiB),
admitting 124.35 GiB would need it at ~97% of RAM leaving ~3.6 GiB for macOS and
Docker, and it would not help anyway, because the ceiling that rejects the
prefill sits *below* the one the lever raises.

### Verdict

Retired, like Coder-Next before it, on residency rather than on quality — but on
a sharper measurement: not "swapping is expensive" (that argument was weaker than
§26 made it look) but "it cannot be co-resident at all, and trying breaks the
model that works". The `fn-*` profiles stay stored and served, wired to nothing.

Left open deliberately: the tool-call bias above. It is the only quality concern
this run raised, and it does not need settling while the arm is unwired.

### Instrument defects found by this run

- **`bench/throughput.py` reported a decode rate from a 2-token generation** with
  no warning, which is what corrupted §26. Now carries `gen_tokens`/`gen_short`
  in every row, a `gen tok` column, and an explicit SHORT GENERATION block.
  `bench/report.py` still reads `decode_tps` without consulting the flag.
- **`bench/flashnext_overnight.sh` phase 1 never compared anything.**
  `taskbench compare` takes labels and prefixes `task_` itself; the loop passed
  `task_coder`, so it looked for `task_task_coder.json` — and a `2>/dev/null`
  turned the `FileNotFoundError` into "(compare failed, skipped)". Both fixed.
  The measurement had been fine all along.
- **The thinking A/B summary asserted a cost regardless of direction**, printing
  "presence_penalty 1.5 costs -15% of decode (41.23 -> 47.35 tok/s)" for a 15%
  gain. Now branches on the sign and says so when the gap is inside noise.

---

## 28. The omlx.ai leaderboard is prior art, and it is mostly misconfigured

§11 records that oMLX uploads every accuracy suite of >=100 questions to
`omlx.ai` with no opt-out. The same board is readable, and it was worth reading
*before* spending GPU hours: it carries 7,228 intelligence results, 80 of them on
Flash-Next variants, including the exact checkpoint on this disk.

### It already answered the suite we were about to authorise

Backlog A6 held an ~8 h paired code suite pending authorisation, described as
confirmatory against the vendor's livecodebench v6 claim of 91.9 vs 90.3.
`Jundot/Qwen3.8-Flash-Next-oQ4e-mtp` is already on the board at **61.0%** on
livecodebench (100 questions), against this repo's 59.8% for the shipped 8-bit
(299/500, §13). The arithmetic kills the run outright:

```
299/500 vs 61/100            z = 0.22, p = 0.82
95% CI on 61/100             [51.4, 70.6]   -- 59.8 sits in the middle
min detectable gap, n=500    8.7 points
min detectable gap, n=1054   6.0 points     -- the ENTIRE dataset
n/arm for the vendor's 1.6   ~14,700
```

livecodebench has 1,054 questions. **The instrument's floor is ~6 points and the
effect is ~1.2.** No run of that suite at any sample size available could resolve
it, so the 8 h was not confirmatory, it was uninterpretable. This generalises:
before commissioning a suite, compute what it can detect. §13's -16.6 for
Coder-Next was detectable; most things are not.

### Treat published scores as configuration reports, not model scores

The same checkpoint appears at MMLU-Pro **82.8%** (n=1000) and **69.7%** (n=300)
on the same day. The 82.8 run has thinking on with a 8192-token budget; the 69.7
run has `reasoning_parser: qwen_3` where working runs use `qwen_3_5`, and it
completed 300 questions in 4.0 minutes — impossible for a 125B model unless the
think block was being graded as the answer. That is trap #2 from backlog A6
happening to somebody else, on an **M5 Max 40c / 128 GB / oMLX 0.6.4** — this
machine's exact specification.

Six Flash-Next runs on the board score <=1%, and their settings are diagnostic:

| what collapsed | config signature | reading |
|---|---|---|
| HumanEval 0.0, MBPP 0.0 | `temperature 0.1, top_k 100, repetition_penalty 1.1` | off-checkpoint sampling plus a penalty; `profiles/flashnext.py` already bans exactly this |
| ARC-C 0.0 | the only run in the set with `specprefill_enabled: true` | the same checkpoint scores 94.0 without it |
| LCB 1.0, MBPP 0.0, HumanEval 0.0 | `mtp_enabled: false`, otherwise sane | the uploader's MTP build scores 50.0/89.6 — a broken build, not a bad config |

So the board is useful as **negative** evidence — it shows which levers destroy a
model and confirms bans this repo arrived at independently — and close to useless
as a ranking. Cross-run comparison is confounded by acceleration: the 56.0%
livecodebench run had TurboQuant KV 4-bit *and* SpecPrefill on, both retired here.

### The performance board is contaminated by the §27 defect

Community M5 Max figures for this checkpoint read TG 53.3 at 1k rising to 71.7 at
64k and 70.1 at 128k. Decode rising with context is not a thing; it is the same
short-generation artifact §27 found locally. **Our own five 2-token runs are now
on that board too** — `bench/throughput.py` uploads, `success_count: 1`, with the
machine `owner_hash`. Nothing on the performance board should be cited, ours
included, without the completion-token count behind it, which the board does not
show.

---

## 29. The A7 reason arms: OptiQ is dominated, gemma-4 wins, and mathqa lied

Both candidates from §28's search, run against the shipped 8-bit at the
incumbent's exact parameters — three suites, 500 samples, batch 8, thinking off,
deterministic sampling (`bench/a7_reason.sh`). Power was computed from the
stored per-question results *before* running, per backlog D3: mmlu_pro runs ~10%
discordant at n=500, so the floor is **~2.9 points**.

```
                 mmlu_pro   gsm8k   mathqa      resident
27B 8-bit           64.0     93.6     43.8       28.20 GiB
27B oQ4e            61.4     92.4     41.6       15.88 GiB
27B RTN 4-bit       59.8     91.8     42.2
OptiQ  (measured)   61.2     92.0     43.0       19.00 GiB
gemma-4-31B         73.0     97.4     27.0       21.88 GiB
```

### OptiQ: A2b answered negatively a second time, and it is strictly dominated

The leaderboard's **65.3 did not replicate: we measure 61.2**, a 4.1-point
overstatement, and §28's rule about treating those rows as configuration reports
rather than model scores is now demonstrated rather than argued.

| | verdict |
|---|---|
| vs the 8-bit | pooled McNemar **p = 0.026**, the 8-bit is better (mmlu_pro p = 0.038) |
| vs oQ4e | pooled **p = 0.797**, indistinguishable (mmlu_pro 61.2 vs 61.4, p = 1.0) |

So KL-divergence-driven per-layer allocation lands exactly where `oq4e.py`'s
simpler allocation landed, and both sit below the 8-bit. A2b asked whether
data-driven allocation recovers the reasoning 4-bit costs. Asked twice now, with
two different methods, answered no twice.

**It is not merely equal to oQ4e, it is dominated by it**: same reasoning, 3.1 GiB
larger, and **no speculative decoding at all** against oQ4e's measured 1.73x
(§A7b — the MTPLX side-car cannot be activated because no published repo ships
`mtplx_runtime.json`). There is no configuration in which it is the better
choice. Retired.

### gemma-4-31B beats the shipped 8-bit on reasoning, at 78% of its size

```
mmlu_pro   73.0 vs 64.0   +9.0   p = 0.00006
gsm8k      97.4 vs 93.6   +3.8   p = 0.00088
```

Both clear the 2.9-point floor comfortably and both are the largest margins any
candidate has produced here. The claimed +11 on mmlu_pro came in at +9.0, which
is the first time a leaderboard figure in this repo has been close to right.

### mathqa 27.0 is a harness artifact, and the pooled p-value is a trap

Taken at face value the arms tie: **pooled McNemar p = 0.293, "no significant
difference"**, because mathqa's -16.8 cancels the two wins. That verdict is
wrong, and the way it is wrong is the point.

gemma answers mmlu_pro — the *same* multiple-choice "Answer:" format — in a
single character and wins it. On mathqa it writes ~342 characters of derivation
instead, and **210 of 500 responses yield no extractable letter** against the
8-bit's 2. Three hypotheses were tested and the first two were wrong:

1. *Mis-parsing.* Re-extracting a final letter with four patterns recovered
   **+0.2 points**. The letters genuinely are not in the stored text.
2. *A hard token cap.* Only 1% of responses sit within 10% of the longest, so
   there is no pile-up at a ceiling. This test was itself invalid — the stored
   `raw_response` is truncated for storage independently of the generation
   limit, so length statistics on stored text cannot reveal a cap.
3. *The generation budget.* Settled by re-running the failures through the
   serving path with 1500 tokens:

```
40 previously-WRONG mathqa items, re-run with a 1500-token budget
  now correct   27/40 = 68%
  still capped  13/40  -- every one finish_reason=length
  genuinely wrong 0/40
projected mathqa  27.0% -> ~76.3%   (8-bit 43.8%)
```

**Not one re-run item was wrong. They were correct or still truncated.** The
mathqa suite allots a budget sized for models that emit one letter; any model
that reasons before answering scores near zero on it. The 8-bit and OptiQ answer
in 1 character and never notice.

So the honest reading of the reason arms is **three wins for gemma-4-31B, not
two wins and a loss**, and `paired_compare.py`'s pooled figure should not be
read without its per-suite table — exactly the warning §27 added to taskbench's
`compare` for the same reason.

### What this does not settle

- **Instruction-following is a real flag.** "Answer with just the letter" was
  ignored on every mathqa item. That is harmless under a generous budget and
  directly harmful in an agent loop, where "do X and nothing else" is the normal
  shape of a request. It belongs in taskbench, not in an accuracy suite.
- The code and agentic axes are untouched. gemma has **no livecodebench figure
  anywhere**, including the leaderboard, and that is the suite that separated the
  last three candidates.
- Tokenizer cost still applies: ~9% denser on code (§A7), so equal context
  windows hold less repository.

### Instrument defect: the mathqa budget

`bench/accuracy.py` passes no `max_tokens`; the budget is the oMLX suite's own,
and it is sized for single-letter answers. Any candidate that reasons before
answering will be scored as catastrophically bad at mathqa, and the failure is
silent — the score simply comes back low. This is the third scoring instrument
in this repo found to mislead rather than fail (after `throughput.py` §27 and
taskbench's validity-only verifiers §27). Recorded in backlog D5.

---

## 30. gemma-4-31B wins both measured axes, and costs 1.8x the decode to do it

§29 left the code axis open, and noted that gemma-4-31B had **no livecodebench
figure anywhere**, including omlx.ai — the first candidate here with no prior art
to read. Measured at the incumbent's parameters (500 samples, batch 8, thinking
off, deterministic), against the shipped 8-bit:

```
                 ship 8-bit   gemma-4-31B    delta        p
humaneval           93.9         95.1        +1.2     0.754
mbpp                80.4         85.2        +4.8     0.00316
livecodebench       59.8         73.4       +13.6     0.0
                                  pooled McNemar p = 0.00000
```

Paired power was computed first, from the stored per-question results:
livecodebench runs 17-21% discordant at n=500, so the floor is ~3.6-4.1 points.
**+13.6 is more than three times that floor**, and it is the largest margin any
candidate has produced in this repo — in the opposite direction from all of
them. For scale, against the arms already retired:

```
                    livecodebench   vs the 8-bit
Coder-Next 6-bit        43.2           -16.6     retired (A1/§13)
oQ4e                    54.0            -5.8     fast tier only
OptiQ                    n/a             n/a     retired on reasoning (§29)
gemma-4-31B             73.4           +13.6
```

Against oQ4e it is +19.4 on livecodebench and +6.6 on mbpp.

**The D5 check is clean this time**, and it was run before the score was read:
`empty_pred` is 0 on all three suites against the 8-bit's 0, and median response
lengths are comparable (livecodebench 2,028 against 2,325). The §29 mathqa
signature is absent, so these numbers mean what they say.

### The cost: no MTP, and it shows

```
                 decode t/s              prefill t/s @16k   resident
                 1k     4k     16k
27B 8-bit      32.3   33.2   34.0             793           28.20 GiB
gemma-4-31B    21.3   20.6   18.5             617           21.88 GiB
```

All twelve rows report `gen_tokens: 128`, so §27's short-generation artifact is
excluded by the guard rather than by assumption.

gemma decodes at **0.54x the shipped 8-bit** at a 16k prompt. That is almost
exactly the 8-bit's own pre-MTP baseline of 17.3 (README), which is the point:
the 8-bit's 34.0 is 17.3 doubled by a drafter, and gemma has no MTP head and no
side-car, so it competes against an accelerated incumbent while unaccelerated.
Prefill is also 22% slower.

### So the trade is real, and it is not obviously worth taking

This is the first candidate that is genuinely better at the work and genuinely
slower at it. Every previous one was faster and worse (Coder-Next, oQ4e,
Flash-Next) or worse at everything (OptiQ), which made those decisions easy.

```
+13.6 livecodebench   +9.0 mmlu_pro   +4.8 mbpp   -6.3 GiB
                  against
          0.54x decode, 0.78x prefill
```

Nothing is wired on this evidence. What it justifies is measuring the two axes
still missing, not a swap:

- **Agentic.** taskbench cannot judge it yet: three of five categories are at
  100% for both existing arms (§27). This is now the blocking item, because the
  roles that would take gemma — `build`, `plan` — are agentic, and §14 already
  established livecodebench is blind to that axis.
- **Instruction-following**, flagged in §29: it ignored "Answer with just the
  letter" on every mathqa item. Under a generous budget that is harmless; in an
  agent loop, "do X and nothing else" is the normal shape of a request.
- **Context above 131072**, where its profiles are capped and nothing has run
  `bench/longctx_quality.py`.

A defensible intermediate reading, not yet acted on: quality-dominated and
latency-insensitive roles are exactly where a 0.54x decode is affordable and a
+13.6 is not, which is the same logic §15/§17 applied to best-of-N and the
inverse of the rule that gave oQ4e the speed-dominated work.

---

## 31. The agentic axis: gemma-4-31B is not worse, and taskbench has hit a structural limit

§30 made the agentic axis the blocking item for wiring gemma-4-31B. It ran.
Before it could, taskbench needed the rest of §27's work finished and two more
instrument bugs fixed — both of which would have decided the comparison wrongly.

### What was still broken in the instrument

- **Nothing verified that edges connect to real vertices.** A draw.io file could
  satisfy every count, geometry and style check with edges whose `source`/
  `target` named cells that do not exist, which renders as floating stubs.
- **`hdio-geom` asked for "non-overlapping" in its prompt and checked nothing.**
  Five vertices stacked at identical coordinates passed. Now verified by actual
  rectangle intersection.
- **Inconclusive counted as failure.** `compare` collapsed on `ok`, and a
  PlantUML `!include` the CDN would not serve has `ok=False`. On this very run
  that would have cost gemma four puml tasks for GitHub being unreachable.
  Inconclusive-for-either-arm tasks are now dropped from the pairing and
  reported.
- **Network flakiness scored as model failure.** The 8-bit's `hpuml-c4bound`
  returned exit_100 "No diagram found" in the same run where five sibling
  attempts were classified `network_include`. The CDN was intermittent and the
  reachability probe happened to succeed. The verifier now retries once before
  believing a failure on any artifact carrying a remote include.
- **The capability collapse saturates.** "Passed >=1 of N" reported 42/42 vs
  42/42 and zero discordant pairs while the 8-bit had two genuine failures gemma
  did not — because the 8-bit passed each on its other repeat. Per-attempt
  McNemar is now reported alongside, which keeps that signal and doubles the
  sample at repeats=2.

Task set grown 39 -> 44, with a CEILING tier requiring arithmetic on a tool
result (`cchain-compute`: the new value appears in neither prompt nor result and
must be computed), selection among distractors with exclusion rules
(`cchain-pick`), exact vertex counts with overlap and connectivity checks
(`cdio-grid`), and multi-constraint diagrams with forbidden constructs.

### The result

```
                              8-bit      gemma-4-31B
genuine task failures            2             0
network-inconclusive             2             4
per-attempt, 84 judged        0 to A        2 to B      McNemar p = 0.50
capability (>=1 of 2)          42/42        42/42
reliability (per attempt)      84/88        84/88   = 95% both
```

**gemma is not worse on the agentic axis, and every attempt that differed went
its way — 2 to 0 — but two flips is not significance.** Six one-way flips would
be needed. The honest verdict is "no regression detected, weak directional
evidence in gemma's favour", and it must not be reported as equivalence.

### §29's instruction-following flag does not reproduce

The concern from §29 was that gemma ignored "Answer with just the letter" on
every mathqa item, which would be dangerous in an agent loop. Against the five
tasks built for exactly that, gemma scored **2/2 on all five**, including the two
where the correct action is to call no tool at all:

```
bpy-nosort         forbids sorted()/sort()/heapq/Counter        PP
bpuml-forbid       forbids autonumber/skinparam/hex colours     PP
cpuml-multi        6 positive constraints + 2 forbidden         PP   (8-bit: FP)
bchain-contradict  must NOT edit when the premise is false      PP
chain-noresult     must NOT edit on an empty search             PP
```

So the mathqa behaviour was specific to a *format* instruction under a *starved*
budget (§29's ~150-token cap), not a general disregard for constraints. The
agentic risk that flag implied is not supported.

### The structural limit, stated plainly

Both arms sit at **95% per attempt** after three rounds of deliberate
hardening. Thirteen tasks that literally could not fail have been fixed, counts
and geometry and connectivity and negative constraints are all checked, and two
strong models still pass essentially everything.

That is not a verifier problem any more. **Single-turn, single-artifact tasks are
saturated at this model size**, and no amount of adding more of them will
resolve a 5-point difference. What would discriminate is a different shape
entirely: multi-step episodes against a real workspace, where the model must
sequence several tool calls, recover from a failed one, and be graded on the
end state of the filesystem rather than on one response. That is a different
harness, not more tasks, and it is the honest prerequisite for ever settling an
agentic comparison here.

Until then the agentic axis can detect a Coder-Next-sized regression and cannot
detect a subtle difference — which is enough to clear gemma-4-31B of harm, and
not enough to credit it with a win.

---

## 32. gemma-4-31B's context ceiling is 65k, so it is an addition and not a replacement

§31 left long context as the last unmeasured axis, and it was chosen next for a
specific reason: it decides whether gemma **replaces** the shipped 8-bit or is
**added** to it, and that decides whether the lineup still fits.

### It fails above 65k, on memory, with the machine to itself

`bench/longctx_quality.py` at B2's parameters — five depths plus multi-hop,
65k/131k/262k:

```
 65536   6/6 PASS   all depths + multi-hop, 65,215 verified prompt tokens
131072   FAIL       peak ~98.49 GB = weights 23.05 + KV 20.76 + transient 54.6
262144   FAIL       peak ~118.41 GB, KV+SDPA 95.33 GB, cap 102.14 GB
```

**The killer is the single-chunk prefill transient (~54.6 GB), not the KV.** At
131k the KV is a modest 20.76 GB; the attention workspace for one prefill chunk
is two and a half times that. The shipped 8-bit serves 131k routinely and was
verified to 261,826 tokens (B2), so this is a property of gemma-4's attention in
this runtime, not of the machine.

Ruled out first, because two confounds would each have produced this result:

- **The lineup being resident.** First attempt failed all 15 retrievals with the
  lineup up. Re-run with gemma alone (pool 21.88 GiB) — still fails.
- **The profile declaring more than the request.** Run 1 refused 65k, 131k and
  262k with byte-identical numbers, which proves the metal_cap guard sizes
  against `max_context_window` rather than the actual prompt. So 131k was
  re-tested through `g4-code`, which declares exactly 131072. Still fails.

**65536 is what was verified, not the ceiling.** The true limit is somewhere in
(65536, 131072] and nothing has bisected it. The profiles are set to the verified
figure, because a cap that promises what the guard will refuse is worse than a
conservative one. The `g4-long` verification profile was deleted rather than
lowered, as its own docstring instructed: a `g4-long` serving 65k under a name
promising 262k is the trap §26 was written about.

### Which makes it additive — and the additive lineup works

gemma cannot take `qwen-long`'s work, so the 8-bit stays resident regardless.
That was the branch worth testing, and it passes:

```
lineup 8-bit + oQ4e + Coder-3B + gemma   =  70.53 GiB resident, 4 models
9 alternating 16k calls across three arms:  9/9 served, 0 evictions
warm wall-clock, 16k prompt -> 256 tokens:
    oq-agent   7.3 s      qwen-code  10.9 s      g4-code  17.6 s
```

Adding gemma costs 21.88 GiB and takes the lineup from 47.19 to 70.53 GiB,
leaving ~14 GiB under the dynamic prefill ceiling. That is enough for 16k work
across all four and is **not** enough to also run the 8-bit at 262k while gemma
is loaded — `qwen-long` at full context will require gemma to be evicted, which
oMLX's pool does on its own.

### The complete picture, four axes measured

```
reason        mmlu_pro +9.0 (p=6e-05), gsm8k +3.8 (p=0.0009)      WIN
code          livecodebench +13.6 (p=0.0), mbpp +4.8 (p=0.003)    WIN
agentic       no regression; 2-0 on attempts, p=0.50              CLEARED
long context  fails above 65k; 8-bit keeps qwen-long              LOSS
speed         0.54x decode; 17.6 s vs 10.9 s on a 16k call        LOSS
memory        additive, 70.53 GiB lineup, coexists and serves     OK
```

So it is not a replacement for the shipped 8-bit under any reading — the 8-bit
holds long context on its own. What it is, is a **quality tier for
short-context, latency-insensitive work**, which is a narrower claim than §30
looked like it was heading toward and is the one the evidence actually supports.

The roles that fit that description are `plan` (architecture and reasoning,
rarely latency-critical, rarely above 65k) and arguably `build`. `build` is the
harder call: +13.6 livecodebench is the largest quality gain available here, and
against it sit a 65k context ceiling on whole-repo work and 1.6x the wall-clock
on every call. That is a workload judgement, not a measurement, and it is
recorded as one.

---

## 33. Wiring `plan`, and why gemma's context ceiling is probabilistic rather than a number

§32 set gemma-4-31B's cap at the verified 65,536 and left the true limit
unbisected in (65536, 131072]. That mattered for one decision only — whether
`build`, which works on whole repositories, could move — so it was worth
finding. `bench/ctx_bisect.py` bisects on the prefill guard, which is
deterministic, so four probes settle it.

```
 98304 -> ADMIT (98,215 actual)      122880 -> ADMIT (121,177)
114688 -> ADMIT (112,755)            126976 -> ADMIT (124,938)
                                     131072 -> REFUSE
```

So the guard admits ~127k, nearly twice §32's verified figure. But **admitting
is not attending**, and it is also not *reliably* admitting:

```
retrieval at ~116,746 tokens
  depth 0.95   PASS   (needle recovered exactly)
  depth 0.50   REFUSED by the guard
  depth 0.05   malformed response
```

The model *can* retrieve at 116k — depth 0.95 returned the needle exactly. What
it cannot do is be *admitted* there dependably: the same length passes or is
refused depending on residual KV and memory state, because at ~116k the
predicted peak sits within a few GB of the ceiling. 65,536 was 6/6 in §32;
~116k is 1/3.

**So the usable ceiling is not a number, it is a probability that degrades above
~65k.** The cap stays at 65,536, which is the largest length that has never been
refused. This is the honest answer to the `build` question: gemma cannot hold
whole-repo contexts dependably, and a cap that works two times in three is worse
operationally than a smaller one that always works.

### Two self-inflicted measurement errors, both worth recording

The first bisection run reported retrieval **0/3** and would have concluded
"gemma cannot attend at 116k", which is false. Both causes were mine:

1. The retrieval check set the profile window to `lo + 4096` — a length already
   *known* to be refused — then sized the prompt at the boundary and made it
   longer by inserting the needle. Every probe tipped over the guard. **A
   measurement taken at the exact edge of a cliff measures the cliff.**
2. The probe's own KV was still resident when the next call was made, so the
   guard counted memory that was about to be freed. The model is now unloaded
   and reloaded between the bisection and the retrieval check.

The same residue produced a spurious wiring failure: `bench/verify_wiring.py`
reported `plan` as unreachable when run immediately after a 126k prefill,
because a two-word prompt was refused on memory. It passed before and after.
Sequencing a measurement after a memory-heavy one is itself a confound.

### The wiring

`plan` now runs on `omlx/gemma-4-31B-it-qat-OptiQ-4bit:g4-reason`. It is the one
role the evidence unambiguously supports: reasoning-dominated (+9.0 mmlu_pro,
p = 0.00006), not latency-critical (0.54x decode is invisible on a planning
turn), and rarely above 65k. `build`, `qwen-long` and every other agent are
unchanged.

`bench/verify_wiring.py` checks what B1b found the repo had never checked: that
a served profile is actually reachable, answers, does not leak monologue into
content, and does not declare a context larger than the model can serve. It
caught two of its own false positives first — reasoning profiles return empty
`content` with the text in `reasoning_content`, so a 24-token probe scored both
`plan` and the untouched `speckit` as "does not answer".

Vision was verified rather than inherited: gemma named both colours in a
synthetic two-tone PNG, so the config declares image input truthfully.

---

## 34. The qwen3_5_moe 35B-A3B class: fast, it coexists, and it cannot code

Backlog A9. Two `qwen3_5_moe` 35B-A3B MoEs were fetched to test whether a
~3.4B-active sparse model could take a tier off the dense 27B — gemma-class
quality without gemma's 0.54x decode. `oMLX` ships `qwen3_5_moe.py`, so unlike
Flash-Next (§26) they load on the native runtime with no vendored patch.

    Tiel-Coder-35B-A3B-MLX-oQ4e     mixed-up eff 6.9   20.6 GB resident   no MTP head
    Qwen3.8-35B-A3B-Distill-oQ8     uniform 8-bit      38.6 GB resident   trained MTP head

Both were downloaded classified-first (`bench/quant_id.py`, no weights until the
config confirmed arch + build), and both went through the full D3 gate. **They are
the first candidates since gemma to pass the coexistence check that closed
Flash-Next**: Tiel served a 16k prompt with the pinned lineup resident at pool
69.3 GB, Novaeon at 87.3 GB, both coherent, no eviction. Residency was never the
problem here. Quality was.

### Speed is real, and Novaeon's MTP is genuinely engaged

`bench/moe_decode_ab.py`, 5 repeats, 400 tokens:

| model | decode | note |
|---|---|---|
| Tiel-Coder (no MTP head) | 113.9 tok/s | ~3.3x the dense 27B-8bit's 34 |
| Novaeon, MTP **on** | **150.5** tok/s | trained `nextn` head |
| Novaeon, MTP off | 90.9 tok/s | |

**MTP is worth 1.66x on Novaeon** (delta 59.6 vs noise 29.3), which refutes
CyberTiel's card claim that the head is a no-op on 3B-active MoEs — that holds
for a *grafted* head, not a *trained* one. So on this axis the class delivers
exactly what it advertises: 3-4x the dense tier's decode.

### The toolcall trap: 24/24 that meant nothing

`bench/taskbench.py --only toolcall`, 3 repeats (the one category §31 found still
discriminates; drawio/puml/pycode are saturated): **Tiel 24/24, Novaeon 23/24**.
Tiel even passed `htool-escape`, which Flash-Next failed 3/3 (§27). On this
evidence Tiel looked like a strong agentic coder.

It is not. **`toolcall` measures whether the model emits well-formed tool calls,
not whether it writes correct code**, and the two came apart completely here. A
candidate can format every call perfectly and still not solve the problem the
call is for. taskbench alone would have advanced a model that the code suite then
buried — the concrete cost of §31's saturation.

### Code accuracy, which is the tier's actual job, and it is decisive

`bench/accuracy.py code`, thinking off, deterministic, paired McNemar against the
arms already on disk (Tiel @500, Novaeon @250):

| | humaneval | mbpp | **livecodebench** |
|---|---|---|---|
| **Tiel-Coder** | 82.3 | 73.8 | **24.2** |
| **Novaeon distill** | 90.2 | 77.6 | **41.6** |
| Qwen3.8-27B-8bit (ship) | 93.9 | 80.4 | 59.8 |
| gemma-4-31B | 95.1 | 85.2 | 73.4 |
| Qwen3.8-27B-oQ4e (fast tier) | 94.5 | 78.6 | 54.0 |

**Tiel-Coder loses to all three incumbents, pooled p ≈ 0 in every case** — 261
question-flips to the 8-bit against 31 to Tiel. Its livecodebench 24.2 is below
Coder-Next's 43.2, the score that retired *that* model in §13. The "Coder" name
and the 24/24 toolcall did not survive contact with actual code generation.

**Novaeon is markedly better than Tiel and still not close** — livecodebench 41.6,
losing to the 8-bit pooled p = 0.00003 and to gemma p ≈ 0. As a distill of the
Qwen3.8 family it cannot exceed the 8-bit it is distilled from, and at 8-bit
uniform RTN (not the mixed-up build the "oQ8" name implies) it does not match it.

### Disposition

**Both retired; the lineup is unchanged.** The `qwen3_5_moe` 35B-A3B class buys
3-4x decode at a code-quality cost this stack will not pay: the roles these would
serve (`build`, the fast tier) are exactly the ones whose correctness is not
independently checked, which is where the 8-bit and oQ4e were deliberately kept.

The §14 caveat is acknowledged and does not save Tiel: livecodebench is blind to
the *agentic* SWE-bench ability its card actually claims, so this run does not
falsify that claim. But a 24.2 static-code floor is so far under the lineup that
wiring a real agentic harness to chase it is not worth the GPU time — the same
cost/detectability logic D3 step 2 applies to every candidate.

**Two lessons carried forward.** (1) A coexistence pass is necessary, not
sufficient — Flash-Next failed residency, these failed the axis residency was
gating for. (2) Never advance on `toolcall` alone; it measures call formatting,
and formatting is not correctness.

---

## 35. The prefill guard refused an 18-token prompt: `current` and the transient are stale, not large

opencode surfaced this on 2026-09-22 against `Qwen3.8-27B-8bit`:

```
Prefill context too large for available memory (preflight safety guard,
kv_len=13545, min_chunk=32): predicted peak would require ~101.05 GB
(current 64.64 GB + KV 995.44 MB + min-chunk transient 35.43 GB) but
prefill safety cap is 96.77 GB (90% of metal_cap ceiling 107.52 GB).
```

The message names three remedies — raise `iogpu.wired_limit_mb`, set the tier to
aggressive, compact the context. **All three are beside the point, and the third
cannot work at all.**

### The refusal is size-invariant, which falsifies the advice

`~/.omlx/logs/server.log`, five rejections over ten minutes:

| time | prompt | kv_len | min_chunk | current | KV | transient |
|---|---|---|---|---|---|---|
| 22:16:10 | 13,578 | 13,545 | 32 | 64.64 | 995.44 MB | 35.43 |
| 22:16:23 | 20 | 0 | 19 | 64.64 | 148 MB | 35.43 |
| 22:21:24 | 20 | 0 | 19 | 64.64 | 148 MB | 35.43 |
| 22:25:40 | 13,578 | 13,545 | 32 | 64.64 | 995.44 MB | 35.43 |
| 22:26:25 | 18 | 0 | 17 | 64.64 | 148 MB | 35.43 |
| 22:32:25 | 20 | 0 | 19 | 64.64 | 148 MB | 35.43 |

**An 18-token prompt was refused on memory.** Only the KV column tracks the
request; it is ~1 GB at 13.5k, exactly §18's ~45 KB/token + 0.42 GB. Compacting
the context can only ever return that ~1 GB against a 4.28 GB shortfall, so
"reduce context length" is unactionable here — and this repo's own §33 already
recorded a two-word prompt being refused after a 126k prefill, which is the same
bug seen from the other end and was misread there as residual KV.

`current` and `transient` are **byte-identical across all six**, spanning a
3.05 GB unload (`freed=3.05GB ... active_memory: 48.92GB (settled)` at 22:25:09),
a 3.10 GB load, and a *successful* 70,028-token prefill on the same model at
22:29:22. Values that survive all three are not measurements.

### Where the two frozen numbers come from

Read from `/Applications/oMLX.app/Contents/Resources/omlx`:

- **`current`** — `Scheduler._current_usage_bytes(refresh_mlx_active=False)`
  (scheduler.py:4427). Its docstring is explicit: "Event-loop callers such as
  early preflight use the cached executor sample". The preflight gate runs on the
  event loop, so it reads `_last_mlx_active_memory_bytes`, which is only rewritten
  by a scheduler step on the MLX thread. No step runs while every request is
  rejected, so the sample never moves.
- **`transient`** — `_predicted_chunk_transient` (scheduler.py:3853) returns
  `max(base_prediction, static_per_token * n * 1.3 + recent_reclaim)`.
  `recent_reclaim_bytes` is memory a preceding preflight LRU eviction released,
  charged back on the theory it will be re-allocated. It is the only
  size-invariant term, which is why 35.43 GB is identical at `min_chunk=32` and
  `min_chunk=17`. It cannot be the observed-max term: that is clamped at
  `_OBSERVED_MAX_CLAMP_BYTES = 4 GiB` (prefill_transient_tracker.py:34).
  `recent_reclaim_bytes` is zeroed only by `PrefillTransientTracker.update()`,
  i.e. on completion of a prefill chunk.

**So it is a deadlock, not a shortage.** A preflight eviction frees ~35 GB,
books it as a predicted re-allocation, and that charge is cleared only by a
prefill that the charge itself forbids. `Running preflight LRU eviction for
request preflight` precedes every one of the six rejections.

### What clears it

Unload and reload the affected model — it rebuilds the Scheduler, so both the
cached sample and the tracker start empty:

```
unload Qwen3.8-27B-8bit   0.6 s
load                      7.3 s
15-token prompt      ADMITTED  0.8 s      <- the shape refused at 22:32
18,416-token prompt  ADMITTED  24.0 s     <- 768 tok/s, the healthy rate
```

Zero rejections since. A server restart works too; nothing else measured here
does. Note what does **not** clear it: a successful large prefill on the same
model. The 70,028-token probe at 22:29:22 went through (199.6 s, 350 tok/s —
depressed by 5.5 GB of swap) and the very next small request was still refused
at 22:32:25, because that probe entered through a path that bypassed the frozen
preflight gate rather than refreshing it.

Raising `iogpu.wired_limit_mb` or the tier would also have papered over it by
lifting the cap above the phantom 101.05 GB. Both were declined as the fix: the
arithmetic being defended against is fiction, and permanently widening a safety
margin to accommodate a stale reading trades a recoverable 400 for the Metal OOM
crash the guard exists to prevent. Tier fractions, for the record
(`process_memory_enforcer.py:_PREFILL_ABORT_MARGIN`): safe 0.90, **balanced
0.90**, aggressive 0.95, custom 0.95. §18 and §32 both quote 102.14 GB as the
operative ceiling, which is 0.95 x 107.52 — the aggressive/custom figure, not
balanced's 96.77. Those sections were measured under a different tier setting
than the one shipping today.

### `ttl_seconds` is inert on a pinned model

§26 called the unset per-model TTL "the cheapest lever, and it is unset". It is
cheaper than that: on this lineup it does nothing. `engine_pool.py:3120` skips
`entry.is_pinned` before it ever reads `settings.ttl_seconds`. Measured:
`Qwen2.5-Coder-3B-8bit`, `ttl_seconds=60`, pinned, zero traffic, still resident
at t+240 s.

TTLs are now set — Coder-3B 600 s, oQ4e 1800 s, 8-bit 3600 s — and every one of
them is a no-op until the model it is on is unpinned. Unpinning is a routing
decision, not a memory setting (§26): it elects the eviction victim. Left as is.

### Carried forward

- **Read the log, not the 400.** The `admission terms:` line
  (scheduler.py:4116) breaks the bound into `predicted_transient` /
  `observed_max_bytes` / `ane_prefill_transient_bytes`. It only fires on the
  *mid-prefill* path; the preflight path that produced these six rejections
  prints no such breakdown, which is why the terms had to be re-derived from
  source here.
- **`bench/memory_probe.py` cannot parse this message.** Its regex wants
  `ceiling is ([\d.]+) GB`; the current format says `ceiling 107.52 GB`. It has
  been silently unable to read a refusal since the message changed.
- **A repeated guard refusal at a small prompt means reload the model.** It does
  not mean the lineup is too big. That distinction is worth a doctor check.

---

## 36. The 2026-09-30 refusal was pinning, not memory: models unpinned, replayed, guard shim withdrawn

opencode surfaced this on 2026-09-30 20:45:59 against `Qwen3.8-27B-oQ4e-mtp`:

```
Prefill would require ~82.66 GB peak (current 52.33 GB + KV+SDPA 30.32 GB) but
dynamic ceiling is 63.52 GB ... static cap is 122.00 GB but only 14.53 GB is
reclaimable right now
```

### What the log says

The 8-bit model had just finished a 34,323-token turn. Within a second the
footprint fell 96.8 → 52.3 GB, the ceiling followed it to 66.9 GB, and the oQ4e
request needed 30.32 GB against ~11.2 GB of headroom. The guard did try the
right remedy, twice:

```
[prefill-eviction] ... action=release_ane outcome=insufficient_headroom
  model_memory=48.65GB predicted=30.32GB target=63.52GB evicted=0
No idle model evicted for request ...; scheduler will fall back to throttling
```

`evicted=0` with two 27B models resident and one of them idle: the idle one,
`Qwen3.8-27B-8bit`, was **pinned**, and the LRU victim search skips
`is_pinned` (still true in 0.7.0). Evicting it frees ~28.8 GB, which puts the
request at ~53.8 GB against 63.52 GB. It would have fit. §35's "Left as is" on
pinning is reversed here: a pin does not just elect the eviction victim (§26),
it removes the only remedy the prefill guard has.

### Change (2026-10-03, oMLX 0.7.0)

All three pins removed (`Qwen3.8-27B-8bit`, `Qwen3.8-27B-oQ4e-mtp`,
`Qwen2.5-Coder-3B-8bit`) via `PUT /admin/api/models/{id}/settings
{"is_pinned": false}`. Every model now loads on request and unloads after its
TTL. The TTLs §35 set are live, and Coder-3B unloaded on its own at 07:59:01.

### Replay (`bench/replay_prefill_eviction.py`, through LiteLLM :4000)

| run | sequence | result |
|---|---|---|
| natural | 30,068-token 8-bit turn, then a fresh 117,800-token oQ4e request | 200, no headroom shortfall, no eviction needed |
| `--forced` | custom ceiling 56.78 GiB (soft 51.1, hard 55.1) with both 27Bs resident, then the same oQ4e request | 8-bit evicted, request completed |

The forced run's log is the proof the natural run could not give:

```
Evicting idle model 'Qwen3.8-27B-8bit' for prefill headroom on
  'Qwen3.8-27B-oQ4e-mtp' (request=preflight, projected=57.02GB > target=53.94GB)
[prefill-eviction] ... action=evict_model outcome=headroom_available evicted=1
Chat completion: model=Qwen3.8-27B-oQ4e-mtp ... prompt: 117801, finish_reason=stop
```

Why the natural run had room: 0.7.0 reserved **+7.08 GB** for 117,800 fresh
tokens (16 full-attention layers, ~64 KiB/token of KV). rc1 charged 30.32 GB for
a request of similar size. The ceiling read 91.5 GB rather than 63.52 GB.

One side effect of the forced run: the llm-router canary reloaded the 8-bit
model mid-prefill. Under a 56.78 GiB ceiling that throttled the oQ4e prefill to
32-token chunks until soft pressure evicted the 8-bit again. This is an
artifact of the artificial ceiling, but it is what alternating 27B traffic looks
like when memory is genuinely tight.

### Withdrawn: feature 003 (guard-400 mediation)

The loopback shim on :4001 and the banner watcher translated a refusal that
the server avoids by itself once nothing is pinned. Spec, code, tests and
fixtures are deleted. No client ever pointed at :4001.

### Routing: all traffic goes through LiteLLM

opencode's `omlx` provider pointed straight at oMLX on :8000, so every opencode
agent bypassed LiteLLM. It now uses `http://127.0.0.1:4000/v1` with
`LITELLM_LOCAL_KEY`. LiteLLM serves the same eight model names, so no agent
definitions changed. The llm-router on Proxmox already reaches oMLX only via
this LiteLLM.

### Carried forward

- **Do not pin.** If a refusal recurs, read `[prefill-eviction] ... evicted=`
  first. `evicted=0` while an idle model is resident means something is pinned.
- **Model-split cost is unmeasured.** With nothing pinned, alternating build
  (8-bit) and agent (oQ4e) traffic can evict and reload a 27B model. Measure
  reload time before deciding whether to consolidate.

---

## 37. Thermal emergencies: the Mac went to sleep mid-inference, eight times

On 2026-10-10 the Mac ended up in a thermal-emergency loop. At 18:09 it was
forced off with the power button, and the kernel booted again at 18:26:07. It
left no panic report. `pmset -g log` shows **eight** incidents since
2026-10-03, and they all follow one sequence.

### The sequence

1. **The Mac enters Idle Sleep or Clamshell Sleep while oMLX is still
   decoding.** An agent loop sends requests through the API, and macOS does
   not treat those as user activity. oMLX holds no `PreventSystemSleep`
   assertion while it decodes. So once the display has slept, the 15-minute AC
   `sleep` timer runs out mid-generation.
2. **Inference continues anyway.** On 2026-10-10 the 8-bit model completed a
   turn every 1–3 minutes from 17:52:35 ("Idle Sleep") to 18:08:55. Prompts
   grew from 70,968 to 90,468 tokens and every turn ended in
   `finish_reason=tool_calls`. The log stops mid-prefill at 18:08:55.
3. **Wake-on-network keeps it going.** Every wake reads
   `DarkWake ... E_RX_IP_PACKET`. With `womp 1` on AC, each incoming request
   wakes the machine into DarkWake, where it keeps serving.
4. **The SMC raises a thermal emergency and powerd forces sleep. The next
   packet wakes it again.** `Dark Wake Count in this sleep cycle: 133`. Starting
   at 13:17 the same day, there were 226 thermal events in five hours.

| incident | entered via | oMLX completions in the 30 min before |
|---|---|---|
| 2026-10-03 21:51 | Idle → Maintenance Sleep | yes, every ~5 min |
| 2026-10-04 11:27 | — | yes, continuous |
| 2026-10-05 11:10–11:22 | **Clamshell Sleep, on battery at 47%** | yes |
| 2026-10-07 04:49–06:05 | Idle Sleep (overnight run) | yes, continuous |
| 2026-10-08 20:49–21:16 | **Clamshell Sleep**, AC | yes |
| 2026-10-09 17:05–17:51 | Idle Sleep | yes |
| 2026-10-10 13:17–13:25 | Idle Sleep | yes, 2 per 5 min (canary cadence) |
| 2026-10-10 17:53–18:09 | Idle Sleep → forced power-off | yes, agentic loop at 70–90k ctx |

Each incident begins seconds to minutes after a sleep entry, and in every one
oMLX was serving at that moment. The machine was not running hot while awake.
Sleeping with GPU work still in flight is what triggered the emergencies.

### Why sleep was on at all

When the AC sleep setting was checked after the incident it read **15**, with
display sleep at 10. `/Library/Preferences/com.apple.PowerManagement.plist`
was last written at **13:16 on 2026-10-10**, one minute before that day's
first incident. The setting was believed to be `0`. The values 15/10 are
exactly what the CIS Level 2 benchmark requires on Apple silicon, and this Mac
runs EY's Intune CIS profiles. There is no MDM energy-saver payload, and
Intune does not log script contents, so the evidence connects a revert to that
timestamp but does not prove Intune caused it. Treat AC sleep as a setting
**that can revert without anyone touching it**. Since §24, this is the expected
failure mode: something configured that silently stops being in effect.

### Attempts

| # | date | change | outcome |
|---|---|---|---|
| 1 | before 2026-10-10 | `pmset -c sleep 0` (believed set) | found at 15 after the 18:09 incident; reverted or never applied |
| 2 | 2026-10-10 19:53 | `sudo pmset -c sleep 0 womp 0` | **reverted** 2026-10-11 13:16:54: AC `sleep` back to 15, `womp` stayed 0. The plist was written at 13:16 on both days, the timer went back to exactly the CIS value, and an Intune script ended at 13:16:49.98 (pid 44039, exit 0). That is consistent with an Intune CIS remediation that enforces only the sleep timer, but not proven, because Intune does not log script contents. A global setting does not hold on this Mac |
| 3 | 2026-10-11 19:19 | `proxy/inference_awake.py` as LaunchAgent `$LAUNCHD_PREFIX.inference-awake`: polls oMLX `/api/status` every 5 s and holds `caffeinate -i -s` while `active + waiting + models_loading > 0`, releasing 180 s after the last busy poll | live-verified: the assertion is held from model load through generation and released after the linger. An idle Mac still sleeps per policy, so this does not fight the MDM. Not covered: Clamshell Sleep. Doctor `host` layer now checks that this agent is running instead of `sleep=0` (fault-injected) |

### Gaps no setting closes

- **Clamshell Sleep overrides `sleep 0`.** The 2026-10-05 and 2026-10-08
  incidents both began with the lid closed, and the first was on battery.
  `doctor.py` now checks the lid state and the power source. It warns and
  blocks nothing.
- **The 2026-10-10 17:00 agent loop had no end condition apart from the
  model.** It ran for over an hour with growing context. The source client is
  unknown: LiteLLM was not running after the reboot, so its logs were gone.

### Carried forward

- `bench/doctor.py` has a `host` layer: AC sleep is 0, AC womp is 0, the Mac
  is on AC, the lid is open, and there was no thermal event in 24h. Run it
  before any long or unattended job.
- A thermal-emergency loop **looks like a hang, not a crash**. Find it with
  `pmset -g log | rg Thermal`. `pmset -g therm` reports nothing, because it
  covers the CPU power status, not SMC sleep emergencies.
- Attempt 2 reverted, so the fix now comes from a running process rather than
  global config (attempt 3). It holds the assertion only during inference, not
  whenever oMLX runs, so the CIS sleep policy still applies to an idle Mac.

---

## 38. Ordered LLM work: neither gateway queues across models, so a 150-line proxy does

The goal: one inference request at a time across the whole machine, requests
for the resident model grouped together, models never pinned, and a 429 when a
request waits too long. Three candidates were checked against their source,
not just their docs.

### What exists

| | queues? | across models? | finding |
|---|---|---|---|
| **oMLX 0.7.0** | yes, FCFS per engine (`scheduler.py:2042`), 503 at `max(max_num_seqs*4, 32)` | **no** | A busy engine cannot be evicted (`in_use`), so a request for another model loads beside it and **both decode at once**. With no room it raises `InsufficientMemoryError` and does not wait. |
| **LiteLLM 1.100.1 (running)** | `max_parallel_requests` queued | no, per deployment | Before 1.101 the slot was released before a stream finished. |
| **LiteLLM ≥ 1.103 (latest 1.105.0)** | **no**: "reject with 429 when a deployment's max_parallel_requests slots are all in use" | no | Upgrading *removes* the implicit queue. The beta `/queue/chat/completions` scheduler keys its heap by `model_name` (`scheduler.py:45`) and only gates when no deployment is healthy. 1.104 "group-scoped priority routing" is routing, not queueing. |
| **Bifrost (transports v2.2.6)** | yes, a per-provider buffered channel, blocking by default | yes, if every model sits under one provider | **For streams the worker is released when the response headers arrive.** `requestWorker` hands the stream channel to the caller and loops, while the body is read in a goroutine (`providers/openai/openai.go:1376`). oMLX sends headers in about 0.1 s, so `concurrency: 1` serialises nothing that streams, and opencode always streams. |
| llama-swap | FIFO scheduler with swap groups | yes | Owns one process per model; oMLX is one process for all of them. Per-model `concurrencyLimit` returns 429 rather than queueing. |

### What was built: `proxy/llm_queue.py`

A stdlib asyncio proxy for `client -> LiteLLM :4000 -> llm_queue :8001 -> oMLX :8000`.
It holds one global slot **until the last byte of the response is relayed**
(the streaming gap that Bifrost has), groups by base model id (the `:profile`
suffix is stripped), and gives the slot to the model with the oldest waiting
request after `--max-streak` (4) consecutive grants. It returns 429 +
`Retry-After` when the queue is full or a request has waited `--max-wait`
(600 s). It never loads, unloads or pins anything; oMLX's LRU and TTLs still do.

Tests: `tests/test_llm_queue.py`, 15 cases (scheduler policy plus HTTP against
a fake streaming upstream). Fault injection: a gate that admits everything
fails the serialisation test (`3 != 1 overlapping generations`).

Live, against oMLX, six streaming requests arriving A,B,A,B,A,B 0.1 s apart
(A = oQ4e, B = Coder-3B, 48 tokens each):

| path | order run | overlapping | model switches | wall |
|---|---|---|---|---|
| direct :8000 | all six at once | **yes**, both models decoding together | — | 7.5 s |
| via :8001 | A,A,A,B,B,B | no | 1 | 7.4 s |

The same wall time at this size: batching bought nothing that the grouping
did not give back.

**LiteLLM will not cool down on the queue's 429.** For a single-deployment
group, the 429 branch is skipped. The remaining trigger is 100% failures at ≥
`SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD` (1000/min). With `num_retries: 0`
the 429 goes straight to the caller.

### Not yet done (deliberately)

The proxy is **not in the live path**. Wiring it means changing
`OMLX_BASE_URL` to `http://host.docker.internal:8001/v1` in
`proxy/docker-compose.yml` and running the proxy as a LaunchAgent. Open
questions before that: how opencode and the llm-router canary handle a 429
(retry with backoff, or fail the turn); whether 600 s suits a 3-minute
agentic turn sitting behind a long batch job; and the extra cost of
`Connection: close` per request (not measured).

### Soak setup and client 429 behaviour (2026-10-11)

**Running:** the LaunchAgent `$LAUNCHD_PREFIX.llm-queue` (`proxy/launchd/`) serves
127.0.0.1:8001. LiteLLM still points at :8000. Events go to
`~/Library/Logs/llm_queue/events.jsonl`, one line per gated request with
`model, outcome, status, waited_s, held_s, depth`. The running log is
`~/Library/Logs/llm_queue/llm_queue.log`. The repeat live run under launchd
was again A,A,A,B,B,B with no overlap.

**Monitoring:** `GET :8001/metrics` serves Prometheus text:
`llm_queue_depth`, `llm_queue_busy`, `llm_queue_running{model}`,
`llm_queue_oldest_wait_seconds`, `llm_queue_switches_total`,
`llm_queue_requests_total{model,outcome}`, the histogram
`llm_queue_wait_seconds{model}` and `llm_queue_held_seconds{model}`. Every
relayed response carries `X-LLM-Queue-Wait-Ms` / `X-LLM-Queue-Depth`. LiteLLM
keeps upstream headers as `llm_provider-*` in
`hidden_params.additional_headers` of its logging payload (checked in source,
not yet observed end to end). Neither oMLX nor LiteLLM has an endpoint that
accepts logs. The homelab has Prometheus/Grafana/Alertmanager on `monitoring`
(`<monitoring-host>`) and no Loki, so metrics are the path. `--metrics-listen` adds a
listener that serves only metrics, for a LAN scrape.

**`doctor.py` has a `queue` layer.** It checks that the queue is reachable,
that the LiteLLM container's `OMLX_BASE_URL` matches the compose file (it
reports SOAK or LIVE), and, in LIVE mode only, that a request through LiteLLM
increments the queue's grant count. Fault-injected: when the queue port is
down, the check fails.

**opencode 1.18.20 on a 429**, tested with a fake server that always returns 429
and `Retry-After: 5`: 6 attempts exactly 5 s apart, then the turn fails with
the error message. It honours `Retry-After`. With the defaults
(`--retry-after 30`), a full queue fails a turn after about 2.5 min. A wait
timeout costs up to 6 × (600 + 30) s before the turn fails, so `--max-wait` is
the knob that matters for agents.

**The llm-router canary** (homelab-proxmox `roles/llm_router/templates/canary.sh.j2`,
read, not run): it is hourly, sends `hermes-fast` with `max_tokens: 1`, makes 3
attempts 30 s apart, and on total failure writes
`llm_router_canary_success 0`, which raises the **warning** alert
`LlmRouterCanaryFailing`. Behind the queue it will wait, not fail, unless a
wait exceeds `--max-wait`. Then a busy queue reads as an unhealthy backend. Hermes
(Open WebUI) does not retry 429 and surfaces it to the user.

### Profiles can be separate engines: switching reloads the model, or returns 409 while it is busy

Found while proving the `fast-q` path. `doctor.py`'s `diagram` check failed with
**HTTP 409**: `Model 'Qwen3.8-27B-8bit' is busy; cannot reload runtime settings
variant until active requests finish or are aborted`. oMLX computes an engine
signature from a profile's **load-time** fields (`engine_pool.py:1014`:
`mtp_enabled`, turboquant, `qwen35_ane_prefill_*`, `qwen35_oq_a8_*`, ...). When
a request's profile signature differs from the loaded engine's, oMLX **unloads
and reloads the model** ("Runtime settings variant changed ... unloading
before reload"). If the engine is busy it refuses with 409 instead of waiting.

On the 8-bit model, `reason` is the odd profile out. It carries a full dump of
engine fields (`mtp_enabled: False`, ANE prefill, turboquant, DFlash, oq_a8...)
where `code`, `agent` and `longctx` inherit them. So every switch between the
`reasoner` role and `coder`/`diagram`/agent traffic on the 8-bit is a 28 GB
reload, and a 409 when anything else is decoding. On 2026-10-11 between 18:06
and 18:09 the log shows 3 such reloads and 2 such 409s, with an unidentified
agent loop (70k-token prompts, `tool_calls`) running on the 8-bit at the same
time.

Consequence for the queue: its group key is now the **full** model id
including the profile. The earlier version stripped `:profile` on the
assumption that profiles share an engine, and that is false here. With one
slot globally, the queue never causes the 409 itself. Ordering by variant
keeps the reloads to one per switch.

### Soak wiring (2026-10-11)

- **Queued twins in LiteLLM:** `coder-q`, `reasoner-q`, `fast-q`,
  `diagram-q`, each with the same model and profile as its role, via
  `OMLX_QUEUE_BASE_URL=http://host.docker.internal:8001/v1`. The plain aliases
  are unchanged. Doctor proved `fast-q`: queue granted `0 -> 1`.
- **LAN metrics:** the LaunchAgent now runs under `/usr/bin/python3` (3.9),
  the interpreter the macOS firewall already allows inbound. It serves
  `--metrics-listen 0.0.0.0:9464` (metrics and status only); inference stays
  on 127.0.0.1:8001. Checked from the LAN IP: `:9464/metrics` 200, `:8001`
  unreachable. The suite passes on 3.9 too.
- **Homelab scrape:** deployed 2026-10-11 from branch `llm-queue-scrape`
  (commit `eec8d0a`, Promote run 38155315630, Apply run 38155558034 with the
  override that monitoring always needs, see homelab 072). In live Prometheus,
  target `llm-mac` is `up` and both rules are `ok/inactive`; 32 targets, 31
  up, and the other one is nextcloud-exporter at `unknown` after the restart.
  The branch is not merged. A default-empty `prometheus_external_scrape_targets` hook in
  `prometheus.yml.j2` (it renders identically without the variable), job
  `llm-queue` -> `<mac-lan-ip>:9464` labelled `instance=llm-mac`, and two
  warning rules in the llm-router contract, `LlmQueueWaitTooLong` (oldest
  wait > 300 s for 5 m) and `LlmQueueRejecting` (any 429 in 15 m). Both pass
  `promtool check rules`. There is deliberately no `up == 0` alert: the Mac is a
  laptop.

### The 8-bit `reason` profile: the drift was on the base model (fixed 2026-10-11)

Of the 37 engine fields stored in `reason`, exactly one differed from the base
model: `qwen35_ane_prefill_enabled`, `False` in the profile and **`True` on
the base**. §3 rejected ANE prefill (GPU-only 855.6 t/s against 799.9 for the
best ANE split), and the shipped `bench/set_variant.py` variant `mtp` sets it
`False`. So `reason` held the intended value and the **base had drifted**,
costing about 7% of prefill on `code`/`agent`/`longctx` and splitting the
engine in two. `reason`'s `updated_at` was 2026-10-03 07:58 (the oMLX 0.7.0
day). The base flip cannot be dated: the logs from that day have rotated.

**It was detectable and went unrun.** `profiles/apply.py --verify-only`
already fails a profile with `has_engine_fields` ("HAS ENGINE FIELDS (forces
reload on switch)"), and the doctor's engine layer calls it. The doctor runs
recorded on 2026-10-11 covered other layers only. Nothing checked the base
model's variant.

**Fix (applied 18:55, rollback snapshot
`results/omlx_8bit_snapshot_20261011-185450.json`):**
1. Base `qwen35_ane_prefill_enabled=False`. oMLX's settings PUT saw the
   load-signature change and auto-unloaded (`requires_reload: true`,
   `auto_unloaded: true`; the model was idle and no request was cut off).
2. `python3 profiles/apply.py` rewrote all four profiles from `definitions.py`.
   `reason` now has 11 keys and `has_engine_fields: false`, and all 4/4 verify.
3. `doctor.py` engine layer: a new check "8-bit base engine variant is the
   shipped one" compares the base against `set_variant.py`'s `mtp`
   (`SHIPPED_VARIANT`). Fault-injected: set to `ane`, it fails with the diff.

**Verified:** one load at 18:55:29, then `coder, reasoner, coder, reasoner`
through LiteLLM with **no** "Runtime settings variant changed" and no 409
(5.8 / 4.0 / 0.3 / 4.2 s). `reasoner` still returns `reasoning_content`;
`coder` does not.

### Live (2026-10-11 ~19:40)

`OMLX_BASE_URL` now points at `:8001`, so every LiteLLM chat model, including
the role aliases and the `model:profile` names opencode uses, goes through
llm_queue. `embed`/`rerank` moved to `OMLX_DIRECT_BASE_URL` (`:8000`), because
they are small calls that would stall Hermes retrieval behind a multi-minute
agent turn, and Hermes does not retry a 429. Verified: doctor `queue` layer is
LIVE (plain `fast` granted `0 -> 1`), 11/11 proxy and queue checks pass, and
embed (1024 dims) and rerank (0.922 vs 0.0) work with the queue grant count
unchanged. Rollback: set `OMLX_BASE_URL` back to `:8000` and run
`docker compose up -d`.

Known side effect: the homelab canary (3 attempts, 30 s curl timeout) will
fail whenever an agent turn holds the slot for longer than its retry window,
raising the warning `LlmRouterCanaryFailing`. Nothing is broken when that
happens; the backend is busy.

### Homelab canary, and the key the homelab cannot use (2026-10-11)

- **Canary waits out the queue:** homelab `30a4cf9` on branch
  `llm-canary-waits-out-queue` sets a per-attempt curl timeout of 660 s (past
  the queue's 600 s) and derives `TimeoutStartSec` (2100 s) from attempts,
  timeout and delay. Deployed: Promote 38161815416, Apply 38162081870. Note:
  the **"LAB" apply ran against `<llm-router-prod>`, the production llm-router**
  (homelab 067, LAB and PROD share an address space), so Prod 2 reported
  `changed=0`. For llm-router, Prod 1 is not a dry run.
- **The canary has been failing for another reason:** the Mac LiteLLM key
  `homelab-lxc-064-llm-router-v2` has `blocked = true`. Its last success was
  2026-10-10 13:37:48 UTC and the first "Key is blocked" 13:42:57 UTC; since
  then about 38 requests an hour have failed. That covers the router's health
  checks, the canary, and **all Hermes chat and retrieval**. The LiteLLM audit
  log is empty, so who blocked it and why is unknown. It was ~2 h after the
  first 2026-10-10 thermal incident, and might have been a deliberate
  load-shed. **Unblocked 2026-10-11 ~18:16 UTC** (`/key/unblock`, by hashed
  token). Router traffic succeeds again from 18:20:24 UTC (`reasoner`,
  `coder`), now through the queue. PR #235 merged (`0331de8`).

### Soak closed: the queued twins retired (2026-10-18)

The soak ended the first way the C3 note in `litellm-config.yaml` allowed:
`OMLX_BASE_URL` moved to `:8001`, so the `*-q` twins duplicated their plain
aliases byte for byte. Removed: `coder-q`, `reasoner-q`, `fast-q`,
`diagram-q` and `OMLX_QUEUE_BASE_URL`. The doctor's queue check now proves the
path with plain `fast` only, and reports BYPASSED rather than SOAK after a
rollback. The rollback itself is unchanged.

LiteLLM spend logs at removal: `coder-q` had 505 calls (438 from the local
key, 10-11 and 10-12; 67 from the homelab gateway key on 10-18, a client
on the owner's Linux laptop, repointed to `coder` the same day). `fast-q` had 1, `reasoner-q` and
`diagram-q` none. The same cleanup removed `diagram` (same model and profile
as `coder`; 6 calls in the 20 days before) and the OptiQ 27B arms (§29).

---

## 39. The agent loops were greedy decoding: oMLX's vlm_mtp path ignores the sampler

The repeated-tool-call loops looked like a new failure after the queue went
live (§38). They are not, and the queue is not involved. **oMLX 0.7.0 decodes
the 8-bit model greedily whenever a request takes the `vlm_mtp` speculative
path. Temperature, `top_p` and `seed` are ignored.** Greedy decoding over a
long agent context that returns the same tool output every turn re-emits the
same tool call, exactly what Qwen warns about.

### Evidence

| request (8-bit, one open-ended prompt) | decode path | distinct |
|---|---|---|
| `:qwen-code` ×5, direct :8000 / queue :8001 / LiteLLM `coder-q` | `vlm_mtp` | **1/5 each** |
| `:qwen-code`, `seed` 1-4 | `vlm_mtp` | **1/4** |
| `:qwen-code` (temp 0.7) vs `:qwen-agent` (temp 0.3) | `vlm_mtp` | **byte-identical** |
| `:qwen-code` + `repetition_penalty: 1.01` (forces the skip) | BatchGenerator | 4/4 |
| `:qwen-reason` (thinking budget forces the skip) | BatchGenerator | 4/4 |
| oQ4e `:oq-agent` (Lightning MTP, a different implementation) | MTP | 4/4 |

The sampler oMLX builds (`_build_sampler_and_processors`) carries the
profile's temperature, and the non-MTP path given the same request samples.
The loss happens inside the `vlm_mtp` route (`scheduler._route_to_vlm_mtp`
into mlx-vlm `speculative/mtp._mtp_rounds`). The exact line was not isolated.
This is an upstream bug.

### The loops predate the queue

Runs of identical 8-bit turns (same output length, same prompt growth) in the
logs. Every window ran 100% on `vlm_mtp` (decodes started / skipped):

| window | identical turns | `vlm_mtp` |
|---|---|---|
| 10-06 21:38 → 10-07 00:06 | ~400 | 544 / 0 |
| 10-07 03:28 → 04:01 | 159 | 166 / 0 |
| 10-07 13:24 → 13:33 | 63 | — |
| 10-11 12:06 → 12:39 (before llm_queue existed) | 144 | 153 / 0 |
| 10-11 21:52 → 22:22 | 49 | yes |
| 10-12 06:34 → 06:55 | 44+ | yes |

The first two windows overlap the 10-06/07 thermal incidents (§37): runaway
greedy loops were likely part of the heat. The loops looked new on 10-11
because the tax-free work moved from `reasoner` (thinking budget, so
`vlm_mtp` skipped) to `coder` / `coder-q` (thinking off, so `vlm_mtp`).

### Fix (2026-10-12 07:10)

- **8-bit base switched to `set_variant plain`** (no `vlm_mtp`). Rollback
  snapshot: `results/omlx_8bit_snapshot_20261012-071055.json`. All 4 profiles
  still verify.
- **Verified:** `qwen-code` gives 5/5 distinct on all three paths (direct,
  queue, LiteLLM `coder-q`) and `qwen-agent` 4/4. Decode is about 16.7 tok/s,
  down from about 30 (§7's MTP win is given up until upstream is fixed).
- **Doctor:** an engine check, "profile samples: <profile>" (4 identical
  requests, at least 2 distinct). Run **before** the fix it failed on
  `qwen-code` and `qwen-agent` (1/4) and passed oQ4e, so it caught the real
  bug, not a simulated one. `SHIPPED_VARIANT = "plain"`, and the 8-bit MTP
  speed floor is removed. That floor was already failing (16.9 tok/s),
  because its probe targets the base model, whose thinking budget makes oMLX
  skip `vlm_mtp`. The decode-speed probe had been measuring the wrong path.

### What this means for earlier numbers

Every 8-bit `code`/`agent` result taken with `vlm_mtp` on was **effectively
greedy**, so it does not represent the profile's sampling policy. This applies
to throughput and quality figures since §7 that ran thinking-off profiles
through MTP. `reason` results are unaffected. Sections cited for routing
decisions (§14, §19) should be re-checked against sampled decoding before
their deltas are relied on.

### Correction to §38

§38 said the 8-bit "now decodes at 22-25 tok/s with MTP" and called that a
speed change with no effect on output. That was wrong: those requests were
greedy.

## 40. The gemma round, part 1: gemma decodes as fast as the 8-bit, and retrieves at 131k

Backlog A10 steps 0 and 1, 2026-10-19, in an exclusive window (Hermes
`scout-assess` paused). Engine: **oMLX 0.7.1rc1**, mlx_vlm 0.7.8. Two of
today's crashes may be specific to that release candidate (see below).

### Step 0: §30's speed objection is dead

`bench/throughput.py`, same harness, `gen_short` guard on, 128 tokens
generated, neither model on an MTP path (`results/tp_a10_*.json`):

| prompt | 8-bit `plain` decode | gemma-31B QAT decode | ratio | 8-bit prefill | gemma prefill |
|---|---|---|---|---|---|
| 1k | 17.9 | 20.6 | **1.15x** | 803 | 630 |
| 4k | 17.8 | 19.9 | **1.12x** | 917 | 627 |
| 16k | 17.2 | 16.2 | **0.94x** | 780 | 591 |

§30's "0.54x the shipped 8-bit" compared against the 8-bit on `vlm_mtp`,
which §39 showed was greedy. Against the 8-bit as it actually ships since
2026-10-12, gemma decodes at about parity. It prefills at about 0.75x. The
doctor's "profile samples" check gives 4/4 distinct on `g4-code`, `g4-agent`
and `g4-reason`, so the gemma profiles sample (no §39 bug).

### Step 1: what the prefill guard admits

`bench/ctx_bisect.py`, `g4code`'s window raised to each probe length:

| `scheduler.chunked_prefill` | admitted up to | refused at |
|---|---|---|
| off (shipped) | 126,976 | 131,072 |
| on | 151,552 | 154,624 |

The guard was deterministic both times, unlike §33's "116k admitted 1 time
in 3" on 0.7.0. Backlog A10 named "a smaller prefill chunk" as the lever.
oMLX exposes no separate chunk size; the lever is the **global**
`chunked_prefill`, which changes every model's prefill. Its effect on the
other models was not measured.

Both bisect runs record "retrieval 0/3". That is wrong: all six requests
errored because oMLX had crashed. `ctx_bisect.py` now reports errors as
inconclusive, and the result JSONs carry a note.

### Retrieval: admitting is attending, up to 131k

`bench/longctx_quality.py`: one haystack per length, needles at five
depths, plus one multi-hop question (find two numbers far apart and add
them). It never unloads the model, which avoided the crash below.

| length | chunked prefill | retrieval | multi-hop (expected → got) | per question |
|---|---|---|---|---|
| 98,304 | on | 5/5 | 128302 → 128202 | 5 s (prefix cache) |
| 98,304 | off | 5/5 | 128302 → 128202 | 5 s (prefix cache) |
| 122,880 | off | 5/5 | 87759 → 87309 | ~8 min |
| 131,072 | on | 5/5 | 103674 → 103674 | ~8–10 min |
| 147,456 | on | 1/5 | refused | ~11.5 min; 4 refused |

- **Every needle the guard admitted was found, up to 147k.** §32's "65k
  ceiling" was a guard limit, not an attention limit.
- **Multi-hop misses look like arithmetic errors, not retrieval misses.**
  Three of four answers were within 0.5% of the right sum. That is one
  question per length, so it is a hint, not a measurement.
- **Chunking does not change the answers below ~127k.** Both settings gave
  byte-identical answers at 98k.
- **147k is limited by memory that is not handed back.** After the one
  admitted 147k prefill, the process footprint was 93.6 GB while MLX's
  active memory was 21.9 GB (the model alone). The guard counts the footprint
  as `current` (§35), so the next four requests were refused (needed 117.9 GB
  against a 110.4 GB limit). In practice 147k works once after a fresh load.
- **Past ~100k every question paid a full prefill.** The log says, for
  example, the stored sequence "shares the first 130,730 of 130,742 comparable
  tokens" and still "re-prefills 130749 of 130749". The SSD cache was at its
  100 GB cap (`ssd_cache_max_size`), with 99.7 GB of it other models' blocks.
  So the likely cause is that a 120k+ sequence cannot be kept. At 98k reuse
  worked (97,280 cached). **Not yet tested:** raise the cap and re-run.

### oMLX 0.7.1rc1 crashes on teardown after a long request

Twice, unloading gemma after a request of 118k–150k tokens took the whole
server down:

- 11:37 `teardown exceeded 120s; exiting process so the supervisor can restart`
- 12:21 `SSD cache writer survived engine teardown; exiting process ...`

The supervisor did not restart it; the owner restarted oMLX from the menu bar
both times. The first crash also left `g4code`'s window raised, which
`profiles/apply.py --set gemma4` repairs. Avoid model unloads after 100k+
requests on this build; `longctx_quality.py` avoided them and did not crash.
Not reproduced on 0.7.0, which was not available to compare.

### Decision: record only, cap stays at 65,536

Owner's decision, 2026-10-19. Retrieval supports 122,880 with today's
settings and 131,072 with `chunked_prefill` on. But at 8–10 minutes per
question, a 120k+ context is only usable once until the SSD cache keeps it.
The cap is re-decided after that test (backlog A10).
