# Notes for coding agents

Measured profile setup and benchmark harness for a local LLM stack on Apple
Silicon: oMLX as the engine, `proxy/llm_queue.py` in front of it, LiteLLM as
the OpenAI-compatible proxy, opencode as the main client. Start with
`README.md`; the evidence is in `docs/FINDINGS.md` (cited as §N) and the plan
in `docs/backlog.md`.

## Machine-specific facts

**Read `LOCAL.md` first if it exists.** It is untracked and maps the
placeholders used in the docs (`<mac-lan-ip>`, `<monitoring-host>`,
`<llm-router-prod>`, `<lan-subnet>`, `$LAUNCHD_PREFIX`, "the corporate TLS
proxy") to this machine's real values, and holds homelab context. If it does
not exist you are on someone else's checkout: the placeholders are theirs to
fill.

Settings scripts read: `.env.local` (see `.env.local.example`) and
`proxy/.env` (see `proxy/.env.example`). Both are untracked.

## Rules

- This repository is public. Never commit a LAN address, a home-directory
  path, a hostname or label prefix from `LOCAL.md`, or a key. Write the
  placeholder and, if it is new, add it to `LOCAL.md`. Shell scripts find the
  repo with `cd "$(dirname "$0")/.."`; Python with `Path(__file__)`.
- `scripts/check_public.sh` must pass before a push.
- Raw logs in `results/*.log` are local only; the JSON in `results/` is the
  published record.
- Enabling something and seeing no error is not evidence it is on (§24):
  measure, then record the result in FINDINGS.

## Tests

    python3 -m unittest discover -s tests
