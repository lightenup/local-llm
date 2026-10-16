#!/usr/bin/env python3
"""Thin client for the oMLX admin + OpenAI-compatible APIs.

Everything in this repo talks to oMLX through here so the auth dance and the
bench polling contract live in one place.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE = os.environ.get("OMLX_BASE", "http://127.0.0.1:8000")
MODEL = os.environ.get("OMLX_MODEL", "Qwen3.8-27B-8bit")

OMLX_SETTINGS = Path.home() / ".omlx" / "settings.json"


def _api_key() -> str:
    """OMLX_API_KEY, else the key oMLX already stores locally.

    Deliberately never hardcoded: the key would otherwise end up committed.
    """
    env = os.environ.get("OMLX_API_KEY")
    if env:
        return env
    try:
        return (json.loads(OMLX_SETTINGS.read_text()).get("auth") or {}).get(
            "api_key", ""
        )
    except (OSError, json.JSONDecodeError):
        return ""


API_KEY = _api_key()

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"


class OmlxError(RuntimeError):
    pass


class Omlx:
    """oMLX API client holding one admin session cookie."""

    def __init__(self, base: str = BASE, api_key: str = API_KEY) -> None:
        self.base = base.rstrip("/")
        self.api_key = api_key
        self._cookie: str | None = None

    # ---------------- transport ----------------

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        timeout: float = 60.0,
        admin: bool = True,
    ) -> Any:
        url = f"{self.base}{path}"
        data = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if admin:
            if self._cookie is None:
                self.login()
            headers["Cookie"] = self._cookie or ""
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:800]
            raise OmlxError(f"{method} {path} -> {e.code}: {detail}") from None
        if not raw:
            return None
        return json.loads(raw)

    def login(self) -> None:
        url = f"{self.base}/admin/api/login"
        data = json.dumps({"api_key": self.api_key, "remember": True}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            cookies = resp.headers.get_all("Set-Cookie") or []
            payload = json.loads(resp.read() or b"{}")
        if not payload.get("success"):
            raise OmlxError(f"admin login rejected: {payload}")
        # Keep only name=value; the attributes are irrelevant when we echo it back.
        self._cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
        if not self._cookie:
            raise OmlxError("admin login returned no cookie")

    def get(self, path: str, **kw) -> Any:
        return self._request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> Any:
        return self._request("POST", path, body=body, **kw)

    def put(self, path: str, body: Any = None, **kw) -> Any:
        return self._request("PUT", path, body=body, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self._request("DELETE", path, **kw)

    # ---------------- introspection ----------------

    def device_info(self) -> dict:
        return self.get("/admin/api/device-info")

    def server_info(self) -> dict:
        return self.get("/admin/api/server-info")

    def health(self) -> dict:
        return self.get("/health", admin=False)

    def models(self) -> list[dict]:
        return self.get("/admin/api/models")["models"]

    def model(self, model_id: str = MODEL) -> dict:
        for m in self.models():
            if m["id"] == model_id:
                return m
        raise OmlxError(f"model not found: {model_id}")

    def settings(self, model_id: str = MODEL) -> dict:
        # A freshly downloaded model that has never been loaded is listed
        # without a `settings` key at all, so treat that as empty rather than
        # raising: update_settings merges a patch onto whatever is there.
        return self.model(model_id).get("settings") or {}

    def global_settings(self) -> dict:
        return self.get("/admin/api/global-settings")

    def global_setting(self, name: str) -> Any:
        """Look up one global setting by its flat name.

        The GET nests fields under sections ("cache", "scheduler", ...) while
        update_global_settings takes them flat, so a caller that patches
        `chunked_prefill` cannot read it back under the same key.
        """
        g = self.global_settings()
        if name in g:
            return g[name]
        for v in g.values():
            if isinstance(v, dict) and name in v:
                return v[name]
        raise OmlxError(f"no such global setting: {name}")

    def update_global_settings(self, patch: dict) -> Any:
        """POST a partial global-settings patch (flat field names).

        Contrary to docs/FINDINGS.md section 8, the cache and scheduler levers
        ARE reachable here: `hot_cache_max_size` and `chunked_prefill` are
        fields on the admin GlobalSettingsRequest and are applied at runtime
        (omlx/admin/routes.py::_apply_cache_settings_runtime), which unloads
        loaded models so they pick the new config up on next load. Only
        `initial_cache_blocks`, host/port and the MCP config path need a full
        server restart.

        Sizes are strings: hot_cache_max_size="20GB", or "0" to disable.
        """
        return self.post("/admin/api/global-settings", patch, timeout=300)

    def cache_probe(self, messages: list[dict], model_id: str = MODEL) -> dict:
        """Classify a prompt's blocks as hot-SSD / disk-SSD / cold.

        This is the only direct instrument for prefix-cache behaviour: the chat
        `usage.prompt_tokens_details.cached_tokens` field reports what a request
        reused, whereas this reports where the blocks currently live before one
        is sent.
        """
        return self.post(
            "/admin/api/cache/probe", {"model_id": model_id, "messages": messages}
        )

    # ---------------- mutation ----------------

    # The settings GET returns derived/read-only fields that the PUT schema
    # (ModelSettingsRequest, additionalProperties=False) rejects outright.
    SETTINGS_READ_ONLY = ("display_name", "description", "active_profile_name")

    def update_settings(self, patch: dict, model_id: str = MODEL) -> Any:
        """PUT a partial settings patch merged onto the current settings."""
        current = self.settings(model_id)
        merged = {**current, **patch}
        for k in self.SETTINGS_READ_ONLY:
            merged.pop(k, None)
        return self.put(f"/admin/api/models/{model_id}/settings", merged)

    def load(self, model_id: str = MODEL) -> Any:
        return self.post(f"/admin/api/models/{model_id}/load", timeout=900)

    def unload(self, model_id: str = MODEL) -> Any:
        return self.post(f"/admin/api/models/{model_id}/unload", timeout=300)

    def reload_model(self, model_id: str = MODEL) -> None:
        """Force the engine to rebuild so load-time variant changes take effect."""
        try:
            self.unload(model_id)
        except OmlxError:
            pass
        time.sleep(2)
        self.load(model_id)

    # ---------------- chat ----------------

    def chat(
        self,
        messages: list[dict],
        model: str = MODEL,
        timeout: float = 900.0,
        **kw,
    ) -> dict:
        body = {"model": model, "messages": messages, **kw}
        return self._request(
            "POST", "/v1/chat/completions", body=body, timeout=timeout, admin=False
        )

    def timed_chat(self, messages: list[dict], model: str = MODEL, **kw) -> dict:
        """Chat with wall-clock timing and derived prefill/decode rates."""
        t0 = time.perf_counter()
        resp = self.chat(messages, model=model, **kw)
        wall = time.perf_counter() - t0
        usage = resp.get("usage") or {}
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        return {
            "wall_s": round(wall, 3),
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get(
                "cached_tokens", 0
            ),
            "tok_per_s_overall": round(ct / wall, 2) if wall else 0.0,
            "text": (resp["choices"][0]["message"].get("content") or ""),
            "finish_reason": resp["choices"][0].get("finish_reason"),
            "raw_usage": usage,
        }

    # ---------------- benchmarks ----------------

    def bench_throughput(
        self,
        prompt_lengths: list[int],
        generation_length: int = 128,
        batch_sizes: list[int] | None = None,
        context_profile: str = "code_python",
        warmup_mode: str = "quick",
        align_prompt_to_ane: bool = False,
        model_id: str = MODEL,
        poll: float = 5.0,
        timeout: float = 7200.0,
    ) -> dict:
        body = {
            "model_id": model_id,
            "prompt_lengths": prompt_lengths,
            "generation_length": generation_length,
            "batch_sizes": batch_sizes or [],
            "context_profile": context_profile,
            "warmup_mode": warmup_mode,
            "align_prompt_to_ane": align_prompt_to_ane,
        }
        started = self.post("/admin/api/bench/start", body, timeout=120)
        bench_id = started["bench_id"]
        return self._poll_bench(
            f"/admin/api/bench/{bench_id}/results", poll, timeout, bench_id
        )

    def bench_context(
        self,
        target_tokens: int = 262144,
        model_id: str = MODEL,
        poll: float = 5.0,
        timeout: float = 7200.0,
    ) -> dict:
        body = {"model_id": model_id, "target_tokens": target_tokens}
        started = self.post("/admin/api/bench/context/start", body, timeout=120)
        bench_id = started["bench_id"]
        return self._poll_bench(
            f"/admin/api/bench/context/{bench_id}/results", poll, timeout, bench_id
        )

    def bench_accuracy(
        self,
        benchmarks: dict[str, int],
        batch_size: int = 8,
        enable_thinking: bool = False,
        sampling_profile: str = "deterministic",
        model_id: str = MODEL,
    ) -> dict:
        body = {
            "model_id": model_id,
            "benchmarks": benchmarks,
            "batch_size": batch_size,
            "enable_thinking": enable_thinking,
            "sampling_profile": sampling_profile,
        }
        return self.post("/admin/api/bench/accuracy/queue/add", body, timeout=120)

    def accuracy_status(self) -> dict:
        return self.get("/admin/api/bench/accuracy/queue/status")

    def accuracy_results(self) -> dict:
        return self.get("/admin/api/bench/accuracy/results")

    def bench_ane_tune(
        self,
        sequence_length: int = 2048,
        repeats: int = 2,
        model_id: str = MODEL,
        poll: float = 10.0,
        timeout: float = 10800.0,
        **flags,
    ) -> dict:
        body = {
            "model_id": model_id,
            "sequence_length": sequence_length,
            "repeats": repeats,
            **flags,
        }
        started = self.post("/admin/api/bench/ane-tune/start", body, timeout=120)
        tuning_id = started.get("tuning_id") or started.get("bench_id")
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            res = self.get(f"/admin/api/bench/ane-tune/{tuning_id}/results")
            status = res.get("status")
            msg = f"{status} {res.get('phase','')} {res.get('message','')}".strip()
            if msg != last:
                print(f"    [ane-tune] {msg}", flush=True)
                last = msg
            if status in ("completed", "cancelled", "error"):
                return res
            time.sleep(poll)
        raise OmlxError(f"ANE tuning timed out: {tuning_id}")

    def _poll_bench(
        self, results_path: str, poll: float, timeout: float, bench_id: str
    ) -> dict:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            res = self.get(results_path, timeout=60)
            status = res.get("status")
            msg = f"{status} {res.get('phase','')} {res.get('message','')}".strip()
            if msg != last:
                print(f"    [{bench_id[:8]}] {msg}", flush=True)
                last = msg
            if status in ("completed", "cancelled", "error"):
                return res
            time.sleep(poll)
        raise OmlxError(f"bench timed out: {bench_id}")

    # ---------------- profiles ----------------

    def list_profiles(self, model_id: str = MODEL) -> list[dict]:
        return self.get(f"/admin/api/models/{model_id}/profiles")["profiles"]

    def create_profile(
        self,
        name: str,
        display_name: str,
        settings: dict,
        api_name: str | None = None,
        description: str | None = None,
        expose_as_model: bool = True,
        model_id: str = MODEL,
    ) -> Any:
        body = {
            "name": name,
            "display_name": display_name,
            "settings": settings,
            "expose_as_model": expose_as_model,
        }
        if api_name:
            body["api_name"] = api_name
        if description:
            body["description"] = description
        return self.post(f"/admin/api/models/{model_id}/profiles", body)

    def update_profile(self, name: str, model_id: str = MODEL, **fields) -> Any:
        return self.put(f"/admin/api/models/{model_id}/profiles/{name}", fields)

    def delete_profile(self, name: str, model_id: str = MODEL) -> Any:
        return self.delete(f"/admin/api/models/{model_id}/profiles/{name}")

    # ---------------- huggingface ----------------

    def hf_download(self, repo_id: str) -> dict:
        """Queue a model download. Returns the task record."""
        return self.post("/admin/api/hf/download", {"repo_id": repo_id}, timeout=120)

    def hf_tasks(self) -> list[dict]:
        return self.get("/admin/api/hf/tasks")["tasks"]

    def wait_for_download(
        self, repo_id: str, poll: float = 30.0, timeout: float = 14400.0
    ) -> dict:
        """Block until the download task for repo_id leaves a running state."""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            task = next(
                (t for t in self.hf_tasks() if t.get("repo_id") == repo_id), None
            )
            if task is None:
                raise OmlxError(f"no download task found for {repo_id}")
            status = (task.get("status") or "").lower()
            msg = f"{status} {task.get('progress') or ''} {task.get('message') or ''}"
            if msg != last:
                print(f"    [hf] {repo_id}: {msg.strip()}", flush=True)
                last = msg
            if status in ("completed", "complete", "done", "error", "failed", "cancelled"):
                return task
            time.sleep(poll)
        raise OmlxError(f"download timed out: {repo_id}")

    # ---------------- results ----------------

    @staticmethod
    def save(name: str, payload: Any) -> Path:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"  saved -> {path.relative_to(REPO_ROOT)}")
        return path

    @staticmethod
    def load_result(name: str) -> Any:
        return json.loads((RESULTS_DIR / f"{name}.json").read_text())


if __name__ == "__main__":
    o = Omlx()
    print("device :", o.device_info())
    print("health :", o.health())
    m = o.model()
    print(
        f"model  : {m['id']} loaded={m['loaded']} engine={m['engine_type']} "
        f"ctx={m['model_context_length']} size={m['actual_size_formatted']}"
    )
    print(
        f"mtp    : compatible={m['mtp_compatible']} reason={m['mtp_compatibility_reason'][:80]}"
    )
    print(f"dflash : compatible={m['dflash_compatible']}")
    print("profiles:", [p.get("name") for p in o.list_profiles()])
