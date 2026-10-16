# Homelab LAN exposure — read after a restart

This proxy was opened to the LAN on **2026-09-16** so the homelab Proxmox gateway
(spec `064-llm-router`) can chain to it. If you just restarted this Mac, start here.

## What changed and why

`docker-compose.yml` publishes LiteLLM on `0.0.0.0:4000` instead of `127.0.0.1:4000`,
so the gateway LXC on `<lan-subnet>` can reach it. **oMLX is untouched — it stays on
`127.0.0.1:8000` and is reachable only by this LiteLLM.** Only this proxy is LAN-exposed,
and it enforces keys and access groups. Your local loopback clients (opencode providers,
`bench/*.py`) are unaffected — `0.0.0.0` still binds loopback.

## Bring it back after a restart

Since 2026-10-10 both **Docker Desktop** and **oMLX** are macOS login items, so they start
once you log in (FileVault: nothing starts before login). LiteLLM runs in **Docker Desktop
only** — Colima must not run a second copy (see `docker context show`; it should say
`desktop-linux`). If either app did not come up:

```bash
open -a Docker          # containers are unless-stopped; they return once Docker is up
open -a oMLX            # auto_start_on_launch:true -> server comes up on 127.0.0.1:8000

# verify
lsof -nP -iTCP:4000 -sTCP:LISTEN                          # want *:4000
lsof -nP -iTCP:8000 -sTCP:LISTEN                          # want 127.0.0.1:8000 (NOT *)
curl -s -m5 http://<mac-lan-ip>:4000/health/liveliness  # "I'm alive!"
```

Confirm oMLX re-pinned the two aliased checkpoints (`Qwen3.8-27B-8bit`,
`Qwen3.8-27B-oQ4e-mtp`) in its UI — a cold boot unloads them, and an unpinned model that
evicts will trip the gateway's timeout into a false failure.

## The LXC key

A virtual key `key_alias: homelab-lxc-064-llm-router` (scope `models: ["local"]`) was minted
for the gateway. It survives restart (its hash is in the `proxy_litellm-pgdata` volume), but
its **plaintext cannot be recovered** — only its hash is stored. It goes into the homelab
Ansible vault as `mac_litellm_key`. If the plaintext was lost, re-mint:

```bash
set -a && . ./.env && set +a
curl -s -X POST http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"models":["local"],"key_alias":"homelab-lxc-064-llm-router-v2"}' | python3 -m json.tool
```

Your own local key (`LITELLM_LOCAL_KEY`, used by opencode) is a different key and was not
touched.

## Next steps live in the homelab repo

The deploy continues off this Mac. Canonical runbook (on the Linux laptop):

    homelab-proxmox/specs/064-llm-router/RUNBOOK.md   § 3 (vault) then § 5 (LAB deploy)
    homelab-proxmox/specs/064-llm-router/amendment-drop-metered-tiers.md   (full write-up)

Remaining: put `mac_litellm_key` in the vault + drop `omlx_api_key`/`openrouter_api_key`/
`anthropic_api_key`, then run the LAB deploy. Nothing else is required on this Mac unless it
restarts, in which case run the "Bring it back" block above.

## Known gap

oMLX residency/pinning cannot be checked programmatically with `OMLX_API_KEY`:
`/admin/api/models` returns `Admin authentication required` (a separate admin credential,
not in `.env`). `/health` only reports `loaded_count`. Verify pinning in the oMLX UI, or
locate the admin key.
