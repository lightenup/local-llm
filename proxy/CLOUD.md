# Enabling a cloud tier (backlog C2)

Everything except a provider name and a credential is already decided. This
file is the checklist; the structure it refers to is commented out in
`litellm-config.yaml` so that enabling it is an edit, not a design exercise.

**Nothing here is enabled.** The proxy is local-only and has no cloud
deployment, no cloud key, and no fallbacks.

## The one thing that makes this dangerous

LiteLLM fires a **router-level fallback even when the calling key is not
permitted to call the fallback model directly**. So a local→cloud spill is the
*default* behaviour rather than an opt-in, and the access group alone does not
prevent it. That is why `router_settings.fallbacks` and
`content_policy_fallbacks` are pinned empty and must stay that way.

The single enabled fallback is `context_window_fallbacks`, mapping `reasoner` to
`qwen-long`. It is local→local, so it cannot cross a trust boundary. Do not add
a cloud entry to it.

## Structure

Two access groups, two keys, and an explicit alias:

| | group | reachable models |
|---|---|---|
| `local-only` key | `local` | the seven local profile IDs and the four role aliases |
| `hybrid` key | `local`, `cloud-approved` | the above plus `coder-cloud` |

A cloud deployment carries `access_groups: ["cloud-approved"]` and **never**
`local`. Callers opt in by naming `coder-cloud` explicitly. Verified already:
the existing local-only key returns **403** for a model outside its group,
before any router logic runs.

## Steps

1. **Confirm the provider is sanctioned for programmatic API use.** This is the
   actual blocker and it is not technical. See "What Factory settles" below.
2. Uncomment the `coder-cloud` block in `litellm-config.yaml`, set
   `model:` to `<provider>/<model>`, and add `CLOUD_API_KEY` to `proxy/.env`.
3. Restart: `docker compose restart litellm`.
4. Create the hybrid key, scoped to both groups:

   ```bash
   curl -X POST http://127.0.0.1:4000/key/generate \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"key_alias":"hybrid","models":["local","cloud-approved"],
          "max_budget":25.0,"metadata":{"purpose":"deliberate cloud use"}}'
   ```

5. **Verify the boundary before using it**, in both directions:

   ```bash
   # local-only key must be REFUSED the cloud alias
   curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
     -H "Authorization: Bearer $LOCAL_ONLY_KEY" -H 'Content-Type: application/json' \
     -d '{"model":"coder-cloud","messages":[{"role":"user","content":"hi"}]}'
   # expect 403

   # a local alias must NOT silently reach cloud when the local model is down
   docker compose stop        # or unload the local model
   curl -s ... -d '{"model":"coder", ...}'
   # expect an error, NOT a cloud completion
   ```

   The second check is the one that matters and the one most likely to be
   skipped. An access group does not prevent a fallback; only an empty
   fallback list does.

6. Budgets become meaningful at this point and not before: local deployments
   carry no per-token pricing, so `max_budget` reads exactly 0.0 against any cap
   no matter how much traffic runs (measured — see FINDINGS §16). A cloud
   provider brings real pricing, which is when the Postgres requirement starts
   earning its place.

## What Factory settles, and what it does not

Investigated 2026-09-12. `~/.factory/cache/org-managed-settings.json` shows an
EY-managed enterprise tenancy with a maintained policy: **28 explicitly allowed
models**, 11 explicitly blocked (every "fast" variant),
`allowAllFactoryModels: false`, `cloudSessionSync: false`,
`wikiCloudSync: false`, Droid Shield on, 90-day retention, revision 38.

**So frontier access already exists and is already sanctioned — through
Factory's droid client, which enforces that policy.**

Two reasons it does not unblock this file:

1. **There is no model API to point a proxy at.** Factory's programmatic surface
   is an *agent*: `droid daemon` is websocket/IPC for spawning agent sessions,
   `droid exec` runs a full agentic loop with tools and permissions. Neither is
   an OpenAI-compatible chat-completions endpoint, so there is nothing for
   `api_base` to address.
2. **The policy is client-enforced.** Even given an API, a key consumed by
   LiteLLM would bypass the allowlist, the blocklist, Droid Shield, retention
   and org visibility — the proxy would happily call a model the org blocked.
   Note `allowFactoryRouterByok: false`: the org has specifically disabled
   bring-your-own-key routing through Factory, which is the nearest thing to a
   direct statement about this pattern.

**Consequence for C2's value.** The reason to build a cloud tier was frontier
capability, and that already exists via droid with controls intact. What remains
is unified routing and logging, which is a convenience rather than a capability.
Weigh that against the governance risk before pursuing a sanctioned API
provider; the structure is ready either way.
