#!/usr/bin/env python3
"""Classify the quantisation build behind an MLX checkpoint, local or on the Hub.

A2a in docs/backlog.md. The premise, from the working notes: measured against
bf16, a stock 4-bit RTN build sits about 3x worse than a DWQ build of the same
file size, so *which* build you pull matters as much as the bit count. Nothing
in this repo had ever checked, and a benchmark run against an unidentified build
measures the build rather than the bit width.

Classification is read off `config.json` alone, so a remote repo costs one HTTP
request and no weight download. Two signals carry it:

  * the scalar `bits` / `group_size` / `mode`, which give the base precision;
  * the per-module overrides in the same `quantization` dict, which are what
    separates a uniform build from a mixed-precision one.

The four labels this assigns, and what each means for a benchmark:

  uniform          One precision everywhere. Plain round-to-nearest.
  router-protected Uniform apart from the MoE router gates, which the mlx
                   converter bumps by formula because a wrong top-k routing
                   decision is catastrophic and the gates are tiny. Still RTN
                   for every tensor that carries real weight.
  mixed-up         A meaningful share of modules held *above* the base
                   precision. The data-driven allocation the notes call oQ.
  mixed-down       Modules pushed *below* the base precision. This is a
                   size-reduction build, not a quality-per-byte bet, and it is
                   the case most easily mistaken for the one above.

DWQ cannot be detected this way: it is a distillation procedure that leaves an
ordinary affine config behind, so it shows up only in the repo name. That is
reported as a naming signal, never as a config finding.

    python3 bench/quant_id.py                          # every local oMLX model
    python3 bench/quant_id.py mlx-community/Qwen3-Coder-Next-6bit
    python3 bench/quant_id.py --all --json results/quant_id.json

Read-only. Touches no model, no profile and no oMLX setting.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import RESULTS_DIR  # noqa: E402

OMLX_MODELS = Path.home() / ".omlx" / "models"
HF_CONFIG = "https://huggingface.co/{repo}/resolve/main/config.json"
HF_INFO = "https://huggingface.co/api/models/{repo}?blobs=true"

# Modules the mlx converter bumps by formula rather than by measurement. A build
# whose only overrides are these is a uniform build wearing a hat.
ROUTER_SUFFIXES = ("mlp.gate", "mlp.shared_expert_gate", "block_sparse_moe.gate")

# Repo-name substrings that claim a data-driven build. Naming only: none of
# these is verifiable from config.json.
NAME_SIGNALS = ("dwq", "awq", "gptq", "oq", "qx", "hqq")


def _fetch(url: str, timeout: int = 30) -> dict:
    """GET a JSON document, surviving a TLS-inspecting proxy.

    urllib validates against Python's own CA bundle, which does not contain the
    corporate proxy root this machine is issued, so every Hub request fails with
    CERTIFICATE_VERIFY_FAILED on the corporate network. curl reads the system
    trust store and succeeds. Try urllib first so the script stays dependency-free
    off-network, and fall back rather than disabling verification, which would
    trade a working setup for an insecure one.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "local-llm/quant_id"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        if not isinstance(getattr(exc, "reason", None), ssl.SSLError):
            raise
    out = subprocess.run(
        ["curl", "-sSL", "--max-time", str(timeout), url],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise urllib.error.URLError(f"curl failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _quant_block(config: dict) -> dict:
    """The quantisation dict, wherever this checkpoint keeps it.

    VLM conversions sometimes nest it under text_config instead of the root.
    """
    for holder in (config, config.get("text_config") or {}):
        block = holder.get("quantization") or holder.get("quantization_config")
        if block:
            return block
    return {}


def _is_router(module: str) -> bool:
    return any(module.endswith(suffix) for suffix in ROUTER_SUFFIXES)


def classify(repo: str, config: dict, size_gb: float | None = None) -> dict:
    quant = _quant_block(config)
    if not quant:
        return {"repo": repo, "quantized": False, "build": "unquantized",
                "size_gb": size_gb}

    base_bits = quant.get("bits")
    overrides = {k: v for k, v in quant.items() if isinstance(v, dict)}
    # A module can appear with bits=False/None to mean "left unquantized".
    graded = {k: v for k, v in overrides.items()
              if isinstance(v.get("bits"), int)}

    above = {k: v for k, v in graded.items() if v["bits"] > (base_bits or 0)}
    below = {k: v for k, v in graded.items() if v["bits"] < (base_bits or 0)}
    non_router_above = {k: v for k, v in above.items() if not _is_router(k)}

    if not graded:
        build = "uniform"
    elif below:
        build = "mixed-down"
    elif non_router_above:
        build = "mixed-up"
    elif above:
        build = "router-protected"
    else:
        build = "uniform"

    # Effective bits ignores tensor sizes, so it ranks builds rather than
    # predicting file size. The real size is reported alongside it.
    counts = Counter(v["bits"] for v in graded.values())
    total_modules = quant.get("_n_modules") or len(graded)
    effective = (
        sum(b * n for b, n in counts.items()) / total_modules
        if total_modules else base_bits
    )

    name_signal = [s for s in NAME_SIGNALS if s in repo.lower().replace("-", "")]

    return {
        "repo": repo,
        "quantized": True,
        "build": build,
        "base_bits": base_bits,
        "group_size": quant.get("group_size"),
        "mode": quant.get("mode"),
        "size_gb": size_gb,
        "modules_graded": len(graded),
        "modules_above_base": len(above),
        "modules_above_base_non_router": len(non_router_above),
        "modules_below_base": len(below),
        "bit_histogram": {str(b): n for b, n in sorted(counts.items())},
        "effective_bits_over_graded": round(effective, 2) if graded else base_bits,
        "name_signal": name_signal,
        "converted_by": config.get("mlx_version") or config.get("transformers_version"),
    }


def module_profile(config: dict, limit: int = 10) -> list[tuple[str, int, int]]:
    """Where the overrides land, collapsed over layer index."""
    quant = _quant_block(config)
    groups: dict[tuple[str, int], int] = defaultdict(int)
    for module, spec in quant.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("bits"), int):
            continue
        shape = re.sub(r"\.\d+\.", ".N.", module)
        shape = re.sub(r"^(model\.)?(language_model\.)?(model\.)?", "", shape)
        groups[(shape, spec["bits"])] += 1
    ranked = sorted(groups.items(), key=lambda kv: -kv[1])[:limit]
    return [(shape, bits, n) for (shape, bits), n in ranked]


def local_models() -> list[Path]:
    if not OMLX_MODELS.exists():
        return []
    return sorted(p for p in OMLX_MODELS.glob("*/*") if (p / "config.json").is_file())


def load_local(path: Path) -> tuple[str, dict, float]:
    config = json.loads((path / "config.json").read_text())
    size = sum(f.stat().st_size for f in path.glob("*.safetensors")) / 1e9
    return f"{path.parent.name}/{path.name}", config, round(size, 1)


def load_remote(repo: str) -> tuple[str, dict, float | None]:
    config = _fetch(HF_CONFIG.format(repo=repo))
    size = None
    try:
        info = _fetch(HF_INFO.format(repo=repo))
        total = sum(
            f.get("size") or 0
            for f in info.get("siblings", [])
            if f["rfilename"].endswith(".safetensors")
        )
        size = round(total / 1e9, 1) or None
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        pass  # size is a nicety; the classification is the point
    return repo, config, size


NOTE = {
    "uniform": "plain RTN, one precision throughout",
    "router-protected": "RTN plus the converter's formulaic router bump",
    "mixed-up": "extra bits allocated above the base precision",
    "mixed-down": "modules dropped below base: smaller, not better-per-byte",
    "unquantized": "no quantisation block",
}


def print_table(rows: list[dict]) -> None:
    head = (f"  {'repo':<44} {'build':<17} {'base':>5} {'eff':>5} "
            f"{'size':>7}  note")
    print(head)
    print("  " + "-" * (len(head) - 2))
    for r in rows:
        size = f"{r['size_gb']:.1f} GB" if r.get("size_gb") else "-"
        eff = r.get("effective_bits_over_graded")
        print(
            f"  {r['repo']:<44} {r['build']:<17} "
            f"{str(r.get('base_bits') or '-'):>5} {str(eff or '-'):>5} "
            f"{size:>7}  {NOTE.get(r['build'], '')}"
        )
        if r.get("name_signal"):
            print(f"  {'':<44} name claims: {', '.join(r['name_signal'])}"
                  " (not verifiable from config)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="*", help="Hub repo ids; default is local models")
    ap.add_argument("--all", action="store_true",
                    help="local models as well as the named repos")
    ap.add_argument("--modules", action="store_true",
                    help="show where the overrides land")
    ap.add_argument("--json", action="store_true",
                    help=f"write results to {RESULTS_DIR / 'quant_id.json'}")
    ap.add_argument("--out", metavar="PATH", help="write results to PATH instead")
    args = ap.parse_args()

    targets: list[tuple[str, dict, float | None]] = []
    if args.all or not args.repos:
        for path in local_models():
            try:
                targets.append(load_local(path))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  ! {path.name}: {exc}", file=sys.stderr)
    for repo in args.repos:
        try:
            targets.append(load_remote(repo))
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError) as exc:
            print(f"  ! {repo}: {exc}", file=sys.stderr)

    if not targets:
        print("nothing to classify", file=sys.stderr)
        return 1

    rows = [classify(name, config, size) for name, config, size in targets]
    print_table(rows)

    if args.modules:
        for (name, config, _), row in zip(targets, rows):
            profile = module_profile(config)
            if not profile:
                continue
            print(f"\n  {name} — overrides by module shape")
            for shape, bits, n in profile:
                print(f"    {bits}b x{n:<4} {shape}")

    if args.json or args.out:
        out = Path(args.out) if args.out else RESULTS_DIR / "quant_id.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"generated": datetime.now().isoformat(timespec="seconds"),
             "models": rows}, indent=2) + "\n")
        print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
