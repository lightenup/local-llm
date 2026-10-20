# Backlog — local AI stack

Scope is the whole local stack, not just the Qwen3.8-27B profile setup: model
lineup and quantization, oMLX serving, a LiteLLM routing layer, task-shaped
evaluation, and a fine-tuned diagram model. Derived from the 10 Sep 2026 working
notes, reconciled against what this repo has already measured.

**Relationship to [FINDINGS.md](FINDINGS.md).** FINDINGS records what was measured
and why each lever was chosen. This file records what has not been done yet.
FINDINGS §10 "Still open" has been absorbed here — those six items appear below as
B1a/B1b, B2, A2b, A3, B4 and B3, with their measured budgets carried over.

Every item states what would settle it, because several entries in the source
notes were plausible hypotheses that measurement then killed (see §H).

---

## Sequence

**Ordering assumptions, stated because they drove the result.** The objective is
the quality ceiling on hard tasks. The scarce resource is *hands-on time*, not GPU
hours — long unattended runs are close to free here, anything needing a live
session is expensive. That second assumption inverts one of FINDINGS' priorities —
its highest-value item needs a live session, so it lands in wave 2; see B1a.

Waves, not dates. A wave is done when its gate question is answered, not when a
date passes.

### Wave 1 — re-baseline the model

The gate question: **is Coder-Next better than the shipped 27B at this workload?**
Nothing below wave 1 should be built on the current lineup until that is answered.

Status: **answered — the 27B stays the default; Coder-Next is a latency
instrument.** Coder-Next wins speed, prefill, long-context retrieval and memory by
wide margins (FINDINGS §12) and loses end-to-end coding badly: livecodebench 43.2
against 59.8, p = 0.0.

That gap was investigated to the bottom (FINDINGS §13–§14). It is *not* algorithmic
weakness — Coder-Next produces fewer outright wrong answers than the 27B, 71
against 79 — and it is *not* a scoring artifact either: a system prompt spelling
out the I/O contract recovered **2 of 60** failures, 3%. The deficit is specific
and durable: it writes a correct function body about as well as the 27B
(humaneval -3.7, mbpp -0.2) and is much worse at producing a complete runnable
program that honours a contract. For agentic coding that is the wrong weakness to
have.

It is also behind on multi-domain reasoning, with no I/O contract available as an
excuse: mmlu_pro -6.0 (p = 0.007), hellaswag -4.4, safetybench -5.4.

**Then D2 changed it back, on evidence.** Best-of-4 against a verifier recovers
22% of those failures for 1.6x wall-clock, projecting livecodebench 43.2 -> ~56.0
against the 27B's 59.8 — a 3.8-point gap — while still running 1.32x faster than
the 27B at N=1 (FINDINGS §15). The speed *is* convertible; §14's "latency
instrument, not a quality one" holds only at N=1.

**So the rule is the verifier, not the model.** Where something can reject bad
candidates — tests, a compiler, a type checker, a renderer — Coder-Next with
best-of-4 beats the 27B on both speed and quality. Where nothing can, the 27B
stays the default. That covers most coding here and all of the diagram work.

A2b's oQ arm still matters for memory: RTN 4-bit costs reasoning specifically
(p = 0.0028), so the reasoning tier cannot ride at 4-bit unless data-driven
allocation recovers it.

**Wave 1 is now closed.** A2a, B2, A1, D2 and B1b are all done, plus two unplanned
prerequisites: Coder-Next's three profiles (`profiles/codernext.py`) and the
opencode wiring.

**Wired, as of the end of wave 1:** `title`, `summary`, `compaction`, `explore`,
`general` and `small_model` run on `cn-agent`; `build`, `plan`, `speckit` and
`speckit-wide` stay on the 27B. The reason is that **opencode never does
best-of-N** — every call is N=1, where §14 applies and §15 does not — so
Coder-Next takes the speed-dominated, quality-insensitive work only. Closing that
gap is C9. The 27B is pinned and Coder-Next is not, so Coder-Next pages first.

1. ~~**A2a — identify the quantization build method**~~ **done.**
   `bench/quant_id.py`, results in `results/quant_id.json`. Everything on disk is
   stock RTN; the three vendor 6-bit Coder-Next builds are byte-identical so the
   build choice is moot; no DWQ exists for either model. It did its job as a gate
   twice over: it re-scoped A2b into a three-way build-method test, and it turned
   up that 6-bit Coder-Next is 64.7 GB rather than the assumed ~55–60, which
   re-opens 6-bit vs 8-bit as a live decision inside A1.
2. ~~**B2 — fix the long-context harness, verify 262k**~~ **done.** 15/15 needles
   and 3/3 multi-hop at a verified 261,826 prompt tokens, no depth penalty
   (`results/longctx_quality.json`, FINDINGS §8). The ratio was wrong in shape,
   not value — there is a fixed ~1,800-token overhead under it — so the harness
   now fits the chars-to-tokens line at run time from two probes instead of
   assuming a constant, which is what makes it survive A1's tokenizer change.
   It also produced the first real 262k prefill timing, 18.7 min at 234 t/s, and
   99.3% cache reuse on repeats.
3. ~~**A1 — Coder-Next at 6-bit**~~ **done, and it does not win.** 2.0–2.5x decode
   and 2.6–3.6x prefill with no speculative decoding available to it, 15/15 needles
   at 262k, valid tool calls, ~85 GB at full context (FINDINGS §12) — then
   -16.6 points on livecodebench at p = 0.0 (FINDINGS §13). `specprefill` turned
   out not to be testable here at all: oMLX needs a draft model sharing the
   target's tokenizer and no Coder-Next drafter exists.
4. ~~**D2 — re-cost best-of-N against a verifier**~~ **done, and it is the most
   consequential result of the wave.** N=4 costs 1.60x wall-clock, not 4x, because
   a shared prompt is the batching best case rather than §8's worst. pass@4
   recovers 9 of 40 failures (22%) where greedy recovers 1. Projects to
   livecodebench ~56.0 against the 27B's 59.8, at 1.32x the 27B's speed
   (FINDINGS §15). Original framing kept below:
   (~1 h, scriptable). Promoted out
   of the D lane because under a quality-ceiling objective this *is* the
   test-time-compute lever, and it is an hour's work. N completions from one
   identical prompt at N = 2, 4, 8, measuring wall-clock to all-N-complete. Run it
   on whichever model wins step 3. If it works, it plausibly raises the ceiling
   more than the model swap did; if it does not, that closes the notes' largest
   untested claim.
5. ~~**B1b — audit the prefix sources**~~ **done.** Skills are static with no
   runtime interpolation. opencode injects an environment block carrying *today's
   date* and the working directory, so the prefix changes daily and per project
   but not per request, and it sits after the bulk of the static instructions so a
   rollover invalidates only the tail. One unconfirmed risk: the injected
   skills list is assembled from two directories and would reorder between
   sessions if that enumeration is unsorted — B1a can settle it against live
   traffic. Also found: `opencode.json` still lists only the four 27B profiles, so
   the `cn-*` profiles are served but unreachable by any client (a C3 prerequisite).
   Original framing kept below:
   (~30 min, code read only). The half of B1
   that needs no live session: check the opencode system prompt, skills and Spec
   Kit scaffolds for anything nondeterministic — timestamps, cwd, tool ordering,
   injected dates. Any one of those turns a shared prefix into a unique one and
   makes the measured half moot.

> **Watch the upload.** Accuracy suites with ≥100 questions upload to `omlx.ai`
> with a machine hash and model settings, with no opt-out (FINDINGS §11). Wave 1 is
> accuracy-heavy. Use `SAMPLES=99` to stay local, or accept it deliberately.

### Wave 2 — settle the lineup, on memory as well as accuracy — CLOSED 2026-09-12

**Answered: no — but the second tier changed, and that closed three problems at
once.** No single checkpoint replaces the lineup: the 4-bit sibling loses to the
8-bit on both reasoning (p = 0.0095) and code (p = 0.00119). It decisively
replaces *Coder-Next* though, and in doing so fixed the residency thrash, the
fast-tier quality gap and the swap cost together. Lineup is **8-bit for quality,
oQ4e for speed**, 44 GB resident against 93.

Everything measured in this wave now lives in **FINDINGS §16–§21**. What remains
below is the plan as executed, kept for the sequencing rationale.

| | outcome |
|---|---|
| routing layer, role aliases, tool calls, overhead | done — §16 |
| best-of-N against a verifier | **closed, measured and rejected** — §17 |
| memory guard and residency | closed — §18 |
| quantisation three-way, lineup change | closed — §19 |
| prefix-cache hit rate | **86.7%** — §20 |
| task-shaped metrics harness | built — §21 |
| second-model reload cost | ~20 s with a 64.7 GB partner, ~2.6 s with 16 GB |

Original gate question: **does one checkpoint replace the two-tier lineup?** Wave 2 is
done when the model set is decided on evidence and every tool addresses it by
role — with the routing infrastructure already built rather than pending.

**Re-cut 2026-09-11, twice, and the second re-cut is on measurement.** The wave
opened as "settle the tier, then make it routable", was re-cut mid-session to
"can the verifier rule be exercised from real tools?", and that gate has now been
**answered: no** (C9, closed). Best-of-N and MTP compete for the same silicon
here, so the sampler loses the speed that was meant to pay for the samples —
measured head to head, the 27B alone recovered 6/12 against best-of-4's 2/12 in
the same wall-clock.

What replaced it emerged from the same work. Two findings point at one question:

- **A3b:** the 27B and Coder-Next cannot both hold KV under the guard's ceiling,
  so every alternation evicts and reloads at ~20 s — on a routing table designed
  to alternate constantly. The pressure is largely an artifact of a guard that
  over-estimates KV by ~9x, but the evictions are real.
- **A2b gate:** `oQ4e` decodes at 64 tok/s in **16 GB** with in-checkpoint MTP,
  against the shipped 8-bit's ~34 tok/s in 28 GB.

So the live question is no longer how to rescue a fast-but-weak second model. It
is whether a 4-bit 27B sibling is simultaneously fast enough to retire
Coder-Next, good enough to retire the 8-bit, and small enough to end the thrash.
If tonight's reasoning arm holds, the lineup collapses from two checkpoints to
one and three problems close together. If it does not, the fallback is the
guard-tier and profile-context levers in A3b, not another model.

**Routing is no longer the gate but is already delivered:** C1, C3, C4-slice and
C5 all landed on 2026-09-11.

**Why this is not the gate the plan originally had.** The written gate was "what
is the final model set, and can every tool address it by role?", which treats
routing as the objective. Wave 1 ended with something better: a *rule* —
where something can reject bad candidates, Coder-Next at best-of-4 beats the 27B
on both speed and quality (§15) — that nothing can currently invoke, because
opencode only ever sends N=1. The lineup question is largely answered already;
the unexercised rule is not. So C9 leads, routing is the means, D1 supplies the
verifiers, and A2b/A3 demote to unattended filler.

**Scheduling shape, stated because it drove the order.** One 6 h live session,
then a weekend. Unattended GPU hours remain near-free, so the two overnight slots
are sized separately from the hands-on ones and never compete with them.

**E1 is out of scope**, and the consequence is that E2, E3 and E4 stay
unscheduled — the diagram-tuning lane is parked for the whole of wave 2.

#### Session 1 — the live 6 h

**Ran 2026-09-11. All six items done in ~35 min against a 6 h budget**, because
the two things budgeted as risky were not: the corporate proxy root CA was
already extracted (`CORP_ROOT_CA` in `proxy/.env`), and the 17 GB download finished
before the proxy was up. Results and the four findings are in §C1-note below.
The A2b gate (step 12) was pulled forward into the session for the same reason
and **passed at 2.12x**, so the overnight slot is committed.

6. **B1a — prefix-cache hit rate, at t=0 and every ~45 min.** FINDINGS ranks this
   #1 by value. Its cost was never the hour budgeted for it; the cost is that it
   cannot be scheduled, because it needs real traffic. A live session *is* the
   opportunity, so it is instrumented rather than scheduled: `POST
   /admin/api/cache/probe` at session start and at intervals through it. Two
   pieces of prior evidence to reconcile against: B2 measured 99.3% reuse on
   repeats, and B1b found opencode injects today's date and the cwd — so the
   prefix rolls over daily and per project, but only in its tail. The one
   unconfirmed risk B1b left open is whether the injected skills list reorders
   between sessions; this is what settles it.
7. **Start the oQ4e download** (~17 GB, background, no GPU). Gates step 12.
8. **C1 + C7 — LiteLLM, Postgres, corporate TLS proxy, `host.docker.internal`.** Full
   Docker + Postgres now rather than a bare container, even though budget
   enforcement is a wave 3 concern: building it twice costs more than building it
   once. Pin a specific image tag (malware advisory against particular 1.82.x
   releases). Local access group and one key only.
   **This is the only unbounded-risk item in the wave** — the corporate proxy CA
   inside Docker cannot see the keychain, so the `NODE_EXTRA_CA_CERTS` trick from LM Studio does
   not carry over; mount the root CA and set `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`.
   **Decided: let it run long.** It blocks every other routing item, so if it eats
   the session, steps 9–11 slip to Saturday and nothing else is attempted Friday.
9. **C4 slice — one tool call with a file edit, through the hop** (~20 min).
   A gate, not the item. C4 proper sits at step 14, but it is the item most likely
   to fail quietly *and* the one that would invalidate C1, C3 and C9 if it failed.
   Coder-Next has its own function-call format and a mangled call degrades into
   plausible-looking prose rather than an error, so the hop is verified before
   anything is built on top of it.
10. **C5 — proxy overhead, and raise timeouts hard** (~30 min). Nearly free once
    C1 is up, and it gates C6 in wave 3. Defaults fire during prefill: cold
    prefill at 16k is ~12.6 s here and 262k contexts are available.
11. **C3 — the role aliases.** `coder`, `reasoner`, `fast`, `diagram`.
    **Decided: `qwen-long` folds into `reasoner` with a context override**, so the
    table stays at four roles and 262k becomes a per-request parameter rather than
    a model identity. Closes the corresponding §G open question. Edits the
    dotfiles `opencode.json`, which `omlx launch opencode` overwrites through the
    symlink.

#### Overnight slot 1

12. **A2b oQ arm — gate first, then ~10 h unattended.** The gate is ~15 min:
    confirm the `Jundot` MTP drafter actually loads under `set_variant.py`. It is
    uploader-asserted and unverified, and if it fails the arm is dead and the slot
    goes to step 15 instead. **Decided: SAMPLES=500, and the `omlx.ai` upload is
    accepted deliberately.** See A2b for why 99 is not available.

