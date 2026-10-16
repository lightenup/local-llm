#!/usr/bin/env python3
"""Replay the 2026-09-30 20:45:59 prefill refusal with all models unpinned.

The original refusal: Qwen3.8-27B-8bit finished a 34,323-token turn, then an
oQ4e request needing ~30.32 GB of KV+SDPA (~124k fresh tokens at ~256 KiB per
token for this 16-full-attention-layer model) was rejected because the
pinned, idle 8-bit model could not be evicted ("No idle model evicted").

This script re-creates that sequence through LiteLLM (:4000), the only path
clients are allowed to use, and then reads the oMLX log lines written during
the run. Pass = the oQ4e request completes and no prefill-guard rejection is
logged. Whether an eviction was actually needed is reported, not assumed:
if the dynamic ceiling had room this time, the run shows no refusal but does
not exercise eviction.

--forced skips the 8-bit turn and instead sets a temporary custom ceiling so
the oQ4e request fits only if the idle 8-bit model is evicted. The ceiling
sits just above the soft watermark so the enforcer's own soft-pressure
eviction does not fire first. The original tier and ceiling are restored in
`finally` and the restore is verified.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OMLX = "http://127.0.0.1:8000"
LITELLM = "http://127.0.0.1:4000"
LOG = Path.home() / ".omlx" / "logs" / "server.log"

LONG_TURN_MODEL = "Qwen3.8-27B-8bit:qwen-code"
FOLLOWUP_MODEL = "Qwen3.8-27B-oQ4e-mtp:oq-agent"
SERVABLE = ("Qwen3.8-27B-8bit", "Qwen3.8-27B-oQ4e-mtp")

WORDS = (
    "river stone garden window letter market winter summer engine paper "
    "bridge castle forest silver golden yellow simple quiet number system "
    "orange purple circle square answer question morning evening travel "
    "doctor teacher farmer kitchen bottle pocket button jacket mirror ladder"
).split()

INTERESTING = re.compile(
    r"prefill-eviction|No idle model evicted|rejected by prefill memory guard"
    r"|needs prefill headroom|Unloaded model|Loaded model|Evict|Chat completion"
    r"|Memory pressure level"
)


_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)

# Custom-tier watermarks in oMLX 0.7.0 (process_memory_enforcer.py
# _SOFT/_HARD_THRESHOLD_BY_TIER). The prefill preflight target is the hard one.
CUSTOM_SOFT = 0.90
FORCED_HEADROOM_GIB = 1.5


def _request(url: str, *, body: dict | None = None, headers: dict | None = None,
             timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")[:2000]}


def _omlx_bearer() -> dict:
    return {"Authorization": f"Bearer {os.environ['OMLX_API_KEY']}"}


def _litellm_bearer() -> dict:
    return {"Authorization": f"Bearer {os.environ['LITELLM_LOCAL_KEY']}"}


def _filler(n_words: int, rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n_words))


def _chat(model: str, n_words: int, rng: random.Random, max_tokens: int) -> tuple[int, dict, float]:
    # A fresh nonce up front defeats the prefix cache, so the whole prompt is
    # charged as new tokens, as in the original request.
    nonce = f"{time.time_ns()}-{rng.random()}"
    prompt = (
        f"Run {nonce}. Ignore the word list below and reply with the single word OK.\n\n"
        + _filler(n_words, rng)
    )
    t0 = time.monotonic()
    status, body = _request(
        f"{LITELLM}/v1/chat/completions",
        body={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        },
        headers=_litellm_bearer(),
        timeout=1800,
    )
    return status, body, time.monotonic() - t0


def _loaded_models() -> dict[str, bool]:
    status, body = _request(f"{OMLX}/v1/models/status", headers=_omlx_bearer())
    if status != 200:
        return {}
    out = {}
    for m in body.get("models", []):
        out[m.get("id")] = bool(m.get("loaded"))
    return out


def _guard_settings() -> tuple[str, float]:
    status, body = _request(f"{OMLX}/admin/api/global-settings")
    if status != 200:
        raise RuntimeError(f"global-settings GET {status}: {body}")
    mem = body.get("memory", body)
    return mem["memory_guard_tier"], float(mem["memory_guard_custom_ceiling_gb"] or 0.0)


def _set_guard(tier: str, ceiling_gb: float) -> None:
    status, body = _request(
        f"{OMLX}/admin/api/global-settings",
        body={"memory_guard_tier": tier, "memory_guard_custom_ceiling_gb": ceiling_gb},
    )
    if status != 200:
        raise RuntimeError(f"global-settings POST {status}: {body}")


def _pressure() -> tuple[int, str]:
    status, body = _request(f"{OMLX}/admin/api/stats")
    mp = body["active_models"]["memory_pressure"]
    return int(mp["current_bytes"]), mp["pressure_level"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--long-words", type=int, default=30000,
                    help="filler words for the 8-bit turn (~34k tokens)")
    ap.add_argument("--followup-tokens", type=int, default=118000,
                    help="target prompt tokens for the oQ4e request (LiteLLM cap 131072)")
    ap.add_argument("--forced", action="store_true",
                    help="skip the 8-bit turn; force a shortfall with a temporary custom ceiling")
    args = ap.parse_args()

    # Without this, a hangup from a closing parent shell kills the process
    # before `finally` can restore the guard (happened on 2026-10-03).
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda signum, _frame: sys.exit(128 + signum))

    rng = random.Random(20260930)
    log_offset = LOG.stat().st_size

    for sid in SERVABLE:
        status, body = _request(f"{OMLX}/admin/api/models/{sid}/load",
                                body={}, headers=_omlx_bearer(), timeout=600)
        print(f"preload {sid}: {status} {body.get('message', body)}")
        if status != 200:
            return 2

    original_guard = None
    try:
        if args.forced:
            status, body = _request(f"{OMLX}/admin/api/login",
                                    body={"api_key": os.environ["OMLX_API_KEY"]})
            if status != 200:
                print(f"admin login failed: {status}")
                return 2
            original_guard = _guard_settings()
            current, level = _pressure()
            ceiling_gb = round((current / 1024**3 + FORCED_HEADROOM_GIB) / CUSTOM_SOFT, 2)
            print(f"guard before: {original_guard}; current={current / 1024**3:.2f}GiB level={level}")
            print(f"setting custom ceiling {ceiling_gb} GiB "
                  f"(soft {ceiling_gb * CUSTOM_SOFT:.2f}, hard {ceiling_gb * 0.97:.2f})")
            _set_guard("custom", ceiling_gb)
            time.sleep(3)
            current, level = _pressure()
            loaded = _loaded_models()
            print(f"after set: current={current / 1024**3:.2f}GiB level={level} "
                  f"8bit_loaded={loaded.get(SERVABLE[0])}")
            if not loaded.get(SERVABLE[0]):
                print("INCONCLUSIVE: the 8-bit model was evicted before the request was sent")
                return 3
            tokens_per_word = 30068 / 30000  # measured on this tokenizer, 2026-10-03
        else:
            print(f"step 1: {LONG_TURN_MODEL}, {args.long_words} filler words")
            s1, b1, t1 = _chat(LONG_TURN_MODEL, args.long_words, rng, max_tokens=64)
            p1 = (b1.get("usage") or {}).get("prompt_tokens")
            print(f"  -> {s1} in {t1:.1f}s, prompt_tokens={p1}")
            if s1 != 200 or not p1:
                print(json.dumps(b1)[:2000])
                return 2
            tokens_per_word = p1 / args.long_words

        words = int(args.followup_tokens / tokens_per_word)
        print(f"step 2: {FOLLOWUP_MODEL}, {words} filler words (~{args.followup_tokens} tokens)")
        s2, b2, t2 = _chat(FOLLOWUP_MODEL, words, rng, max_tokens=16)
        p2 = (b2.get("usage") or {}).get("prompt_tokens")
        print(f"  -> {s2} in {t2:.1f}s, prompt_tokens={p2}")
        if s2 != 200:
            print(json.dumps(b2)[:2000])
    finally:
        if original_guard is not None:
            _set_guard(*original_guard)
            restored = _guard_settings()
            ok = restored == original_guard
            print(f"guard restored: {restored} ({'verified' if ok else 'MISMATCH'})")
            restore_failed = not ok
    if original_guard is not None and restore_failed:
        return 4

    time.sleep(2)
    with LOG.open("rb") as fh:
        fh.seek(log_offset)
        new = fh.read().decode(errors="replace").splitlines()
    lines = [ln for ln in new if INTERESTING.search(ln)]
    print("\n--- oMLX log during the run ---")
    for ln in lines:
        print(ln[:400])

    rejected = any("rejected by prefill memory guard" in ln for ln in lines)
    evictions = [int(m.group(1)) for ln in lines
                 if (m := re.search(r"\[prefill-eviction\].*evicted=(\d+)", ln))]
    headroom_needed = any("needs prefill headroom" in ln for ln in lines)

    print("\n--- verdict ---")
    print(f"followup status: {s2}")
    print(f"prefill guard rejection logged: {rejected}")
    print(f"prefill headroom needed: {headroom_needed}")
    print(f"prefill-eviction evicted counts: {evictions}")
    print(f"models loaded after run: {_loaded_models()}")
    if s2 == 200 and not rejected:
        if headroom_needed and any(evictions):
            print("PASS: headroom was short and oMLX evicted an idle model instead of refusing")
        elif headroom_needed:
            print("PASS: headroom was short and resolved without eviction")
        else:
            print("PASS (eviction not exercised): the ceiling had room this time")
        return 0
    print("FAIL: the follow-up request was refused")
    return 1


if __name__ == "__main__":
    sys.exit(main())
