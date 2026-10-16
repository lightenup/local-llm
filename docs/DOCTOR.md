# The doctor: `bench/doctor.py`

A layered self-check that the local LLM stack is doing what it is configured
to do. It exists because this stack's dominant failure mode is **configured
and inert**: something enabled, no error, and no effect (FINDINGS §24). Every
instance so far was found by measuring, not by reading config.

## Status: manual, by decision (2026-10-12)

The doctor runs **only when someone runs it**. Nothing schedules it, calls it,
or watches its result. There is no LaunchAgent, cron job, CI job, metric or
alert. Keeping it manual was a deliberate choice for now.

What that costs: a failing check stays invisible until the next run. It
happened twice in October 2026. The engine layer would have flagged the
`reason` profile's engine fields (§38), but nobody ran it. The 8-bit decode
floor had been failing for weeks while measuring the wrong decode path (§39).

**So run it:**

- after any change to oMLX, LiteLLM, the profiles, the queue or the power setup;
- after an oMLX or LiteLLM upgrade;
- before a long or unattended job (overnight runs, benches);
- when something "feels off" (slow, looping, refusals).

If it is ever automated, the pieces already exist: the LaunchAgent pattern in
`proxy/launchd/`, the homelab Prometheus that already scrapes this Mac on
`:9464`, and Alertmanager to Pushover. Run `--quick` on a schedule and export
pass/fail as metrics. The full run uses the GPU, so it would have to go
through llm_queue and run rarely.

## Running it

```bash
python3 bench/doctor.py                 # all layers, ~1-2 min, uses the GPU
python3 bench/doctor.py --quick         # config + cheap probes only, seconds
python3 bench/doctor.py --layer engine  # one layer (repeatable: --layer a --layer b)
```

- Exit code 0 means all checks passed, 1 means at least one failed.
- Each line is tagged `obs` (observed behaviour) or `cfg` (read
  configuration). They are not equal evidence: `cfg` passing means the
  setting is there, not that it works.
- Every run overwrites `results/doctor.json` (`{at, results[]}`). It is not a
  history. Fault-injection runs overwrite it too, so re-run after one.
- `--quick` skips anything that generates on the GPU: decode floors and
  sampling checks.

## Layers and checks

Layers run in this order: the cheapest and most fundamental first.

| layer | what it guards | checks |
|---|---|---|
| **host** | the Mac under everything (§37) | sleep prevented during inference (`inference_awake` running, or AC sleep 0), AC wake-on-network off, on AC power, lid open, no thermal-emergency sleep in 24 h |
| **engine** | oMLX | server responds; profiles match `profiles/definitions.py` (and carry no engine fields); 8-bit base engine variant equals `SHIPPED_VARIANT` (`plain`, §39); no retired profiles served; *full only:* each profile in `SAMPLING_PROFILES` actually samples (4 identical requests, at least 2 distinct, §39); oQ4e speculative decode above its floor (§23) |
| **queue** | `proxy/llm_queue.py` (§38) | queue reachable; the LiteLLM container's `OMLX_BASE_URL` matches the compose file (reports LIVE/SOAK); *full only:* a LiteLLM request is granted by the queue |
| **proxy** | LiteLLM, the contract every client uses | reachable; each role alias answers; access group refuses out-of-group models; `enable_pre_call_checks` on (§16); router fallbacks empty (C2) |
| **harness** | opencode | provider env vars visible to non-interactive shells (§24); every agent's model is declared; Spec Kit commands are stock |

Knobs at the top of the file: `MTP_FLOORS`, `SHIPPED_VARIANT`,
`SAMPLING_PROFILES`, `ROLE_ALIASES`, and the env vars `PROXY_BASE` (default
`:4000`) and `LLM_QUEUE_BASE` (default `:8001`).

## Reading a failure

| failing check | usual meaning | where to look |
|---|---|---|
| sleep prevented during inference | `$LAUNCHD_PREFIX.inference-awake` not running | `launchctl print gui/$(id -u)/$LAUNCHD_PREFIX.inference-awake`, FINDINGS §37 |
| no thermal-emergency sleep in 24 h | the Mac hit a thermal emergency | `pmset -g log \| rg Thermal`, §37 |
| profiles match their definitions | profile drift, or engine fields copied into a profile | `python3 profiles/apply.py` re-applies, §38 |
| 8-bit base engine variant | base drifted from `set_variant.py` | `python3 bench/set_variant.py apply plain`, §38/§39 |
| profile samples: … (1/4 distinct) | a decode path ignores the sampler, so agents will loop | §39 |
| speculative decode active | the drafter loaded but is not accelerating | §23 |
| LiteLLM upstream matches compose | compose edited without recreating the container | `cd proxy && docker compose up -d` |
| role alias answers | LiteLLM or oMLX down, or a key or alias problem | `docker logs litellm`, `~/.omlx/logs/server.log` |

## Adding a check

1. Pick the layer and add `d.check(layer, name, kind, fn)`, where `fn()`
   returns `(ok, detail)`. A check that raises counts as a fail.
2. Prefer `kind="behaviour"`: observe the effect, not the setting. Use
   `"config"` only when the effect cannot be observed cheaply, and say so.
3. Put anything that generates on the GPU after the `if d.quick: return` of
   its layer.
4. **Fault-inject before trusting it.** Make the condition false and confirm
   the check fails. The doctor has caught itself being inert this way
   (the harness env-var check, §24). Best of all, run it against the real
   failure before fixing it, as the sampling check was (§39).
5. Reference the FINDINGS section the check comes from in a comment.

## Related

- `profiles/apply.py --verify-only`: the profile-definition check, which
  `engine` calls.
- homelab-proxmox `scripts/healthcheck/llm-router-doctor.py`: the homelab
  gateway's own doctor. It is separate and does not call this one.