#### Session 2 — the weekend

13. **The verifier substrate, then D1.** C9's verifier (binary, per-project, gates
    a candidate) and D1's verifier (paired, per-item, scores a model) are two
    consumers of one runner, so the runner is built once and both sit on top:
    D1 as scorer, C9 as gate. `bench/best_of_n.py::value()` is the prototype but
    its verifier is livecodebench-shaped and needs generalising. D1's metrics are
    render-success rate, XML validity, tool-call format adherence and
    tokens-to-first-useful-diff; it must be paired and per-item like
    `bench/paired_compare.py`, since a 500-sample suite resolves nothing under
    ~2 points. It also sidesteps the `omlx.ai` upload, which matters more now that
    step 12 has accepted one.
14. **C9 — best-of-N orchestration in the proxy.** The wave's gate item. Sample N,
    run the project-supplied verifier command, return the first candidate that
    passes. 1.60x wall-clock at N=4, recovers 22% of Coder-Next's failures (§15).
    The hard part is the per-project verifier contract, which is what step 13
    exists to supply.
15. **C4 proper — a real opencode task with file edits through the proxy**, now
    that step 9 has de-risked the format question.
16. **A3 — second-model reload cost** (~1 h). Partly answered in practice already:
    Coder-Next is the unpinned second resident model and pages first. **Must not
    interleave with step 15** — it forces reloads that evict residents.

### Wave 3 — cloud, capability, and ceiling probes

Everything here is either externally blocked or has a stated prior that it loses.

17. **C2 — cloud access group, second key, explicit `coder-cloud` alias.**
    **Structure built 2026-09-12, still blocked on a sanctioned API provider.**
    `proxy/CLOUD.md` is the checklist and the `coder-cloud` block sits commented
    in `litellm-config.yaml`, so enabling a provider is an edit rather than a
    design exercise. The safety property is already pinned: `fallbacks` and
    `content_policy_fallbacks` stay empty, because a router-level fallback fires
    **even when the calling key may not call the fallback model directly** — so
    local→cloud spill is the *default*, and an access group alone does not
    prevent it. The 403 half is verified; the "does a local alias silently reach
    cloud when the local model is down" half cannot be tested until a cloud
    deployment exists, and is the check most likely to be skipped.

    **Its value has dropped.** The reason to build a cloud tier was frontier
    capability, and Factory already provides that with org controls intact (§G).
    What remains is unified routing and logging — convenience, not capability —
    which is a weak case for taking on a governance question.
18. **C6 — move the benchmark harness behind the proxy.** Gated on C5's result.
19. ~~**B5 → A4 — the GGUF lane and the ceiling probe**~~ **CLOSED 2026-09-12,
    see FINDINGS §22.** Run once and closed, as intended. A 122B MoE loses to the
    dense 27B by 6.7–8.0 points, and the extra bit of precision bought nothing
    (Q5 vs Q4: p = 0.790), so the result is not quantisation damage. Three
    measurements now converge — 80B MoE −16.6, 122B MoE −6.7/−8.0, and 4-bit
    losing to 8-bit on the same model — so **parameter count is not the lever on
    128 GB**. Nothing in the lane justified a second model store; llama.cpp was
    uninstalled. The original DeepSeek IQ2 target could not have run at all
    (110–125 GB against a 107.52 GB cap) and would have confirmed its own premise
    at ~2.5 bits/param.
20. **F1, F2 — decisions, not work.** Vision needs a stated use case; the most
    plausible is a rendered-PNG critic feeding E3's reward, which would fold it
    into the E lane rather than making it a lane of its own. Docling/MinerU needs a
    stated driver — locality, quality or cost.

### Wave 4 — findings dashboard

The gate question: **can a first-time reader trust every number on the
dashboard without reading FINDINGS.md?** The wave is done when every displayed
metric carries an explanation (what / how / evidence) with a link to its
source, and the generator fails loudly on stale or invalid findings.

Status: **specified 2026-09-22** (`docs/001-findings-dashboard/spec.md`),
not yet planned. Spec Kit is initialised for opencode; the flow is
`/speckit.plan` → `/speckit.tasks` → `/speckit.implement`.

Constitution amended to v1.1.0 on 2026-09-22 (owner-mandated during spec
review): added Principle VI, KISS/YAGNI — smallest implementation that
satisfies the spec wins, unspecced capabilities go to the backlog — and
Principle VII, Test-First (red-green-refactor, non-negotiable) — no production
code without a test seen to fail first.

Design decisions settled with the owner on 2026-09-22, because they constrain
every later wave on this lane:

- **A curated findings layer** (one small structured file per finding) sits
  between the dashboard and `results/`. The dashboard never auto-derives
  findings from the 134 heterogeneous results files; verdicts that exist only
  in FINDINGS prose live in the layer with a section link. This is the
  mitigation for "benchmarks evolve over time": a finding names the file and
  JSON path it depends on, so drift becomes a visible stale flag, never a
  silent wrong number.
- **Static, self-contained HTML** generated by a Python script. No server, no
  network at view time, opens from `file://`. Chosen over a web app (no
  running process, output is a reviewable artifact) and over a JS framework
  (no Node toolchain in a Python repo).
- **V1 scope** is the live lineup + headline metrics + candidate status board
  + settled dead ends. Full per-section FINDINGS rendering is D8, later.

21. **D7 — findings dashboard v1.** Spec:
    `docs/001-findings-dashboard/spec.md`. P1: lineup view with
    evidence-backed metrics (tooltips: what/how/evidence + link), candidate
    status board (shipped / on-trial / retired / dead-end / open), settled
    dead-ends list. P2: one-file-per-finding editing, stale-source detection.
    P3: deep links to raw sources. Success criteria SC-001–SC-006 are in the
    spec. The seed set must cover the live lineup (27B-8bit with its four
    profiles, oQ4e, gemma-4-31B), the closed candidates (Coder-Next,
    Flash-Next, OptiQ, the 35B-A3B class) and the §H dead ends.
22. **D8 — full FINDINGS rendering (later).** One view per FINDINGS section,
    driven by the same findings layer. Gated on D7's layer proving itself: the
    known maintenance cost is section renumbering (links are section number +
    title; the generator warns on title mismatch).

### Parked — not scheduled, and that is deliberate

- **B3 — sampling variance floor.** ~~Parked~~ **closed, and the answer is zero.**
  A same-model re-run of mathqa at 500 samples flipped **0 of 500** verdicts, and
  the engine is bit-exact at temperature 0. There is no variance floor at these
  settings, so every measured flip is signal and no A/B here needs repeating to
  average out noise (FINDINGS §14). It cost 4 minutes, not the 6 h budgeted.
- ~~**B4 — DFlash.**~~ **CLOSED 2026-09-12, see FINDINGS §23.** The stated
  blocker genuinely lifted — a DFlash2 drafter now exists for exactly the shipped
  checkpoint, and MLX builds of it are published. Assembled from three repos and
  architecturally sound, oMLX registers it as a helper after a restart, accepts
  `dflash_enabled` without error, and then decodes at **17.9 tok/s against the
  shipped MTP's 31.1** — indistinguishable from no speculative decoding at all.
  A silent no-op: oMLX falls back to dense when the drafter is not in its native
  format. The attractive third-party number (76.1 tok/s) comes from raw mlx-lm
  plus a community port, i.e. a third serving stack, which is the same trade
  §22 rejected. Re-open only if oMLX ships native DFlash drafter support; the
  assembled drafter is already on disk.
- **C8 — Hermes over the tailnet.** No stated urgency, and it depends on C1 and C2
  landing first anyway.

Also still closed from FINDINGS, and not revisited by any wave above: ANE prefill,
TurboQuant KV, the in-memory hot cache, `chunked_prefill`, raising the concurrency
limit, and `qwen-code` temperature. `specprefill` is the one exception — closed for
the dense 27B, re-opened for exactly one test in step 3.

---

## A. Model lineup and quantization

### A1. Pull Qwen3-Coder-Next at 6-bit and re-run the profile benchmark

80B total / ~3B active, 512 experts with 10 active, hybrid Gated DeltaNet + Gated
Attention, 256K context, trained for long-horizon tool use and recovery from
failed execution. Roughly 55–60 GB at 6-bit with real context headroom. The
argument for it is structural: decode on Apple Silicon is memory-bandwidth-bound,
so a dense 27B pays 27B of weight reads per token while this pays ~3B. On a 128 GB
machine that is the single largest unexploited gap.

Three things this repo already knows that the notes do not account for:

- **The 1.97× MTP decode win does not transfer** — confirmed, three ways: no
  `nextn`/`mtp` keys in the build's config, zero matching tensors among its 1,891,
  and no MTP block in the upstream `Qwen/Qwen3-Coder-Next` either, with no drafter
  on the Hub. Every Coder-Next number is therefore unaccelerated, and it still
  wins by 2.0–2.5x. A future drafter is upside, not a prerequisite.
- **`specprefill` did not engage on a dense checkpoint** and was predicted to need
  a sparse one (§8). Coder-Next is that sparse one. It is worth one re-test here
  even though it is closed for the 27B.
- **It has its own function-call format.** `bench/smoke.py`'s tool-call check is
  the minimum bar; a real opencode task with file edits is the actual bar. This
  overlaps C4.

**Measured. See FINDINGS §12 for the numbers**; what follows is what the plan got
right and wrong.

**Which build**, settled by A2a: `mlx-community/Qwen3-Coder-Next-6bit`, **64.7 GB**
— not the ~55–60 GB the notes estimated. The three vendor 6-bit builds are
byte-identical, so there is nothing to choose between them, and no DWQ exists.

**The 6-bit vs 8-bit question is now a real one and is not settled.** 8-bit is
84.7 GB against 6-bit's 64.7 GB — a 20 GB difference on a 128 GB machine — and the
notes' own quantization numbers say 8-bit tracks bf16 roughly 2× closer than
6-bit. Under a quality-ceiling objective that is the argument for 8-bit; the
argument against is context headroom. Pulling both costs 150 GB of a 1.5 TB free
disk, so the download is not the constraint.

**Now measured, and 8-bit is not excluded after all.** The worry here was based on
the 27B, where a 262k run put the server at ~72 GB against a 29.5 GB checkpoint,
implying ~40 GB of KV. The caveat logged at the time was right: Coder-Next's hybrid
layers carry constant-size state, and its curve is much flatter — 61.6 GB at 1k,
73.3 at 131k, 79.2 at 200k, so ~88 KB/token and **~85 GB extrapolated at 262k**.

An 8-bit build would therefore land near **105 GB of 128** at full context. That
fits, but it leaves ~23 GB and forecloses a second resident model, which is what
A3 and E4 both want. 6-bit at ~85 GB leaves ~43 GB. Unless an accuracy pass shows
6-bit giving something away, 6-bit is the better trade — decided on measurement
rather than on the assumption that started this.

**Settles:** whether the tier in §A is worth building at all.
**Cost:** download plus ~2 h of throughput and smoke work; add ~7 h if accuracy
suites run. **Blocks:** A3, A4, C3.

### A2a. Identify the build method — done

Run `python3 bench/quant_id.py --all --json`; results in `results/quant_id.json`.
Classification is read off `config.json` alone, so a Hub repo costs one request
and no weight download. What it found:

| build | classification | size |
| --- | --- | --- |
| `Qwen3.8-27B-8bit` (shipped) | uniform RTN, group 64, affine | 29.5 GB |
| `Qwen3.8-27B-4bit` (on disk) | uniform RTN, group 64, affine | 16.1 GB |
| `Qwen3-Coder-Next-6bit` (three vendors) | 6-bit RTN + router bump | 64.7 GB |
| `Qwen3-Coder-Next-8bit` | uniform RTN | 84.7 GB |
| `nightmedia/…-qx53n-mlx` | mixed-**down**, ~3.8 effective bits | 43.4 GB |
| `Jundot/Qwen3.8-27B-oQ4e-mtp` | mixed-**up**, 4-bit base + 166 modules at 5 | 17.0 GB |

Four things worth carrying forward:

1. **Everything this repo has ever measured is stock RTN.** The shipped 8-bit is
   plain affine round-to-nearest, no per-module allocation anywhere. That does not
   invalidate anything in FINDINGS, but it does mean no result here has ever been
   compared against a data-driven build.
2. **The three 6-bit Coder-Next builds are the same conversion.** mlx-community,
   lmstudio-community and NexVeridian ship **byte-identical `config.json`**
   (sha256 `ad6f5598ad9e24c1…`) and identical file sizes. Choosing between them is
   not a decision; take mlx-community for consistency. This kills a question the
   plan assumed was open.
3. **No DWQ build exists for either model**, so the notes' headline "DWQ beats RTN
   3× at the same size" has no local action attached to it. DWQ is also not
   detectable from config — it is a distillation procedure that leaves an ordinary
   affine config behind — so it can only ever be read off the repo name.
4. **`qx53n` is not the oQ-style bet it looks like.** It is base 5-bit with the
   bulk expert projections pushed *down* to 3-bit: ~3.8 effective bits and 43.4 GB.
   That is a size-reduction build, not better-quality-per-byte, and it is the case
   most easily mistaken for the opposite. The classifier separates `mixed-up` from
   `mixed-down` for exactly this reason.

### A2b. The quantization comparison — now three-way

`bench/quant_compare.py` is fixed but unrun (FINDINGS §10.3). A2a turned it from a
bit-width question into a **build-method** question, which is the more valuable
one: `Jundot/Qwen3.8-27B-oQ4e-mtp` is a genuine mixed-up build of the current
production model at 17.0 GB against the on-disk RTN 4-bit's 16.1 GB — near enough
the same size — **and it ships an MTP drafter**, so it can be measured under the
shipped engine variant rather than a degraded one.

So the run becomes 8-bit RTN (shipped) vs 4-bit RTN (on disk) vs 4-bit oQ. That
tests the notes' central claim directly, on the production model, drafter intact,
for one extra ~17 GB download. ~10 h unattended instead of ~7 h.

Caveat to check before trusting it: `Jundot` is an individual repo, not
mlx-community. It has ~17k downloads, so it is not obscure, but the extra-bit
allocation is asserted by the uploader and unverified. Confirm the MTP drafter
actually loads under `set_variant.py` before committing the accuracy hours.

