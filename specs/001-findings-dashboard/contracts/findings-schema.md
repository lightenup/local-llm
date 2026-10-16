# Contract: Findings File Schema (TOML)

**Feature**: 001-findings-dashboard
**Audience**: whoever curates `findings/*.toml` (human or agent) and the
generator that consumes it.

One file per finding. Filename stem must equal the finding's `id`.
Read with stdlib `tomllib`; validation rules in [data-model.md](../data-model.md).

## Field reference

```toml
# findings/qwen38_8bit_mtp.toml

id        = "qwen38_8bit_mtp"          # required, ^[a-z0-9][a-z0-9_-]*$, == filename stem
title     = "MTP speculative decode ships the 1.8x decode win"   # required
status    = "shipped"                  # required: shipped | on-trial | retired | dead-end | open
models    = ["Qwen3.8-27B-8bit"]       # required (may be []), model ids concerned
section   = 4                          # required, FINDINGS.md section number
section_title = "MTP speculative decode — the big win, shipped"  # optional, drift-checked
rationale = ""                         # required in practice for retired/dead-end
updated   = 2026-09-22                 # optional

[[metrics]]
name      = "decode @ 16k prompt"      # required, unique within finding
unit      = "tok/s"                    # required
direction = "higher"                   # required: higher | lower
headline  = true                       # optional, default false
tooltip_what     = "Sustained decode throughput at a 16,384-token prompt, single stream."
tooltip_how      = "bench/throughput.py, code_python context, 128-token generations, warmup quick."
tooltip_evidence = "Deterministic sampling; 4 prompt lengths per run; see §4 for the A/B."
source_file = "tp_baseline.json"       # required unless the metric is "no measurement"
source_path  = "summary[?pp=16384].decode_tps"

# optional comparator — also resolved, never hardcoded
baseline_label  = "baseline, MTP off"
baseline_source_file = "tp_baseline.json"
baseline_source_path = "summary[?pp=16384].decode_tps"
```

Notes on the flat `tooltip_*` / `source_*` keys: TOML tables would nest them,
but flat keys keep each `[[metrics]]` block a flat, grep-friendly list of
strings — the curation surface is edited by hand and by agents, and flat
blocks are the least error-prone shape. The generator maps
`tooltip_what/how/evidence` → the tooltip triple and `source_file/source_path`
→ the Source (research.md R1).

## Path expressions (`source_path`)

Three constructs, chainable (research.md R3):

```text
raw.result.verified_tokens          # dotted keys
summary[0].decode_tps               # integer index
summary[?pp=16384].decode_tps       # predicate filter — LAST match wins
results[?benchmark=humaneval].accuracy
```

- Values in `[?key=value]`: bare numbers, or bare/quoted strings.
- Must resolve to a scalar (number or string).
- Last match wins because older `acc_*.json` files append the run's own
  records after the server-side history (see `bench/report.py::own_results`).

## Dead-end / no-measurement example

```toml
# findings/deadend_hot_cache.toml

id      = "deadend_hot_cache"
title   = "In-memory hot cache"
status  = "dead-end"
models  = ["Qwen3.8-27B-8bit"]
section = 8
rationale = "Measured worse on every number at a 20 GB cost (FINDINGS §8); do not re-open."

# no [[metrics]] block at all — renders with a "no measurement" marker
# (closed by measurement cited in the rationale, or by argument).
```

## Validation summary (exit 1, no site written)

Parse errors, missing/typed-wrong required fields, bad `status`/`direction`,
duplicate or mismatched `id`, `section < 1`, `source_file` escaping
`results/`, unparsable `source_path`, duplicate metric names — every problem
is listed on stderr with file + field. Unresolvable-but-well-formed sources
are **stale** (exit 2, site still written), not validation failures.
