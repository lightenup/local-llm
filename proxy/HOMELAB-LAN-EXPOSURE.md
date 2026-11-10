# Homelab LAN exposure — read after a restart

This proxy was opened to the LAN on **2026-09-16** so the homelab Proxmox gateway
(spec `064-llm-router`) can chain to it. If you just restarted this Mac, start here.

## What is exposed

Since 2026-11-10 (FINDINGS §43) `docker-compose.yml` publishes LiteLLM on
`127.0.0.1:4000` only. The gateway LXC on `<lan-subnet>` reaches it through the homelab
TLS terminator on `:4443` (spec 064, `mac-tls-terminator/`), which forwards over loopback.
From 2026-09-16 to 2026-11-10 the compose file published `0.0.0.0:4000` as well, a plain-HTTP
path the gateway stopped using once the terminator landed. **oMLX stays on `127.0.0.1:8000`
and is reachable only by this LiteLLM.** LiteLLM enforces keys and access groups.

## Bring it back after a restart

**Docker Desktop** and **oMLX** are macOS login items, so they start once you log in
(FileVault: nothing starts before login). LiteLLM runs in **Docker Desktop only**; Colima
must not run a second copy (`docker context show` should say `desktop-linux`).

Since §43 the `stack-supervisor` LaunchAgent does the rest: within a minute of LiteLLM
failing its health check it opens Docker if needed and runs `docker compose up -d`, and it
opens oMLX if `:8000` stays down for 5 minutes. The 2026-11-09 reboot left both containers
`Exited (0)` despite `restart: unless-stopped`; the restart policy alone is not enough.
Its log is `$LLM_LOG_DIR/stack_supervisor.log`. To check by hand:

```bash
lsof -nP -iTCP:4000 -sTCP:LISTEN                          # want 127.0.0.1:4000 (NOT *)
lsof -nP -iTCP:8000 -sTCP:LISTEN                          # want 127.0.0.1:8000 (NOT *)
lsof -nP -iTCP:4443 -sTCP:LISTEN                          # the terminator, *:4443
curl -s -m5 http://127.0.0.1:4000/health/liveliness      # "I'm alive!"
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
restarts, in which case check the "Bring it back" block above.

## Known gap

oMLX residency/pinning cannot be checked programmatically with `OMLX_API_KEY`:
`/admin/api/models` returns `Admin authentication required` (a separate admin credential,
not in `.env`). `/health` only reports `loaded_count`. Verify pinning in the oMLX UI, or
locate the admin key.