**The upload is not avoidable here, and that is now a taken decision.** The RTN
arm ran at **500 samples per suite** (`acc_ship_*.json`, `acc_quant4_*.json`:
humaneval/mbpp/livecodebench and mmlu_pro/gsm8k/mathqa, batch 8, deterministic).
`paired_compare.py` works question by question, so the oQ arm must use the *same*
question set or there is nothing to pair against — and 500 crosses the ≥100
threshold that triggers the `omlx.ai` upload (§11). `SAMPLES=99` would keep it
local at the cost of the pairing, which is the only thing making A2b
interpretable at all: the effect being tested is the p = 0.0028 reasoning gap,
which a 500-sample *score* comparison already cannot resolve. **So: run at 500 and
accept the upload deliberately.** It is a quantization A/B on public suites, so
what leaves the machine is the hash and the model settings, not any private
prompt content. This is also a standing argument for D1, which sidesteps the
mechanism entirely.

#### ANSWERED 2026-09-11/12 — build method does NOT rival bit count

The whole three-way ran. `Jundot/Qwen3.8-27B-oQ4e-mtp` is a genuine data-driven
mixed-up build at 17.0 GB against stock RTN 4-bit's 16.1, and it performs the
**same as stock RTN on both axes**:

| comparison | reasoning | code |
|---|---|---|
| oQ4e vs **RTN 4-bit** | p = 0.582, no difference | p = 0.096, no difference |
| oQ4e vs **shipped 8-bit** | **8-bit better, p = 0.0095** | **8-bit better, p = 0.00119** |

That closes the source notes' central quantization claim: at this size, on this
model, *how* you allocate the bits does not measurably rival *how many* you
allocate. A2a had already shown everything on disk was stock RTN and that no DWQ
build exists; this shows the one genuine alternative build on the Hub buys
nothing. **Do not re-open without a DWQ checkpoint**, which is a different
procedure and the only untested branch left.

Note it also **fails to replicate the RTN arm's one encouraging result**. RTN
4-bit matched the 8-bit on code (p = 0.130); oQ4e does not (p = 0.00119,
livecodebench 59.8 → 54.0 at p = 0.00193). So 4-bit is *not* safely
quality-neutral for code after all — the earlier reading was the more favourable
of two samples, not a settled fact.

**So oQ4e cannot replace the 8-bit.** The lineup does not collapse to one
checkpoint. But that is not where its value turned out to be — see A2c.

### A2c. oQ4e replaces Coder-Next as the fast tier — decided

The wave-2 gate asked whether one checkpoint replaces the two-tier lineup. The
answer is no, but the **second** tier changes, and that resolves A3b.

| oQ4e vs Coder-Next | result |
|---|---|
| code | **oQ4e better, p = 0.00006** — livecodebench **43.2 → 54.0**, p = 0.0 |
| reasoning | no difference, p = 0.383 |
| weights | **16.0 GB** vs 64.7 GB |
| residency with the 27B | **no eviction, ~2.8 s swaps** vs eviction every time, ~20 s |
| decode @16k | 58.8 vs 71.4 tok/s — Coder-Next 1.21x |
| prefill @16k | 798 vs 2,475 tok/s — **Coder-Next 3.10x** |

Coder-Next keeps exactly one durable advantage: **3.1x prefill**, the hybrid
Gated DeltaNet architecture doing what it was built for. oQ4e has *no* prefill
advantage at all over the 8-bit (798 vs 793 — same architecture, fewer bits does
not help prefill); its entire gain is decode, 1.73x.

**And the prefill advantage does not survive the memory arithmetic.** At a 16k
prompt Coder-Next saves 13.9 s of prefill per call, while every alternation to or
from it costs ~20 s of evict-and-reload (A3b). Break-even is ~1.4 prefills per
alternation. The opencode routing alternates on essentially every call, so in the
workload it was actually chosen for, Coder-Next loses to its own residency cost —
before counting that it is 10.8 livecodebench points worse.

**Decision: retire Coder-Next; the lineup is 27B 8-bit (quality) + oQ4e (fast).**
44 GB of weights against 93, both resident, no thrash. **Keep Coder-Next on disk**
for one case it would still win: a long-context scan that stays on one model for
many large prefills without alternating. That is not how anything here currently
uses it.

#### Gate notes (2026-09-11), kept — the gate was passed better than the plan assumed
(`results/a2b_gate_oq4e.json`). The uploader's claim is not just credible, it is
structurally better than the shipped arrangement: `Qwen3.8-27B-oQ4e-mtp` reports
`mtp_compatible=true` with **no blocking reason**, where both mlx-community MTP
drafters report false ("config declares MTP layers but the weight files contain
neither mtp.* tensors nor native nextn layers"). So this checkpoint carries a
genuine **in-checkpoint** MTP block and needs no separate drafter at all —
unlike the shipped 8-bit, whose 1.97x comes from a separate
`Qwen3.8-27B-MTP-8bit` wired through `vlm_mtp_draft_model` (§H).

Measured: **64.0 tok/s with `mtp_enabled`, 30.2 with it off — 2.12x**, against
the shipped 8-bit's 1.97x. Loads in 4 s at 15.97 GB. In absolute terms it
decodes roughly **1.9x the shipped setup** at **half the memory**, which raises
the stakes on the accuracy arm considerably: if reasoning holds, the lineup
collapses to one checkpoint that is faster and smaller than either current tier.

Run it with `bench/oq4e_overnight.sh`, which puts the **reason** group first —
cheaper (~4 s/question against livecodebench's ~50 s) and the only arm that can
change the lineup, since RTN already showed code does not discriminate at 4 bits.

**Settles:** whether build method rivals bit count here, and A3/E4's memory budget.
**Judge with:** `bench/paired_compare.py`, not suite scores — §8 showed a
500-sample suite cannot resolve under ~2 points.

### A3b. Memory guard — closed, see FINDINGS §18

Settled and moved to the measurement record. Summary: the guard projects
**~45 KB/token + ~0.42 GB**, linear in prompt length; the real constraint is
residency, not per-request KV. Under the 102.14 GB balanced ceiling, the 8-bit
plus Coder-Next is 96.24 GB and leaves 5.9 GB for KV, so any substantial request
evicts — ~20 s per alternation. The 8-bit plus oQ4e is 44.2 GB and leaves 57.9,
which is why the lineup change fixed it.

**Two readings were published here and both were wrong** — "KV is sized to the
configured context" and "the guard over-estimates by ~9x". Both are retracted in
FINDINGS §18 with what falsified them, because both are plausible guesses that
would otherwise be re-derived.

**Levers, none needed at 44 GB**, and the trigger that would revive them is
adding a third model or any pair above ~60 GB of weights: tier accepts
`aggressive`/`balanced`/`custom`; `memory_guard_custom_ceiling_gb` **does** work
at runtime but is ignored unless the tier is `custom`.

### A3. Fast / routing model, 4B-class, kept resident

For classification, commit messages, tool-arg fill, and opencode's `title`,
`summary` and `compaction` calls, which currently each occupy the 27 GB model.
FINDINGS §10.4 already identified the blocker and it is not memory: a second model
is a **different variant signature**, so serving both means either two resident
engines or an engine reload per switch, and §6's zero-cost profile switching does
not extend across models. `Qwen2.5-Coder-3B-8bit` is already registered.

**Measure the reload cost before assuming this is a win.** If A1 lands, re-check —
paging an 80B MoE off the SSD tier while a 4B stays pinned is a different trade
than the 27B case.

**Cost:** ~1 h to measure the reload; unknown to implement. **Depends on:** A1.

### A4. DeepSeek V4-Flash at IQ2 — one ceiling experiment, then a decision

284B total / 13B active, ~110–125 GB, ~27–34 tok/s. This is the "biggest thing
that runs locally" probe. IQ2 is a GGUF quant, so it is llama.cpp/Ollama
territory, not oMLX — which means it also decides B5. Run it exactly once against
6-bit Coder-Next and either adopt or close it.

The prior is that it loses: the notes' own rule of thumb is that **mid-size at 6–8
bit beats huge at 2-bit** when the constraint is quality ceiling, and this repo's
quantization evidence (A2) points the same way.

**Settles:** whether parameter count is worth chasing at all on 128 GB.
**Cost:** ~1 day including the lane setup. **Depends on:** A1, B5.

### A5. Watch item — MoE expert streaming from disk

Tracking only, no work planned. The technique is real on Metal (llama.cpp
`--moe-stream` and friends; a proof-of-concept ran Qwen3-30B-A3B-Q6_K on a 16 GB
M1 Pro at 13 tok/s, byte-identical output), but it does not rescue GLM-5.2 here —
see §H. Where it *would* pay is a small-active-param model with a high resident
fraction, which is Coder-Next's profile exactly. Revisit only if the PR merges
**and** B5 exists.

### A6. Qwen3.8-Flash-Next-oQ4e-mtp — CLOSED, retired on residency, see §27

**Status 2026-09-16: closed. The arm is retired and wired to nothing.** All four
axes are now measured. It is 1.41x faster than the shipped 8-bit end to end at
16k (12.45 s vs 17.50 s to 512 tokens) and indistinguishable from it on quality
(taskbench McNemar p = 1.00000 against all three arms on disk) — and it cannot be
co-resident with the lineup at all. Loading it alongside the shipped 8-bit
succeeds at 101.50 GiB, and then **all 8 alternating 16k calls were rejected for
both models**: the prefill guard's dynamic ceiling (91.29 GB) sits below the
admission cap (115.45 GB) that let it load. Co-residency does not make Flash-Next
slow, it takes the shipped model down with it. Full write-up in FINDINGS §27.

`iogpu.wired_limit_mb`, which the HTTP 507 names as the lever, does not help:
the ceiling that rejects the prefill is below the one the lever raises, and
admitting the projected 124.35 GiB would need the kernel limit at ~97% of RAM.

Three of §26's numbers were withdrawn in the process — the decode figures came
from truncated generations, "three ceilings" was a GB/GiB unit error, and the
63.5 s swap was a cold-PLE number (~31 s warm). §27 carries the corrections.

**One question left open, deliberately:** the tool-call bias. `htool-escape`
failed 3/3 with `wrong_function: read_file`, inventing a tool absent from the
schema, and on a bare corpus prompt with no tools needed one reply in six came
back `finish_reason: tool_calls`. It does not need settling while the arm is
unwired, but it would have to be settled before any `qwen4_exp` checkpoint is
considered again.

**The generalisable result, which outlives this checkpoint:** a model can be
admitted and then be unable to serve. Any future >70 GB candidate has to be
tested by issuing a realistic prompt *while the lineup is resident*, not by
checking that it loads. `is_pinned` and `ttl_seconds` are ordinary per-model
settings, so recomposing the lineup for that test costs nothing and needs no
restart — which is how A6's coexistence question finally got answered after §26
had inferred it.

<details>
<summary>Original framing and resume instructions, kept for the record</summary>

**Status 2026-09-13:** downloaded, profiled, and measured on three of four axes.
MTP works and is worth 1.87x; decode is 74.57 tok/s, which is the fast tier's
speed at 4.3x its memory; the PLE must be offloaded explicitly or the prefill
guard rejects every prompt; and it **cannot be used concurrently** — 3 evictions
in 6 calls at a median 63.5 s per swap, against the ~20 s that retired Coder-Next.
The weights of both models do fit together; the two KV caches do not. See §26,
including the correction that the load refusal was a *pinning* artifact and that
oMLX's pool already implements LRU eviction, pinning and idle TTL.

#### How to resume this arm

Everything needed is on disk; nothing below depends on session context. State that
survives a reboot: the checkpoint (99.0 GiB, downloaded), the three `fn-*`
profiles (stored server-side, verify with `python3 profiles/apply.py --set
flashnext --dry-run`), and `mtp_enabled: True` + `qwen4_ple_ssd_offload: True` on
the base model. The pinned lineup preloads itself at oMLX startup.

Two blockers, both needing a human decision rather than more work:

1. **The proxy has not been restarted since the `fn-*` entries were added to
   `proxy/litellm-config.yaml`.** LiteLLM reads that file at startup, so
   `bench/taskbench.py` cannot address the model and its phase self-skips with
   this message. Unblock with
   `docker compose -f proxy/docker-compose.yml restart litellm`.
2. **The ~8 h paired code suite has not been authorised.** It is confirmatory
   (the vendor claims 91.9 vs 90.3 on livecodebench v6), so the agentic axis
   should decide whether it is worth the GPU hours at all.

Then, once the proxy serves `Qwen3.8-Flash-Next-oQ4e-mtp:fn-agent`:

    # agentic axis only, ~45 min, skips the probe and the code suite
    SKIP_RESIDENCY=1 SKIP_ACCURACY=1 \
      nohup bash bench/flashnext_overnight.sh > results/flashnext_overnight.log 2>&1 &

    # everything, including the ~8 h code suite
    SKIP_RESIDENCY=1 \
      nohup bash bench/flashnext_overnight.sh > results/flashnext_overnight.log 2>&1 &

The script makes room by unloading the lineup, records it in a temp file, and its
EXIT trap reloads it and re-verifies the shipped profiles. Do **not** route this
model through `bench/set_variant.py`: its `RESET` sets `mtp_enabled: False` and
knows nothing of `qwen4_ple_ssd_offload`.

Results already on disk: `flashnext_residency.json` (plus `_refused.json`, the
pinning refusal), `fn_mtp_check.json`, `fn_decode_baselines.json`,
`fn_thinking.json`, `tp_flashnext_batch.json`. There is no `tp_flashnext.json`:
oMLX's bench task returns a bare `error` for single-stream requests on this
engine, which is why §26's decode figures come from `timed_chat` and are not
comparable to `tp_mtp`. `fn_thinking_plumbing_27b.json` is a harness self-test
against the 27B, not a result about Flash-Next.

Everything below is the original framing, kept because it is what the measurement
was designed against.

`Jundot/Qwen3.8-Flash-Next-oQ4e-mtp`, 106.3 GB on disk. 125B total / 6B active
plus a 51.2B hashed n-gram table (the PLE) and a 4B Lightning MTP head. Same
uploader as the oQ4e fast tier, so the build method is held constant (A2b) and
only the model changes.

FINDINGS §22 dismissed this checkpoint as "a different architecture, not a
scaled-up Qwen3.8". That is still true and is now the reason to test rather than
to skip: `qwen4_exp` is 3-of-4 layers Gated DeltaNet with a fixed recurrent state,
so it does not obey §22's parameter-count arithmetic, and oMLX 0.6.4 ships a
vendored runtime for it with correct numerics (RMSNorm `1 + w`, hash seed 1234) —
unlike DFlash2 (B4), which was a silent no-op. The vendor's claimed gaps over the
shipped 27B are agentic, not general: DeepSWE 58.7 vs 42.2, JobBench 55.7 vs 33.4,
Toolathlon 73.5 vs 67.1, against LiveCodeBench v6 91.9 vs 90.3. That is exactly
the axis §14 found livecodebench blind to.

**Two traps, both recorded because they are silent.** (1) The PLE is held
*resident* by default: 111.6 GB against this machine's real admission ceiling of
115.448 GB — read from `health()["engine_pool"]["final_ceiling"]`, **not** the
107.52 GB §H quotes. oMLX only forces SSD offload when resident exceeds the
ceiling and mmap does not, so at 111.6 GB it does not fire and nothing warns you.
Offloaded it is 80.1 GB, which is the only figure that coexists with the shipped
8-bit's 28.2 GB. (2) The chat template defaults thinking **ON** at
`reasoning_effort: xhigh`, the inverse of every other model here, and
`accuracy.py` has no `chat_template_kwargs` path — so an unpinned "thinking off"
suite measures something else entirely.

**Settles:** whether an agentic-shaped architecture beats the 27B on the axis the
existing suites cannot see, at 80 GB and one eviction per model swap.
**Cost:** ~10 h unattended (`bench/flashnext_overnight.sh`). **Depends on:** the
residency gate passing first — A3b retired Coder-Next on that measurement alone.

</details>

> Reading the block above after the fact: its "real admission ceiling of 115.448
> GB ... **not** the 107.52 GB §H quotes" is the unit error §27 corrects. Both
> are `final_ceiling` — 115,448,725,504 bytes is 115.45 GB decimal and 107.52 GiB
> binary. §H was never wrong.

### A6b. QSA above 131k — DROPPED with A6

The long-context axis was scoped out of A6 and is now moot: A6 is closed and the
arm earned no tier, which was this item's stated dependency ("there is no point
measuring the context ceiling of a model that does not earn a tier"). Kept only
for the hypothesis, which would apply to any future `qwen4_exp` candidate: Qwen
Sparse Attention retrieves a **fixed** budget — 512 blocks, 2048 tokens —
regardless of prompt length, so recall at 262k is a constant amount of attention
spread over eight times the haystack, where the 27B's dense attention degrades
gracefully instead. B2 settled long-context quality for the 27B; nothing
equivalent exists for `qwen4_exp`, and nothing now needs to.

<details>
<summary>Original framing</summary>


The long-context axis was dropped from A6's scope, which leaves the likeliest
place this architecture regresses unmeasured. Qwen Sparse Attention retrieves a
**fixed** budget — 512 blocks, 2048 tokens — regardless of prompt length, so
recall at 262k is a constant amount of attention spread over eight times the
haystack, where the 27B's dense attention degrades gracefully instead. B2 settled
long-context quality for the 27B; nothing equivalent exists for `qwen4_exp`.

Consequences already applied: the `fn-*` profiles and their proxy entries cap
context at 131072, half the checkpoint's claimed 262144, and
`profiles/flashnext.py::validate()` rejects anything above that cap.

**Settles:** whether the claimed 262k is usable or nominal, and therefore whether
this model could ever take `qwen-long`'s work.
**Cost:** ~2 h (needle plus multi-hop at 131k and 262k, paired against B2's
figures). **Depends on:** A6 winning on its own axes first — there is no point
measuring the context ceiling of a model that does not earn a tier.

</details>

### A6c. On-demand model swapping — still open, and cheaper than §26 priced it

Raised while scoping A6: "we might look into a mechanism for swapping models on
request later". **A6 closing does not close this** — it removes the candidate
that would have ridden it, and it corrects the price downward:

    §26's figure    reload + 6000-token prefill   median 63.5 s
    §27's figure    the same cycle with a warm PLE      ~31 s

The PLE's SSD cache survives unload and reload, so 63.5 s was a cold-since-boot
number. ~31 s is an upper bound inferred from wall-clock across nine cycles, not
an instrumented measurement, so instrument it before designing against it.

§27 also sharpens what such a mechanism must do. The failure mode is not slowness
but **mutual refusal**: with two large models resident the prefill guard rejects
calls to *both*, including the one that was working. So a swap mechanism has to
unload, not merely co-load, and it has to wait for `current_model_memory` to drain
because an unload is not synchronous. The measured requirement is now:

  1. the swap is user-initiated, never router-initiated
  2. it unloads the displaced set explicitly and waits for the drain
  3. it reloads the displaced lineup afterwards — the BASELINE_FILE trap in
     `bench/flashnext_overnight.sh` is the working reference implementation
  4. it verifies the shipped profiles afterwards, because a restore that loads
     the weights but loses the profiles looks identical until a client calls it

**Settles:** whether a >100 GB replacement-only model can be part of this stack at
all, or whether 128 GB simply means one big model at a time. §27's answer is
currently the latter, on this engine and this ceiling.
**Cost:** ~1 day for a LiteLLM-side or wrapper-side mechanism. **Depends on:** a
candidate worth carrying, which A6 did not produce. Do not build it speculatively.


### A7. Two candidates, gated and profiled, UNMEASURED

Found by working backlog D3 step 1 against mlx-community and omlx.ai rather than
by downloading and benchmarking (§28). Both cleared the D3 gate on 2026-09-17 —
`bench/candidate_gate.py`, results in `results/candidate_gate*.json`:

| | resident | pool w/ lineup | 16k serve | MTP | family |
|---|---|---|---|---|---|
| `Qwen3.8-27B-OptiQ-4bit` | 19.00 GiB | 67.65 | 11.2 s | none available | qwen3_5 |
| `gemma-4-31B-it-qat-OptiQ-4bit` | 21.88 GiB | 70.53 | 15.7 s | none | gemma4 |
| *(Flash-Next, for contrast)* | 72.65 GiB | 101.50 | **HTTP 400** | yes | qwen4_exp |

Both serve a realistic prompt with the pinned lineup still resident, which is
the check A6 failed. Profiles are stored, served and proxied (`opt-*`, `g4-*`)
and wired to **no role alias** — they are addressable by `bench/taskbench.py`
and by nothing else.

**OptiQ is not a 4-bit build.** `optiq/metadata.json`: `achieved_bpw` 5.14, 260
modules at 8 bits and 237 at 4, allocated by `kl_divergence_vs_reference`
against `Qwen3.8-27B-bf16`. So the honest comparison is 19.0 GiB against the
shipped 8-bit's 28.2, not against oQ4e's 16.0. Its claim is A2b: the leaderboard
progression is 8-bit 64.0, RTN 4-bit 59.8, oQ4e 61.4, OptiQ **65.3** on
mmlu_pro, which if it replicates is the first build to beat the 8-bit on
reasoning at two thirds of the size. `oq4e.py` records A2b answered negatively
once already (p = 0.582) for a weaker allocation, so this re-opens it rather
than repeating it.

**gemma-4-31B is the only candidate whose claimed gap clears the resolution
floor**: mmlu_pro +11.0 against a floor of ~6 (§28). It is also the first
non-Qwen family here — top_k 64 not 20, a tokenizer ~9% denser on code (18,196
tokens against 16,663 for the identical corpus), and a template defaulting
thinking OFF rather than ON.

**Settles:** whether either earns a tier. **Cost:** the reason arms first —
that is where both claims live — paired against `acc_ship_reason`.
**Depends on:** nothing for the reason arms. The agentic comparison depends on
D1 continuing, since taskbench still shows puml/drawio/toolchain at 100% for
both existing arms and cannot yet resolve the ~5 points these will show.

### A7c. Reason arms measured — OptiQ retired, gemma-4-31B advances

Run 2026-09-17, `bench/a7_reason.sh`, full write-up in FINDINGS §29.

**OptiQ: retired.** mmlu_pro 61.2 measured against the leaderboard's 65.3 — it
did not replicate. Indistinguishable from oQ4e (pooled p = 0.797) and below the
8-bit (pooled p = 0.026), while being 3.1 GiB larger than oQ4e and having no
speculative decoding at all. Strictly dominated; there is no configuration in
which it is the right choice. A2b is now answered no twice, with two different
allocation methods.

**gemma-4-31B: advances.** mmlu_pro 73.0 vs 64.0 (p = 0.00006) and gsm8k 97.4 vs
93.6 (p = 0.00088), both clearing the measured 2.9-point floor, at 21.88 GiB
against the 8-bit's 28.20. Its apparent mathqa collapse (27.0 vs 43.8) is a
harness artifact: re-running 40 failures with a 1500-token budget made 27 correct
and left 13 still truncated, with **zero genuinely wrong**.

**Code axis measured 2026-09-17 and it wins decisively** (FINDINGS §30):
livecodebench **73.4 vs 59.8, +13.6, p = 0.0**, mbpp 85.2 vs 80.4 (p = 0.003),
humaneval 95.1 vs 93.9. Pooled p = 0.00000. That is more than 3x the measured
~4-point floor and the largest margin any candidate has produced here, in the
opposite direction from all of them. D5 check clean: empty_pred 0 on all three
suites, so this is not another §29 artifact.

**And it costs 1.8x the decode.** 18.5 tok/s at 16k against the 8-bit's 34.0,
prefill 617 against 793. gemma has no MTP head and no side-car, so it competes
unaccelerated against an incumbent whose 34.0 is its own 17.3 baseline doubled
by a drafter. This is the first candidate that is genuinely better at the work
and genuinely slower at it; every previous one was faster and worse.

Still open, in priority order:

1. ~~**The agentic axis**~~ **measured 2026-09-17 (FINDINGS §31): no regression,
   weak directional evidence for gemma.** Genuine task failures 0 for gemma
   against 2 for the 8-bit; per-attempt pairing 2 to gemma, 0 to the 8-bit,
   McNemar p = 0.50. Not significance — six one-way flips would be needed — so
   this clears gemma of harm without crediting it a win.
2. ~~**Instruction-following**~~ **the §29 flag does not reproduce.** Against
   the five negative-constraint tasks, including the two where the correct
   action is to call no tool at all, gemma scored 2/2 on all five and the 8-bit
   failed one. The mathqa behaviour was a format instruction under D5's starved
   budget, not general disregard for constraints.
3. ~~Context above 131072~~ **measured (FINDINGS §32): the ceiling is 65k.**
   6/6 retrieval at 65,215 tokens; 131k and 262k both REFUSED by the prefill
   guard with the machine to itself, on a ~54.6 GB single-chunk attention
   transient rather than on KV. Profiles corrected to the verified 65536 and the
   `g4-long` verification arm deleted rather than lowered. So gemma cannot take
   `qwen-long` and is an ADDITION, not a replacement. The additive lineup was
   then gated and works: four models, 70.53 GiB, 9/9 alternating 16k calls, no
   evictions.

   **Bisected 2026-09-17 (FINDINGS §33): the ceiling is probabilistic, not a
   number.** The guard admits ~127k (124,938 actual tokens) and retrieval works
   at 116k — depth 0.95 returned the needle exactly — but admission at that
   length is unreliable, 1/3 against 6/6 at 65,536, because the predicted peak
   sits within a few GB of the ceiling and moves with residual KV. Cap stays at
   65,536, the largest length never refused. **That answers `build`: gemma
   cannot hold whole-repo contexts dependably.**
4. Whether the speed cost is acceptable per role. The defensible reading is
   that quality-dominated, latency-insensitive roles can afford 0.54x decode
   and cannot afford -13.6 livecodebench — the inverse of the rule that gave
   oQ4e the speed-dominated work — but that is a decision, not a measurement.

**One flag, from §29.** gemma ignored "Answer with just the letter" on every
mathqa item. Harmless under a generous budget, directly harmful in an agent loop
where "do X and nothing else" is the normal shape of a request. Measure it in
taskbench, not in an accuracy suite.

### A7b. The OptiQ MTP side-car cannot be activated — CLOSED

`Qwen3.8-27B-OptiQ-4bit` ships `optiq/mtp.safetensors` (314 MB) and oMLX
advertises "MTPLX side-car detected but not imported. Import it to merge the MTP
head into the checkpoint index." It cannot be imported:

    POST /admin/api/models/{id}/import-mtplx
    -> 400 Missing required runtime contract: .../mtplx_runtime.json

That file is not in the repo, and not in **any** mlx-community OptiQ repo
checked (gemma-4-26B-A4B, KAT-Coder-V2.5, Qwen3.5-9B, gpt-oss-20b, Ornith-1.5;
two of them ship `optiq/mtp.safetensors` and none ship the contract). The
README does not mention MTP. oMLX's own implementation is compiled, so the
schema is not recoverable, and fabricating a speculative-decoding runtime
contract is how §23's DFlash2 silent no-op happened. Closed on that basis.

The failure mode is well-behaved, unlike B4's: oMLX **rejects**
`mtp_enabled: True` for this model outright rather than accepting it and
no-op'ing, so a profile cannot claim MTP it does not have. The failed import
left `model.safetensors.index.json` untouched (2,180 entries, 0 `mtp.*`).

Consequence for A7: OptiQ competes against the 8-bit's 1.97x and oQ4e's 1.73x
with no speculative decoding at all. Re-open only if mlx-community publishes
`mtplx_runtime.json`.

---

## B. Serving and speed (oMLX)

### B1. Prefix-cache hit rate under a real opencode session

FINDINGS §10.1 makes this the highest-value measured lever remaining: concurrency
is prefill-bound, cold prefill is ~12.6 s at 16k against ~3.5 s warm, and
`POST /admin/api/cache/probe` can measure hit rate against real traffic. The
unknown is what opencode actually achieves across a working session, where the
system prompt is stable but tool results keep landing mid-context.

The notes contribute the actionable half: **shared prefixes only work if they are
byte-identical across calls**, and reordering a system prompt silently busts the
cache. The two halves have very different scheduling costs, so they are split:

**B1a — measure the hit rate** with the probe during a real session (~1 h of
elapsed session time, wave 2 step 6). This is the part FINDINGS ranks #1 by value,
and it was held back from wave 1 for one reason only: it needs a live working
session, which is the scarce resource here. It is not expensive so much as
unschedulable — so it is no longer *scheduled*, it is *instrumented*: probe at the
start of the next live session and every ~45 min through it, whatever that session
happens to be about. Two things to reconcile it against: B2 saw 99.3% reuse on
repeats, and B1b established that the rolling part of the prefix (date, cwd) sits
in the tail. The open question B1b could not close — whether the injected skills
list reorders between sessions — is what this settles.

**B1b — audit the prefix sources** (~30 min, code read only, wave 1). Check the
opencode system prompt, skills and Spec Kit scaffolds for anything
nondeterministic: timestamps, cwd, tool-list ordering, injected dates. Any one of
those turns a shared prefix into a unique one, which would make B1a's measurement
moot before it is taken. Cheap, needs no session, and can invalidate the expensive
half — so it goes first.

**ANSWERED 2026-09-12: 86.7% on real opencode traffic.** A `build` agent task
(glob → read → edit a Python file) across 5 requests averaging 7,088 prompt
tokens reused **30,720 of 35,440** tokens. Note 30,720 is exactly 15 × 2,048 —
the hit rate is the block arithmetic below, not a coincidence. The ~4,700 cold
tokens are partial trailing blocks plus genuinely new tool output.

So FINDINGS' highest-ranked open lever resolves favourably: agentic loops are
**not** paying cold prefill repeatedly. At ~7k tokens per request an agent clears
three blocks comfortably, and the date/cwd rollover B1b identified sits in the
tail where it invalidates at most one block.

`cached_tokens` survives the LiteLLM hop intact, so the same measurement works
whether opencode talks to oMLX directly or through the proxy.

**Original partial answer, 2026-09-11, arrived sideways via C9.** The instrument is
`bench/cache_probe.py` (`mark` / `report` / `prefix`). It reports the **delta**
between two marks rather than the cumulative `cache_efficiency` on
`/admin/api/stats`, which read 81.4% over 729 requests but is contaminated: this
server has served ~40 h of benchmark traffic, which is pathologically repetitive
and inflates the figure. Only traffic between two marks is attributable.

**The structural finding, which caps B1a before it is taken: the prefix cache
operates in 2,048-token blocks and only caches whole ones.** Measured directly —
a 4,523-token prompt reuses exactly **4,096** tokens on repeat (two blocks, 91%),
and a 22-token prompt reuses **zero, always, no matter how often it is sent**.
So the achievable hit rate is bounded by `floor(tokens/2048)*2048/tokens`, and
short calls are structurally uncacheable. Any agent making many small requests
is not paying cold prefill *repeatedly* so much as paying it *unavoidably*.

That reframes the question B1a should ask. Not "what hit rate does opencode
get?" but "what fraction of opencode's requests even clear one block?" — because
below 2,048 tokens the cache is not slow, it is absent.

**Still outstanding:** the measurement against genuine opencode traffic. Every
window captured on 2026-09-11 read 0.0%, correctly: all of it was this repo's
own short test traffic, and the session driving the work ran on a cloud model,
so it generated no local requests at all.

**Settles:** whether agentic loops are paying cold prefill repeatedly.
**Note:** the 5.8× figure in the notes is a claim about other workloads, not a
measurement of this one — treat it as a hypothesis.

### B2. Long-context quality above 136k — done

`qwen-long`'s advertised 262,144 window is now verified rather than assumed:
15/15 needles across five depths and 3/3 multi-hop at 261,826 prompt tokens, with
no depth penalty. Details in FINDINGS §8; the superseded run is kept as
`results/longctx_quality_uncalibrated.json`.

The fix was not the new constant the plan called for. The old data fits
`tokens = 0.1653855 * chars + 1796` exactly, so a fixed ~1,800-token overhead sits
under a constant per-character rate and *no* single ratio holds across lengths
(5.74 chars/token at 65k, 5.97 at 262k). The harness now measures the line at run
time from two probes, lands within 8 tokens of a 261,824-token target, and will
stay correct when A1 changes the tokenizer underneath it.

**Byproducts worth keeping:** the first measured 262k cold prefill (18.7 min at
234 t/s, against 533 t/s at 65k and 372 at 131k), and 99.3% prefix-cache reuse on
repeats — evidence that feeds B1a.

### B3. Variance floor for sampling changes — parked

§8 could not separate the temperature effect from run-to-run variance, so every
sampling A/B since is uninterpretable at fine granularity. One repeat of
`livecodebench` at an unchanged temperature establishes the churn baseline.

**Cost:** ~6 h, which is why it has not been done. **Parked:** un-park the first
time a sampling A/B lands inside the noise and the answer actually matters.

### B4. DFlash — parked

`dflash_draft_model` needs a purpose-built block-diffusion draft checkpoint and no
local model qualifies: the MTP drafter is `qwen3_5_mtp`, and `Qwen2.5-Coder-3B-8bit`
has a different tokenizer. oMLX reports `dflash_compatible: true`, so the option is
real, but it needs a third-party download and supervision. Expect MTP-like
behaviour: a single-stream win at best, and this setup already has one.

### B5. Decide whether to build the Ollama / GGUF lane at all

The only real reasons are Unsloth's dynamic GGUF quants and access to GGUF-only
checkpoints (A4). It is a second serving stack to maintain, with its own model
store, and it does not share oMLX's KV cache or profiles. Decide explicitly rather
than drifting into it.

**Depends on:** whether A4 is worth running.

---

## C. Routing — LiteLLM as the unified interface

Decision already taken in the notes: the proxy runs on the Mac, since that is
where the models are. Fronting local plus work-sanctioned cloud APIs.

### C1. Stand up LiteLLM with Postgres

Not `pip install litellm` with a bare config. Virtual keys require a database, and
without one **`max_budget` silently does not enforce** — the proxy keeps serving
past the cap, with a single startup warning as the only signal and no per-request
error. So it is the Docker + Postgres sidecar path, or else cap spend at the
provider and stop pretending the proxy is doing it.

Pin a specific image tag: a malware advisory was reported against particular
1.82.x releases.

**Blocks:** C2–C6.

**Done 2026-09-11.** `proxy/docker-compose.yml` + `proxy/litellm-config.yaml`,
Postgres 16, LiteLLM pinned **by digest** (`ghcr.io/berriai/litellm@sha256:a3715fa…`,
resolved from `v1.100.1`) rather than by tag — a tag can be repointed, which is
the 1.82.x advisory's actual failure mode. One virtual key, `local-only`, scoped
to a `local` access group; a call to `gpt-4o` through it is refused 403 before
it reaches any router, which pre-verifies C2's guardrail. Four findings, none of
which were in the plan:

1. **`max_budget` does not enforce for local models even WITH Postgres.** The
   backlog's premise — no database, no enforcement — is true but incomplete.
   Local deployments carry no per-token pricing, so `spend` stays exactly 0.0
   and no budget can ever trip: measured 0.0 against a 5.0 cap after live
   traffic. Postgres is necessary and not sufficient. Token counts *are* logged
   per request in `LiteLLM_SpendLogs`, so usage tracking works; only the budget
   mechanism is dormant, and it stays dormant until C2 brings real pricing.
2. **The config lives in `proxy/` in this repo**, which closes the §G question.
   Not dotfiles: `omlx launch opencode` overwrites files there through the
   symlink, and that hazard should not be extended to the proxy. `proxy/.env`
   is gitignored and 0600.
3. **The corporate TLS proxy was a non-event** (C7). The root CA was already extracted on this
   machine, and `docker pull` works through the inspection proxy untouched. The
   CA is mounted into the container's trust directory but is **inert**: nothing
   in a local-only configuration makes an outbound TLS call, since oMLX is plain
   HTTP on the host. It is wired now and only becomes testable at C2. Do not
   record it as verified.
4. **Postgres is not published to the host.** Nothing outside the compose
   network needs 5432, and this machine roams.

### C2. Make the local/cloud boundary an alias, not a failover

By default a router-level fallback fires **even when the calling key is not
permitted to call the fallback model directly**. A local→cloud spill is therefore
the default behaviour rather than an opt-in, which is the wrong default when work
data is in context.

Target design: two access groups (`local`, `cloud-approved`); a local-only virtual
key for anything touching client work; a separate hybrid key where cloud failover
is deliberate; and an explicit `coder-cloud` alias instead of a silent chain.

**Depends on:** C1, and on the open question of which cloud provider is sanctioned.

### C3. Role aliases: `coder`, `reasoner`, `fast`, `diagram`

One alias per role, not per model, so swapping the 27B for Coder-Next is a
one-line change and every tool follows. Note what this touches here: the four
current model IDs are profile-suffixed (`Qwen3.8-27B-8bit:qwen-code` and friends)
and opencode pins each agent to one in
`~/Development/private/dotfiles/opencode/opencode.json`. Aliasing means that file
changes too — and `omlx launch opencode` overwrites it through the symlink, so
that hazard now applies to the alias mapping as well.

`qwen-long` does not map cleanly onto the four role names. **Decided: fold it into
`reasoner` with a context override**, rather than adding a fifth `long` role. The
262k window is then a per-request parameter instead of a model identity, which
keeps the alias table at four and means a context change does not require a
routing change. The cost is that `speckit-wide`'s current pin becomes a parameter
on `reasoner` rather than a distinct target — acceptable, since the profile still
exists underneath and can be addressed directly when needed.

**Depends on:** C1.

**Done 2026-09-11, and the context override needed a flag nobody had named.**
All four aliases resolve and answer through the scoped key. But the first
implementation of the `qwen-long` fold was **silently inert**, and it is worth
recording exactly how, because the failure mode generalises:

`context_window_fallbacks` is **reactive, not proactive**. It fires only when
the backend raises a context-window error; `max_input_tokens` in `model_info`
is otherwise inert metadata that the router never pre-checks. Measured by
shrinking `reasoner`'s declared window to 2,048 and sending 4,059 tokens: HTTP
200, served by **qwen-reason**, no fallback, no log line. oMLX accepted the
prompt happily — its real limit is 131k — so no error existed to react to, and
the override would therefore never have fired in production either.

The fix is **`router_settings.enable_pre_call_checks: true`**, which makes the
router filter deployments by declared window *before* dispatching. Re-measured
with it on: the same 4k request was served by **qwen-long**. Note the test was
deliberately run by shrinking the declared limit rather than by sending a real
131k prompt — it exercises the same mechanism for about 1/50th of the prefill.

**Carried caveat, unresolved by the fix.** This is not a pure context bump:
`qwen-reason` runs thinking on with an 8k budget, `qwen-long` runs thinking
**off** at temp 0.3. So crossing 131k silently trades reasoning depth for
window. Right for whole-repo analysis, wrong for a hard architecture question
that happens to carry a big context. If that bites, the honest fix is a fifth
`long` role, not a cleverer fallback.

**`diagram` is the alias most likely to move.** Diagram work *has* a
deterministic verifier, so by the wave 1 rule it should be `cn-code` at
best-of-4. It points at the 27B today only because nothing can sample N yet.
That one line is the visible payoff of C9.

### C4. Verify tool calls and streaming survive the proxy hop

Needs a real opencode task with file edits, not a chat completion. Coder-Next has
its own function-call format (A1), so this is the item most likely to fail
quietly: a mangled tool call degrades into plausible-looking prose rather than an
error.

**Depends on:** C1, and pairs with A1.

**DONE 2026-09-12 — full task, both paths.** A real `opencode run` task with
file edits (glob → read → edit) succeeded **direct** against oMLX and **through
the proxy via the `coder` role alias**, with tool calls, multi-turn tool results
and streaming all intact. The quiet failure C4 exists to catch — a mangled call
degrading into plausible prose — did not occur on either path.

Wiring, chosen deliberately: the proxy was added to `opencode.json` as a
**second provider** (`role/*`) rather than by repointing the existing `omlx`
provider. Repointing would make a container a single point of failure for the
daily driver, where oMLX itself is always up. Both paths now work, and the role
aliases are usable without betting the editor on the proxy.

**Slice done 2026-09-11 — PASSED, and the quiet failure did not happen.**
`bench/proxy_check.py toolcall` sends a file-edit-shaped tool (`path`,
`old_string`, `new_string`) direct and through the hop, and grades four things:
a tool call exists at all, the name matches, the arguments parse as JSON, and
the required keys are present. Both models passed both paths with
**byte-identical arguments**, including `Qwen3-Coder-Next-6bit:cn-agent`, whose
own function-call format was the stated risk. The check deliberately compares
direct against hop rather than testing the hop alone: both arms failing is a
model problem and must not be logged as a proxy fault.

What this does **not** cover, and why C4 proper still stands: one call, no
streaming, no multi-turn tool result feedback, and no real file on disk. It is a
gate on building C3 and C9, not a substitute for a real opencode task.

### C5. Measure proxy latency overhead vs. hitting oMLX directly

Also: raise timeouts hard. Defaults fire during prefill on long local contexts,
and this setup's cold prefill at 16k is ~12.6 s with 262k contexts available.

**Cost:** ~30 min once C1 is up.

**Measured 2026-09-11: +21.1 ms median per request** (direct 295.3 ms, proxy
316.4 ms, 9 interleaved repeats at `max_tokens=1` so the hop is not drowned by
decode). Reported as a fixed delta rather than a ratio, because a ratio taken on
a 1 s request says nothing about a 20 min one.

**This does not straightforwardly clear C6, and the ratio is why.** The overhead
is 0.167% of the 16k cold prefill and irrelevant there — but 2.1% of a 1 s
request, and `bench/throughput.py` is built to resolve single-digit-percent
differences at prompt lengths starting at 1,024. So moving the harness behind
the proxy is safe for long-prompt work and **contaminating for short-prompt
work**. C6 should either stay direct below ~16k or subtract a measured constant.

**Tail, not just median:** 1 request in 9 cost +373 ms rather than +21 ms.
Almost certainly the spend-log flush to Postgres. Irrelevant to a human, but it
matters to C9, where N=4 means four chances to draw that tail on every call.

Timeouts raised to **2400 s** (`request_timeout`). The default fires during
prefill, and the measured worst case here is the 262k cold prefill at 18.7 min
(1,122 s). `num_retries` is set to **0** on purpose: a retry against a local
single-GPU server does not route around a fault, it re-queues the same
20-minute prefill behind itself.

### C6. Move the benchmark harness behind the proxy

Every call then logs model, latency and token counts in one place. Worth doing
only after C5 shows the overhead is small enough not to contaminate throughput
numbers — `bench/throughput.py` measures single-digit-percent differences.

**Depends on:** C1, C5.

### C7. Mac-specific setup gotchas

- Inside Docker, oMLX is `http://host.docker.internal:8000/v1`, not `127.0.0.1`.
- Corporate TLS proxy: the container cannot see the keychain, so the `NODE_EXTRA_CA_CERTS`
  approach used for LM Studio does not carry over. Mount the root CA and set
  `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`.

Fold into C1 rather than tracking separately.

### C8. Hermes access over the tailnet — parked

`tailscale serve` exposes :4000 on the tailnet without opening the port on other
interfaces. Hermes gets its own key with the tightest budget of the three. Use the
OpenAI SDK pointed at the proxy — calling `litellm.completion()` in-process
bypasses the proxy entirely, so no virtual key check and nothing in the admin UI.
Plan for the Mac sleeping: Hermes needs a graceful failure path, not a hang.

**Depends on:** C1, C2.

### C9. Best-of-N orchestration in the proxy — CLOSED, measured and rejected

**Closed 2026-09-11 on a head-to-head measurement** (`results/c9_decide.json`,
`bench/c9_decide.py`). Everything below is kept because the reasoning was sound
and only the arithmetic was wrong; the conclusion is at the end of the section.

Was the gate item of wave 2. §15 is the strongest result wave 1 produced and it is
unreachable from every tool currently in use, because opencode only ever sends
N=1 — which is exactly the regime where §14 applies ("latency instrument, not a
quality one") and §15 does not. The proxy is the natural place to fix that,
because it is the only component that sees a request and can turn it into several.

Shape: sample N candidates from one identical prompt, run a project-supplied
verifier command against each, return the first that passes. The prompt is shared,
which is the batching best case rather than §8's prefill-bound worst case — that
is why N=4 costs 1.60x wall-clock instead of 4x, and why the result still runs
1.32x faster than the 27B while recovering 22% of Coder-Next's failures.

**The hard part is the verifier contract**, not the sampling. A verifier is a
per-project command with a binary exit — `pytest`, `tsc`, a PlantUML render — and
something has to declare which one applies. That contract is shared with D1, which
needs the same runner for scoring rather than gating, so the runner is built once
and consumed twice.

**Depends on:** C1, and on D1's verifier substrate.
**Settles:** whether the wave 1 rule — *use the verifier where one exists* — is
something the toolchain can actually act on, or just a benchmark result.

#### Built and measured 2026-09-11 — and §15's cost does not reproduce

`bench/verifier.py` (substrate, 21 unit tests) and `bench/bon_gate.py` (the
loop). The loop works end to end through the proxy, and the no-verifier path
refuses to sample N and escalates instead, which is §G's fallback question
answered in code: `Verdict` separates `ok` from `ran`, so "verifier rejected it"
and "verifier never ran" can never be confused, and only an `ok` verdict can
select a candidate. Picking blind is strictly worse than N=1 — it spends N times
the compute to choose at random.

**The cost result is the important one, and it is bad news that §15's 1.60x is
regime-dependent in a way nobody checked.** Measured through the real path:

| regime | N=4 cost vs N=1 | why |
|---|---|---|
| short prompt (~30 tok) | **3.08x** | nothing to share; near-linear |
| cold long prefix (~6k) | **2.13x** | prefill shared, but only partly |
| warm prefix, free-running | **4.84x** | straggler-dominated |
| warm prefix, equal token budget | **2.40x** | the true batching cost |

Three mechanisms, separated by measurement:

1. **The prefix cache works in 2,048-token blocks and only caches whole ones.**
   A 4,523-token prompt reuses exactly 4,096 tokens on repeat — two blocks, 91%
   — and a 22-token prompt reuses **zero, always**. So best-of-N's entire cost
   advantage depends on the prompt crossing block boundaries. This is also a
   real B1a result and is recorded under §B1.
2. **Prefix sharing inside one burst is partial and racy.** N=4 cold on a 6k
   prefix returned `cached=[0, 4096, 0, 4096]` — two of four raced to prefill
   the same blocks and duplicated the work. §15's "a shared prompt is the
   batching best case" is only half true: it is the best case *across* calls,
   not *within* one.
3. **Decode does not batch, because MTP is single-stream.** At equal token
   budget, aggregate throughput rises only 1.67x going from N=1 to N=4 while
   per-stream decode collapses **63 → 26 tok/s**. That is §8's finding arriving
   from the other direction: the 1.8x MTP win is 0.99x at batch 2+, so sampling
   N deliberately destroys the speedup that makes the sampler fast in the first
   place. **Best-of-N and MTP are in direct competition here.**

**One fix already applied.** The gate verifies candidates **as they arrive**
(`as_completed`) and returns on the first pass, rather than waiting for all N.
Wall-to-all-complete is set by the slowest candidate, and at temperature 0.7 the
straggler dominates — that is the whole 2.40x vs 4.84x gap. Measured improvement
on short prompts: N=4 1.84x → **1.75x**, N=8 3.04x → **2.76x**. The gain is
larger the more output length varies. **Honest limit:** this cuts time-to-answer,
not GPU cost — abandoned candidates keep decoding server-side, so `max_tokens`
remains the only real lever on the machine's bill.

**What this does not settle.** §15's *value* (22% of failures recovered at N=4)
is untouched — these prompts were too easy to discriminate, passing 4/4 at N=1.
Cost and value need re-pairing on genuinely hard problems before `diagram` or
`coder` are flipped to a sampled model. On present numbers the break-even is
much tighter than §15 implied: recovering 22% of failures for 2.4x wall-clock in
the warm case is a far weaker trade than for 1.6x.

#### Verdict: the 27B alone beats best-of-4, on both axes

Every cost figure up to this point — §15's 1.60x, and this repo's 1.75x / 2.40x
/ 4.84x — compares a model against ITSELF at a different N. That is not the
decision. The decision is between two things a request could be sent to, so both
were run head to head on 12 livecodebench problems Coder-Next had actually
failed, same verifier, same warm ~3.4k shared prefix:

| arm | recovered | wall-clock | correct answers/min |
|---|---|---|---|
| `fast` best-of-4 + verifier | **2/12** | 368.8 s | 0.33 |
| `coder` (27B) greedy, N=1 | **6/12** | 390.9 s | **0.92** |

The 27B recovered **three times as many** in essentially the same wall-clock.
Against §15's projection — best-of-4 landing 1.32x faster at ~56.0 against 59.8 —
the measured reality is *the same speed and a third of the correct answers*.
That is not a marginal trade to weigh, it is the reverse of the claim.

**The one confound runs against best-of-N, not for it.** The 27B arm hit repeated
prefill memory-guard rejections and needed retries, inflating its *time* (two
problems took 112 s and 122 s against a 14 s median). It cannot have affected its
*quality*: arm B ran at temperature 0.0, and B3 established this engine is
bit-exact there, so every retry returned identical text. A clean re-run therefore
only makes the 27B faster, moving best-of-4 from tying on speed to losing on it.
The verdict cannot flip, which is why no clean re-run was done.

**Why it fails here, mechanically.** Best-of-N and MTP compete for the same
silicon. The speculative-decoding win that makes the sampler fast is
single-stream only — 1.74x at batch 1, 0.99x at batch 2+ (§8) — so sampling N
destroys the very speed advantage that was supposed to pay for the extra
samples. Measured directly: per-stream decode collapses 63 → 26 tok/s from N=1 to
N=4 while aggregate throughput rises only 1.67x. On hardware without speculative
decoding the trade would look completely different; this is a property of *this*
setup, not of best-of-N.

**What survives.** `bench/verifier.py` stays — D1 needs exactly that runner for
render-success and XML-validity, and it has 21 tests. What is closed is
*sampling N to pick a winner*, not *verification*. `bench/bon_gate.py` and
`bench/c9_decide.py` stay as the record.

**Do not re-open unless** the lineup loses MTP (which would remove the
competition), or a verifier appears whose pass@N recovery is far above
livecodebench's 22% — a real project test suite might be. Both are testable with
what now exists.

---

### A8. `plan` runs on gemma-4-31B — wired 2026-09-17, **silently reverted, date unknown**

**Correction 2026-10-19:** the wiring never reached a commit. In the dotfiles history
`plan` is `qwen-reason` in every revision, and so is the live file. The likely cause is
the `omlx launch opencode` overwrite this item warns about. The "week of real `plan`
use" below therefore never happened. Re-decided in A10.

The one role the evidence unambiguously supports: reasoning-dominated
(+9.0 mmlu_pro, p = 0.00006), not latency-critical (0.54x decode is invisible on
a planning turn), and rarely above the 65k cap. `build`, `speckit`,
`speckit-wide`, `explore` and the small-model roles are unchanged.

Config is `~/Development/private/dotfiles/opencode/opencode.json`, symlinked to
`~/.config/opencode/`. Note `omlx launch opencode` overwrites it through the
symlink (§C3), so a launch will revert this.

`bench/verify_wiring.py` checks the thing B1b found had never been checked — that
a served profile is reachable, answers, does not leak monologue into content, and
does not declare more context than the model can serve. Run it after any config
change.

**What would reverse this:** a week of real `plan` use showing the 65k cap or the
1.6x wall-clock biting. That is the evidence a benchmark cannot produce (§24).

**Still not wired, deliberately:** `build`. +13.6 livecodebench is the largest
quality gain available here, and against it sit a 65k ceiling on whole-repo work
and 1.6x on every call. §33 settled the context half against it. The remaining
question is workload, not measurement.

---

### A10. The gemma round: is the speed objection dead, and can gemma serve 131k?

Raised 2026-10-19 from the first Hermes role assessments (backlog D7, homelab 088).
**Status: steps 0–3 done 2026-10-20 (§40, §41).** gemma-31B's cap raised to
122,880. `fast` moved to gemma-26B-A4B (`g4m-agent`). `coder` stays on the 8-bit.
Left: step 4 for `plan` (`g4-reason`) and `coder` (gemma-31B against the 8-bit;
the 8-bit has not yet run livecodebench on oMLX 0.7.1rc1's harness, §41).
- Step 0: the speed objection is dead. Gemma decodes at 1.15x / 1.12x / 0.94x the
  8-bit `plain` at 1k/4k/16k.
- Step 1: retrieval 5/5 at 98k, 122k and 131k. The guard admits up to 126,976 with
  `chunked_prefill` off and 151,552 with it on.
- Correction to the plan below: there is no separate prefill chunk size. The lever
  is the global `scheduler.chunked_prefill`, which affects every model.
- Disk-cache test done: with `ssd_cache_max_size` raised from 100 GB to 300 GB (kept),
  repeat questions at 122k take 3–19 s instead of ~8 min.
- Cap re-decided the same day: 122,880 (owner). 131,072 would need the global
  `chunked_prefill`.
- Engine was oMLX 0.7.1rc1. Unloading after a 100k+ request crashed it twice (§40).

**Why now: §30's main objection has been invalidated.**
- §30 kept gemma-4-31B as an addition partly on decode: "0.54x the shipped 8-bit",
  18.5 against 34.0 t/s at 16k.
- That 34.0 was the 8-bit on the `vlm_mtp` path, which §39 showed decoded greedily.
- Since 2026-10-12 the 8-bit runs `plain` at about 16.7 t/s. So the QAT gemma already
  on the Mac is plausibly the faster model now, as well as:
  - livecodebench +13.6 (73.4 vs 59.8, §30)
  - mmlu_pro +9.0 (73.0 vs 64.0, p = 0.00006, §29)
  - 6.9 GiB smaller (21.88 vs 28.20)
- Caveat: those are two different runs; step 0 puts them on one harness.

**What is left against it is context.**
- Above about 65k, the single-chunk prefill transient (~54.6 GB at 131k, §32) breaks
  the ceiling. The KV cache is not the problem.
- 65,536 is dependable. About 116k is admitted 1 time in 3 (§33).
- The transient is per chunk, so a smaller prefill chunk is an untested lever. It is
  unrelated to the `chunked_prefill` closed for the dense 27B.

**New builds the scout found** (Hermes assessed both on 10-19; both verdicts need
correcting, see below):
- `Jundot/gemma-4-26B-A4B-it-oQ6e-mtp` (21.3 GiB, MTP in the checkpoint): a fast-slot
  challenger to oQ4e (livecodebench 54.0, §19).
- `Jundot/gemma-4-31B-it-oQ8e-mtp` (32.3 GiB): **out of this round.** It has less
  memory headroom than the build that already fails above 65k, and its MTP path carries
  the §39 risk. Revisit only if step 1 fixes context.

**Steps, cheapest and most decisive first:**

| # | Step | Mac cost | Decides |
|---|---|---|---|
| 0 | `bench/throughput.py`: 8-bit `plain` against gemma-31B QAT, same harness, 1k/4k/16k, `gen_short` guard on. Doctor "profile samples" on `g4-code`, `g4-reason` | ~30 min | whether §30's speed objection is dead |
| 1 | `bench/ctx_bisect.py` for gemma-31B at the current oMLX, then again with a smaller prefill chunk. Retrieval at the admitted length with `bench/longctx_quality.py`: admitting is not attending (§33) | 1–2 h | replacement (131k) or addition (65k) |
| 2 | gemma-26B-A4B-oQ6e-mtp: download, `candidate_gate.py`, profile-samples check (does its MTP path honour the sampler? §39), decode with MTP against oQ4e | ~2 h + download | fast-slot contender or not |
| 3 | Paired n=500 runs for the surviving arms: livecodebench, mmlu_pro, sampled decoding. Leave mathqa out (§29) | overnight | the role change itself |
| 4 | Wiring: `plan` → `g4-reason` (re-do A8, committed this time); `coder` → `g4-code` if step 1 reaches 131k. `long` stays on `qwen-long` | an edit + `verify_wiring.py` | — |

**Run condition: an exclusive window.**
- Pause Hermes `scout-assess`.
- The owner stops opencode and Hermes chats for the duration.
- Benchmarks share the single llm_queue slot otherwise (§38), and the numbers would not
  be clean.

**The assessments to correct.** Hermes judged gemma-26B-A4B only against the
quality-slot bar ("pass") and carried the 31B's 65k ceiling over to a model with a
different memory profile. It called the 31B oQ8e "gate it" in order to "confirm 262k",
which is the wrong axis. `scout/assess.md` now compares against both slots and does not
carry one checkpoint's measured ceiling over to another.

**Settles:** whether the best-measured model on the Mac stays unwired.
**Cost:** steps 0–1 one afternoon, 2 an afternoon, 3 one night.
**Depends on:** nothing.

---

### A9. Two qwen3_5_moe 35B-A3B candidates measured and retired — see §34

Fetched to test whether a ~3.4B-active MoE could take a tier off the dense 27B:
gemma-class quality at 3-4x the decode. `oMLX` ships `qwen3_5_moe.py`, so both
load natively (no Flash-Next-style vendored patch).

| candidate | build | resident | decode | livecodebench |
|---|---|---|---|---|
| `Tiel-Coder-35B-A3B-MLX-oQ4e` | mixed-up eff 6.9 | 20.6 GB | 113.9 t/s | **24.2** |
| `Qwen3.8-35B-A3B-Distill-oQ8-fp16-mtp` | uniform 8-bit | 38.6 GB | 150.5 t/s (MTP 1.66x) | **41.6** |

**Both PASS the D3 coexistence gate** — the first since gemma to serve a 16k
prompt with the pinned lineup resident (Tiel pool 69.3 GB, Novaeon 87.3 GB). So
residency, which closed Flash-Next (A6), was not the problem.

**Both fail on code, decisively.** Paired McNemar vs the arms on disk: Tiel loses
to the 8-bit, gemma and oQ4e all at pooled p ≈ 0 (livecodebench 24.2, below
Coder-Next's retiring 43.2); Novaeon loses to the 8-bit p = 0.00003 and gemma
p ≈ 0. Speed (§34) and toolcall (Tiel 24/24, Novaeon 23/24) both looked strong
and neither meant code correctness — `toolcall` measures call *formatting*, the
§31 saturation biting in practice.

**Retired; lineup unchanged.** Checkpoints deleted (~58 GB reclaimed). The class
verdict — `qwen3_5_moe` 35B-A3B buys decode at a code-quality cost this stack
will not pay in unchecked roles — generalises, so re-open only for a specific
build with a *measured* code number at or above the 8-bit's, not another fast one.

**Settles:** the MoE-speed-play question for this generation. **Cost:** spent
(one evening + one overnight). **Depends on:** nothing further.

---

## D. Evaluation

### D3. A candidate-evaluation gate, derived from three retirements

Coder-Next (A1/A3b), the GGUF MoEs (§22) and Flash-Next (A6) were each retired
after the expensive measurement rather than before it. §27 and §28 make the cheap
checks explicit. Run these **in order**, and stop at the first failure:

1. **Read the leaderboard first.** `omlx.ai/benchmarks/intelligence?model=<name>`
   is free and often already holds the suite being contemplated. It answered A6's
   8 h code suite outright (§28). Treat scores as configuration reports, not
   rankings — and check `specprefill_enabled`, `repetition_penalty`,
   `reasoning_parser` and `enable_thinking` on any run before believing it.
2. **Compute what the suite can detect.** livecodebench at its full 1,054
   questions resolves ~6 points at 80% power. If the expected gap is smaller,
   the run cannot answer the question at any sample size and should not be
   commissioned. §28 has the arithmetic.
3. **Serve a realistic prompt while the lineup is resident.** Not "does it load"
   — Flash-Next loaded fine at 101.50 GiB and then refused every 16k call, taking
   the shipped model down with it. `is_pinned` and `ttl_seconds` are per-model
   settings, so recomposing the lineup for this test is free and needs no restart.
4. **Check `completion_tokens` on any throughput number.** §27's headline was a
   rate computed over two tokens. `bench/throughput.py` now flags it; nothing
   else does.
5. Only then spend the GPU hours.

**Settles:** how much of a candidate evaluation can be done before committing to
an overnight run. **Cost:** none, it is a checklist. **Depends on:** nothing.

### D5. The mathqa suite's generation budget scores reasoning models as zero

`bench/accuracy.py` passes no `max_tokens`, so each suite uses the oMLX budget,
and mathqa's is sized for models that reply with a single letter. gemma-4-31B
writes a derivation first and scored 27.0% where a 1500-token budget puts it near
76% (FINDINGS §29): of 40 failures re-run, 27 became correct, 13 were still
truncated, and none were wrong.

The failure is silent — the score simply comes back low, and it looks exactly
like a model that cannot do maths. This is the third scoring instrument here
found to mislead rather than fail, after `throughput.py` reporting a rate over a
2-token generation (§27) and taskbench grading diagram validity instead of
content (§27).

**CONFIRMED 2026-09-17, and it is not settable.** Across every arm ever stored
in `results/`, the longest mathqa response is **609 characters** — codernext 609,
gemma 597, oq4e 459, quant4 423 — while the same models reach 4,917 on mmlu_pro
and 2,119 on gsm8k. That is a hard per-suite cap of roughly 150 tokens. It is
invisible from the API: `sampling.max_tokens` is 32768 globally, per-model
`max_tokens` is unset, and `/admin/api/bench/accuracy/queue/add` takes an
undocumented body with no budget field. It lives inside oMLX's suite definition.

It went unnoticed for the whole project because every Qwen arm answers mathqa in
a single character and never approaches the cap. It only bites a model that puts
its reasoning before its answer, and then it decapitates the answer.

Note the cap alone is not fatal: `codernext_reason` hit 609 and still scored
46.8, above the 8-bit. The damage needs cap AND answer-last ordering together.

So the options are narrowed to two, neither needing more measurement:

  - **Drop mathqa from the `reason` group** and say why, so it stops producing
    numbers nobody can interpret. mmlu_pro and gsm8k both discriminate and
    neither is capped.
  - **Keep it as an instruction-following probe rather than a reasoning one**,
    which is what it now actually measures: whether a model answers in the
    demanded format under a tight budget. If kept, it must never be pooled with
    the other two — FINDINGS §29 shows pooling it reverses the verdict.

**Settles:** already settled; what remains is the decision above.
**Cost:** minutes. **Depends on:** nothing.

### D6. Episode-shaped agentic evaluation — taskbench cannot go further

FINDINGS §31: after three rounds of hardening, thirteen unfailable tasks fixed,
and checks added for counts, geometry, edge connectivity, overlap and forbidden
constructs, **both the shipped 8-bit and gemma-4-31B still sit at 95% per
attempt**. The verifiers are no longer the limit. Single-turn, single-artifact
tasks are simply saturated at this model size, and adding more of them cannot
resolve the ~5-point differences candidates actually show.

What would discriminate is a different harness shape, not more tasks:

  - a real scratch workspace on disk, seeded per episode
  - several tool calls required in sequence, where a later call depends on an
    earlier result (taskbench's `toolchain` does two turns; an episode needs
    five to ten)
  - at least one injected failure per episode — a tool returning an error, a
    file that is not where the search said — to measure recovery rather than
    happy path
  - graded on the END STATE of the workspace, not on any single response, which
    keeps the verifier deterministic without constraining the route

**Settles:** whether an agentic difference between two strong models can be
measured here at all. Until it exists, the agentic axis can detect a
Coder-Next-sized regression (-16.6) and nothing subtler, which is worth saying
out loud whenever it returns a null.
**Cost:** ~2 days. **Depends on:** nothing, but only worth building if a
candidate's wiring actually hinges on the agentic axis — gemma's currently does.

### D7. Model scout: know the lineup, its roles, and say so on quiet days

The scout is now a daily Hermes Agent no-agent cron job (`scout/hermes-cron.sh`,
`docs/SCOUT.md`). Observations from its first runs, 2026-10-17/18:

**What went wrong**
- **A quiet day looks like a broken scout.** On 2026-10-18 the 07:30 run succeeded with
  `Status: silent (empty output)`: nothing was new since a manual run the day before, so
  `--quiet` printed nothing. From the phone, that cannot be told apart from a scout that
  never ran.
- **It proposes models already on the Mac.** `prefilter()` treats a repo as local only
  when its exact name, or an entry in `setup.json` `source_repos`, matches. The 2026-10-16
  export has `source_repos: []`. So `Jundot/Qwen3.8-27B-oQ6e-fp16-mtp` and
  `Jundot/gemma-4-31B-it-oQ8e-mtp` were reported as promising, while four Qwen3.8-27B builds
  and a gemma-4-31B build are local. A new quant of a base model already in the lineup is a
  quant comparison (A2b) at most, not a new model.
- **No concept of roles.** `scout.toml [lineup]` names hand-kept tiers (quality, fast,
  additive, embedding, reranker). Clients call role aliases instead: `fast`, `coder`,
  `reasoner`, and the embed/rerank pair in `proxy/litellm-config.yaml`; also
  `ROLE_ALIASES` in `bench/doctor.py`. The report says only "alongside" or "replaces
  quality". It never says which role a candidate would compete for, or against which
  incumbent and evidence.
- **The setup goes stale silently.** `setup.json` is exported by hand on the Mac, and
  `SETUP_STALE_DAYS` is 30.

**The work**
1. **Quiet-day line.** In `--quiet` mode with no news, print one line, e.g.
   `Model scout: nothing new since <last_scan> (<n> repos scanned)`. A run that errors
   still exits non-zero, as today.
   - Put this in `model_scout.py`, not the wrapper. The deployed wrapper is a copy in the
     agent's `scripts/`, and only the clone updates, via `git pull`.
   - Better: make the wrapper a two-line `exec` of `scout/hermes-cron.sh` from the clone,
     so wrapper changes deploy themselves, and re-copy it once.
2. **Roles in the export.** `export` reads the LiteLLM role aliases (role → oMLX model
   id) and writes them to `setup.json` as `roles`. It also fills `source_repos`, or
   records why oMLX does not report them. `[lineup]` is then derived from `roles`, or
   references them, rather than being maintained by hand.
3. **Family-level "already local".** Compare `family()` and the parameter class against
   every local model.
   - A new build of a local base model is reported as "variant of <local id>
     (role <r>)" and demoted to `consider`.
   - The exception is when it brings a signal the incumbent lacks (DWQ, MTP on a model
     without it, a build method not measured in A2b).
4. **Role-aware report.** Each candidate states the role it competes for, the incumbent,
   the size delta, and the finding that put the incumbent there. For example: "competes
   for `coder` (incumbent Qwen3.8-27B-8bit, §19); 21.3 GiB, fits alongside".
5. **Freshness.** Refresh `setup.json` now. Then either run `export` on a schedule on
   the Mac (launchd, committing only when it changes), or make the scan put a stale-setup
   warning in the report itself past a few days.

**Status 2026-10-18:**
- Done: items 1, 3 and 4, and the refresh in item 5.
- Item 2 changed course. The roles are the four profile-level ones (`coder`,
  `reasoner`, `agent`, `long`), kept by hand in `scout.toml [roles]`. Reading them
  from LiteLLM does not work: `agent` and `long` have no alias. A test fails if an
  incumbent stops being served by LiteLLM. `source_repos` stays empty because oMLX
  does not record it; `base_key()` matching replaces it.
- Still open:
  - a scheduled `export`. Today the stale warning fires at 30 days.
  - item 3's "build method not measured in A2b" exception. Only DWQ and missing MTP
    count today.
- Next: a Hermes agent job reads `scan --format json` and judges role fit. See
  homelab-proxmox.

**Settles:** whether the daily scout is a signal the owner reads or noise to be muted.
**Cost:** an evening. **Depends on:** nothing; needs the Mac for items 2 and 5.

**Done when:**
- A no-news run delivers the one-line message.
- `scan --since 3` no longer reports Qwen3.8-27B or gemma-4-31B variants as new
  models.
- Every reported candidate names a role.

### D4. Make `bench/report.py` honour `gen_short`

`bench/throughput.py` now records `gen_short` per row and prints a SHORT
GENERATION block (§27), but `report.py` still reads `decode_tps` unconditionally,
so the cross-model table will keep rendering rates derived from 2-token
generations as if they were measurements — which is exactly how §26's decode
claim survived review. Either suppress flagged values or mark them in the table.

Also re-summarise the stored `results/tp_*.json` from their own `raw` once the
flag exists, the way §8 did after the batch-row bug, so history is consistent.

**Settles:** whether the reporting layer can still launder a bad measurement.
**Cost:** ~30 min. **Depends on:** nothing; the flag is already in the payload.

### D1. Convert profile benchmarking to task-shaped metrics

The current harness scores public suites. That was right for settling the accuracy
floor, and it is wrong for judging whether a model does *this* job better:
render-success rate, XML validity, tool-call format adherence, tokens-to-first-useful-diff.

Two constraints carry over from FINDINGS:

- A 500-sample suite resolves nothing under ~2 points (§8). Task-shaped metrics
  need to be paired and per-item, like `bench/paired_compare.py`, or they will not
  be interpretable either.
- oMLX uploads any accuracy suite result with ≥100 questions to `omlx.ai`,
  including a machine hash and model settings, with no opt-out (§11). A
  purpose-built task harness sidesteps this entirely, which is a second reason to
  build one — and a sharper one now that A2b's oQ arm has accepted an upload
  because no paired alternative existed.

**One runner, two consumers.** D1 scores (paired, per-item, for model comparison)
and C9 gates (binary, per-project, to accept a candidate), but both need the same
thing underneath: run a declared verifier command against a candidate and report
pass/fail plus a reason. `bench/best_of_n.py::value()` is the working prototype —
it already samples N and buckets failures per item — but its verifier is
livecodebench-shaped and needs generalising before either consumer can sit on it.
Build the runner first; D1 and C9 are then thin.

#### BUILT 2026-09-12 — `bench/taskbench.py`, 27 tasks, four deterministic verifiers

    puml       plantuml -failfast2 -checkonly, exit 0
    drawio     well-formed XML AND an mxGraphModel root with real mxCells
    pycode     asserted behaviour executes, exit 0
    toolcall   name correct, arguments parse, required keys present

All four are `bench/verifier.py` objects, so the runner C9 used as a gate is
reused here as a scorer. `run` scores one model, `compare` does McNemar per
category and pooled.

**The first version measured nothing, and that is the main lesson.** Sixteen
tasks, both tiers scored **16/16**, and the tool duly reported "no significant
difference" — a statement about the tasks, not the models. A task set everything
passes is exactly as uninformative as one nothing passes. A hard tier was added
(C4 with boundaries, sequence with nested blocks, explicit draw.io geometry and
swimlanes, LRU cache, word-break, stdin contracts, quote-escaping in tool args)
and every hard task has a demonstrated reference answer, so difficulty was
verified rather than assumed.

**Three traps found while building it, all of which would have produced
confident nonsense:**

1. **Temperature confound.** `coder` (qwen-code, temp 0.7) against `fast`
   (oq-agent, temp 0.3) showed the 4-bit model *winning* 27/27 to 24/27. That is
   a sampling artifact: `force_sampling` means the profile's own temperature
   applies and cannot be overridden per request, so comparing role aliases
   compares profiles, not models. Re-run like-for-like at 0.7 (`qwen-code` vs
   `oq-code`) the direction reverses to **98% vs 91%**. Compare profiles when
   choosing a profile; compare equal sampling when choosing a model.
2. **Network-dependent verdicts.** A model that emits
   `!include https://raw.githubusercontent.com/...` makes the verdict depend on
   GitHub. One task failed on a **404** with perfectly good diagram source.
   Those are now INCONCLUSIVE rather than FAIL, and the prompts ask for the
   bundled `<C4/C4_Container>` include instead.
3. **The local PlantUML rejects `par ... and ... end`** (1.2026.7, exit 200)
   while accepting `par ... end`, `group`, `opt` and `break`. Textbook-correct
   source that will not render here. The task was rewritten to avoid it — a task
   no candidate can pass measures as little as one every candidate passes — but
   the quirk is worth knowing before trusting any PlantUML output on this box.

**Capability and reliability are reported separately** because they disagree.
Collapsing repeats to "passed at least once" asks whether a model *can* do the
task; per-attempt rate asks whether it *does*. 8-bit vs oQ4e: identical on
capability (27/27 vs 26/27), **98% vs 91%** on reliability.

**Honest limit, printed by the tool itself:** 27 tasks resolve differences of
roughly 15 points and up. The 7-point reliability gap above is **unmeasured, not
absent** — one discordant pair, p = 1.0. This is §8's lesson in a new place: the
harness removes the omlx.ai upload and the suite-score blindness, and it does not
manufacture statistical power. Scaling to ~100 tasks is the obvious next move if
a real decision depends on it.

**Enables:** honest comparison in A1, A2, and every stage of E — E3's reward
signal is the `puml`/`drawio` verifier unchanged. **Blocks:** nothing now; C9 is
closed.

### D2. Re-cost best-of-N sampling against a verifier — contested, measure before adopting

The notes propose sampling N and keeping whatever passes a hard verifier
(compiles, tests pass, diagram renders), on the grounds that continuous batching
makes this nearly free locally. This repo's concurrency measurements argue the
opposite on the current model: 8 concurrent requests bought **1.38× aggregate
throughput for 4.9× p95 latency**, and batch 2 was *worse* than batch 1, because
the requests were prefill-bound. Best-of-N with a shared prefix is a friendlier
shape than that test's — N completions off one prompt is the batching best case,
not the worst — so the finding does not settle it, but neither does the notes'
assumption.

Promoted into **wave 1** despite being contested, because under a quality-ceiling
objective this is *the* test-time-compute lever and it costs an hour. Wherever
there is a hard verifier, the only local cost of sampling N is watts. If it works
it plausibly raises the ceiling more than the model swap does; if it does not, it
closes the largest untested claim in the source notes. Either outcome is worth an
hour.

**Settles:** whether "spend speed on quality" is available here at all.
**Do this:** N completions from one identical prompt at N = 2, 4, 8, measuring
wall-clock to all-N-complete, not aggregate tok/s. Run it on whichever model wins
A1. **Cost:** ~1 h. **Related:** B1b — same prefix-sharing mechanism, so a
nondeterministic prefix would depress this too.

---

## E. Diagram DSL fine-tuning

Prioritized because PlantUML and draw.io supply something rare: **a deterministic
verifier**. The renderer exits 0 or it does not; the XML validates or it does not.
No hand-labelling. The existing render skills already are that verifier.

Target a **4–9B model, not the 27B**. A small model tuned on two DSLs should beat a
general 27B at them and costs almost nothing to keep resident.

### E1. Spike the corpus — ANSWERED 2026-09-12

**Volume is fine. Requests do not exist. Provenance is the interesting part.**

The corpus is `~/Documents/EY/Cases`: **64 PlantUML and 65 draw.io sources**
across 5 of 19 cases. Run through D1's verifiers:

| | pass | rate |
|---|---|---|
| PlantUML | 46/64 | **72%** |
| draw.io | 65/65 | **100%** |
| total | **111/129** | 86% |

**The request half is not recoverable**, which is exactly the risk this spike was
written to catch: **0 of 129** diagrams have a same-named markdown alongside,
only ~12% of filenames are referenced from any markdown at all, and the two git
repos carry 30 commits touching diagram files between them. There are 111
verified answers and essentially no questions. Any training set has to have its
prompts **reconstructed**, not recovered.

**The sources are themselves AI-generated**, which constrains what training on
them can achieve:

- **Filter, do not train on all 129.** The 28% of PlantUML that does not render
  is not "lower quality", it is wrong, and imitating it teaches syntax errors.
  Costing nothing to exclude is the whole reason this domain was chosen.
- **SFT cannot exceed its targets.** So the direction matters: the E lane targets
  a 4–9B model and these came from something much larger, which makes it
  distillation rather than a circle. Training the 27B on its own output would
  gain nothing.
- **GRPO is immune to the concern**, because the reward is the renderer's exit
  code rather than imitation. Seed quality has to be good enough to get syntax
  into the right neighbourhood, not good enough to be the ceiling.
- **72% render rate IS the target.** E2 predicted LoRA fixes syntax adherence
  fastest; this is the evidence that it is the failure mode worth fixing.

**The real risk is diversity, not provenance.** 111 examples from 5 cases, median
11,424 characters of source — long-form generation from very few architectural
patterns. Overfitting to five clients' house style is the likely failure mode.

**Gate verdict: PARKED 2026-09-12, on diversity.** E2, E3 and E4 are not
scheduled. Volume clears the bar and the verifier makes filtering free, but 111
examples drawn from five cases is too narrow a base to tune on: the model would
learn five clients' house patterns rather than the two DSLs. Manufacturing the
missing prompts would not fix that — it would multiply the same narrow set.

**Un-park when the corpus broadens through normal work**, which costs nothing to
wait for: every future diagram made with the render skills is another verified
sample, and `bench/taskbench.py`'s verifiers already measure the render rate this
would aim to improve. Re-run the count when the case spread is meaningfully wider
than five. The 72% PlantUML render rate stands as the benchmark to beat.

### E1 (original framing). Spike the corpus — a feasibility question, not a commitment

**Out of scope for wave 2**, so E2, E3 and E4 remain unscheduled with it and the
whole lane is parked until wave 3 at the earliest. Nothing about it got harder;
it lost to the verifier items on a fixed live-time budget.

The gating item, and scheduled as a **spike only**: count how many usable
(request → valid source) pairs actually exist across the diagram skills'
successful renders. Unknown until attempted: whether there is enough volume, and
whether the "request" side has to be reconstructed rather than recovered.

**Gate: E2 and E3 are not scheduled until this answers.** If the corpus turns out
thin, the honest next move is to generate more by using the diagram skills on real
work, not to start training on what is there.

### E2. Stage 1 — SFT LoRA

Via `mlx-lm-lora`, which supports LoRA, DoRA and QLoRA at 4/6/8-bit. Expect this
to fix syntax adherence quickly; that is the failure mode prompting plateaus on.

**Depends on:** E1.

### E3. Stage 2 — GRPO against the renderer

Tooling exists and is real: `mlx-lm-lora` ships GRPO, GSPO, Dr. GRPO and DAPO;
`mlx-tune` has a stable `GRPOTrainer` with multi-generation plus reward and a
`VLMGRPOTrainer` for vision; MLXSmith does GRPO with verifier-based rewards.

Reward shape: renders cleanly (binary); no renderer warnings; layout heuristics
(crossings, aspect ratio); semantic match scored by the 27B as judge.

**Depends on:** E2, and on D1 for the render-success metric.

### E4. Serving the tuned model alongside the coder

Same blocker as A3: a second model is a different variant signature. Resolve the
serving story before tuning finishes, or the result sits on disk unused.

**Depends on:** A3's reload measurement.

---

## F. Underspecified — need a decision before they can be scheduled

### F1. Vision

Qwen3.8-27B is vision-capable and neither main use case exploits it. The plausible
one is diagram work — feeding a rendered PNG back as a critic signal, which would
also give E3 a richer reward than "exit 0". But no use case has actually been
stated, and the engine already runs the VLM path (`vlm_mtp_*` is what MTP is
configured through), so the capability is not the blocker. **Needs a use case
before it is a backlog item.**

### F2. Local document parsing

Docling and MinerU as a possible LlamaParse replacement. The motivation is
presumably keeping documents local rather than quality or cost — worth stating
which, because it changes the bar the replacement has to clear.

---

## G-bis. Open, from the workflow A/B (§25)

- **Is §25's result sampling or signal?** One run each, temperature 0.7, on a
  model at 63.3% livecodebench. Zero defects against two is wide but it is one
  sample. **Repeat runs each way settle it and are cheap** — this is the single
  highest-value open question about how to use the setup, and the only one where
  more data changes the answer rather than confirming it.
- **If it is signal, which mechanism?** Three untested candidates: artifact
  dilution in front of a context window that did not need compressing; task
  decomposition orphaning cross-cutting decisions; or executable standards being
  the whole effect. Only the third is available to both workflows, which makes
  it the cheapest to test — require constitution-asserting tests in a Spec Kit
  run and see whether its defect count moves.
- **Cost is unmeasured.** The §25 comparison lost it to a marker that only
  watched the proxy while the run went direct. Any repeat must instrument both
  paths, or force one.

---

## G. Open questions

- **Which cloud provider is actually sanctioned *for programmatic API use*.**
  Partly answered 2026-09-12, and the qualifier is the whole point.
  `~/.factory/cache/org-managed-settings.json` shows an EY-managed enterprise
  Factory tenancy with a maintained policy — **28 explicitly allowed models**, 11
  blocked (every "fast" variant), `allowAllFactoryModels: false`, cloud session
  and wiki sync off, Droid Shield on, 90-day retention, revision 38. So frontier
  access exists and is sanctioned **through droid, which enforces that policy**.
  It does not unblock C2 for two independent reasons: Factory exposes an *agent*
  (`droid daemon` is websocket/IPC session spawning, `droid exec` runs an
  agentic loop) and **not** a chat-completions endpoint, so there is nothing to
  point `api_base` at; and the policy is client-enforced, so a key consumed by
  LiteLLM would bypass the allowlist, blocklist, Droid Shield and retention
  entirely. `allowFactoryRouterByok: false` reads as the org having already
  decided about this pattern. Still open: a provider sanctioned for API use with
  credentials issued for that purpose — a procurement question, not a technical
  one.
- **Whether the llama.cpp MoE-streaming PR has merged** — only matters if B5 goes
  ahead.
- **Where the LiteLLM config lives:** this repo, dotfiles, or its own. The notes
  reference companion `litellm-config.yaml` and `SETUP.md` that are not in this
  repo yet. **Now urgent** — C1 is wave 2 step 8, so this needs answering before
  the first live session, not after.
- **What the verifier contract looks like** (C9, D1). Per-project command with a
  binary exit is the shape; where it is declared, and what happens for a project
  that has none, is not decided. The fallback matters most: with no verifier, C9
  must degrade to plain N=1 on the 27B rather than picking a candidate blind.
- ~~What replaces `qwen-long` in the role-alias scheme~~ **decided:** folded into
  `reasoner` with a context override (C3).
- ~~Whether Coder-Next has an MTP-style drafter available at all~~ **answered: no**
  — no `nextn`/`mtp` keys, zero matching tensors, nothing upstream or on the Hub
  (A1). Every Coder-Next number is unaccelerated, and it wins anyway.

---

## H. Settled — do not re-open

Carried from FINDINGS so the source notes do not get re-litigated.

**Closed by measurement in this repo** (§3, §5, §8): ANE prefill; TurboQuant 4-bit
KV; the in-memory hot cache (measured *worse* on every number, at a cost of 20 GB —
an earlier draft's "largest remaining win" claim was wrong); `chunked_prefill` (no
effect); `specprefill` on a dense checkpoint (no effect, revisit only for A1);
raising the concurrency limit; and `qwen-code` temperature 0.7 vs 0.3.

**"Verify whether oMLX exposes draft-model / MTP speculative decoding" — already
done, and the notes' reasoning is wrong.** MTP is shipped and validated: 17.3 →
34.0 t/s at a 16k prompt, output-neutral across 1,664 questions. But it does *not*
come from an in-checkpoint MTP block. The 8-bit checkpoint declares MTP layers
whose tensors are absent, so `mtp_enabled` is unusable; the win comes from a
separate `Qwen3.8-27B-MTP-8bit` drafter wired through `vlm_mtp_draft_model`. The
consequence matters for A1: the win is a property of having a matching drafter,
not of the model family.

**Also settled:** the 1.8× is single-stream only (1.74× at batch 1, 0.99× at batch
2+); `qwen-reason`'s thinking budget stays on (without it, one hard prompt in eight
burned all 32,768 tokens and returned nothing); MTP is exclusive with logits
processors, so no profile may set `repetition_penalty` ≠ 1.0, `presence_penalty` ≠
0.0, or `guided_grammar_enabled`.

**That last rule is narrowed by §26, not overturned.** It is a property of
**drafter-based** speculation, which is how the 8-bit gets its win
(`vlm_mtp_draft_model`), and it still applies there as written. On a checkpoint
whose MTP head ships inside the checkpoint, `presence_penalty: 1.5` measured at 1%
of decode while MTP was still worth 1.87× — so the rule must be cited with its
mechanism rather than as a blanket ban on penalties.

**Not viable locally on 128 GB:** GLM-5.2 and Kimi-class models. GLM-5.2 needs
~239–245 GB even at 2-bit and degrades below Q4, making a 256 GB Mac Studio the
entry ticket. Expert streaming does not rescue it: streaming cost scales with
*active* parameters, and GLM-5.2 is 40B active (~13 GB of expert reads per token at
~2.6 bits/param), so several GB come off SSD every token — sub-2 tok/s optimistically.
Prefill is worse still, since every prompt token routes independently and touches
essentially all experts; a 20K-token Spec Kit context would drag most of 239 GB
through the SSD before the first output token, which is the worst possible shape
for agentic coding. The sane use of GLM-5.2 is remote, as a planner over its native
Anthropic API at roughly 1/6 frontier cost, with local doing execution.

**Do not attempt the naive version:** plain mmap with macOS swap handling overflow
produces documented thrashing, kernel panics and page-out death spirals — one
measurement had mmap at 0.017 tok/s. `--cpu-moe` / `--n-cpu-moe` buy nothing on
unified memory; there is no separate VRAM pool to spill from.
