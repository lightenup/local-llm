#!/usr/bin/env python3
"""Watch the Hub for new MLX checkpoints worth gating on this Mac.

Backlog D3 puts the cheap checks before the GPU hours; this puts a cheaper one
in front of those: was anything published that this setup could use at all?
It lists new repos from the uploaders in scout/scout.toml, drops what cannot
run here or was already closed by a finding, and says for the rest why it is
worth a look and the exact commands to gate it.

    python3 bench/model_scout.py scan                 # new since the last scan
    python3 bench/model_scout.py scan --since 14      # last 14 days, ignore state
    python3 bench/model_scout.py scan --format json   # for an agent
    python3 bench/model_scout.py scan --notify        # daily job: post what is new
    python3 bench/model_scout.py scan --quiet         # stdout only when there is news
    python3 bench/model_scout.py export               # on the Mac: refresh setup.json
    python3 bench/model_scout.py download REPO        # on the Mac: queue in oMLX

`scan` runs anywhere with Python 3.11+ and network: it reads only Hub listing
metadata, config.json and file sizes. It never downloads weights and never
reads a model card, so untrusted prose on the Hub cannot reach whoever acts on
the output. What the Mac can load and what the lineup costs come from
scout/setup.json, which `export` writes on the Mac and which is committed.

The verdicts, in the order a scan sorts them:

  promising  fits, loads, and carries a signal from [signals] in scout.toml
  consider   fits and loads, no signal; or flagged by a closed finding
  blocked    fits, but the bundled mlx_lm/mlx_vlm has no such architecture;
             rechecked automatically after the next `export`
  skip       too large, too small, too few bits, closed, or not a chat model

Seen repos are remembered in $SCOUT_STATE (default
~/.cache/local-llm/scout_state.json), so each one is reported once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quant_id import HF_CONFIG, HF_INFO, _fetch, classify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "scout" / "scout.toml"
SETUP = ROOT / "scout" / "setup.json"
STATE = Path(os.environ.get(
    "SCOUT_STATE", Path.home() / ".cache" / "local-llm" / "scout_state.json"))
GIB = 2**30
LISTING = ("https://huggingface.co/api/models?author={author}&sort=createdAt"
           "&direction=-1&limit={limit}&expand[]=createdAt&expand[]=pipeline_tag"
           "&expand[]=config")
LEADERBOARD = "https://omlx.ai/benchmarks/intelligence?model={name}"
SETUP_STALE_DAYS = 30
ORDER = {"promising": 0, "consider": 1, "blocked": 2, "skip": 3}

Fetch = Callable[[str], object]


# ---- inputs ---------------------------------------------------------------

def load_policy(path: Path = POLICY) -> dict:
    return tomllib.loads(path.read_text())


def load_setup(path: Path = SETUP) -> dict:
    return json.loads(path.read_text())


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def list_new(author: str, since: datetime, fetch: Fetch = _fetch) -> list[dict]:
    """Repos by `author` created after `since`, newest first.

    The listing is sorted by createdAt. A daily scan fits in the first 100;
    a long look-back asks again for 1000, the Hub's page maximum, and says so
    if even that is not enough (its cursor is in a Link header _fetch drops).
    """
    for limit in (100, 1000):
        url = LISTING.format(author=urllib.parse.quote(author), limit=limit)
        rows = fetch(url)
        new = [r for r in rows
               if r.get("createdAt") and _when(r["createdAt"]) > since]
        if len(new) < limit:
            return new
    print(f"  ! {author}: {limit}+ new repos, only the newest {limit} read",
          file=sys.stderr)
    return new


def inspect(repo: str, fetch: Fetch = _fetch) -> dict:
    """What evaluate() needs about one repo, without downloading weights.

    config.json, the weight size in GiB, and whether MTP weights are actually
    present: two requests, three when the config claims an MTP head. Raises
    LookupError when there is no readable config.json: a gated repo, a
    GGUF-only repo, or an upload still in progress.
    """
    try:
        config = fetch(HF_CONFIG.format(repo=repo))
    except urllib.error.HTTPError as exc:
        raise LookupError(f"no readable config.json (HTTP {exc.code})") from exc
    except json.JSONDecodeError as exc:
        raise LookupError("no readable config.json") from exc
    if not isinstance(config, dict) or "error" in config:
        raise LookupError("no readable config.json")
    size, files = None, []
    try:
        info = fetch(HF_INFO.format(repo=repo))
        files = info.get("siblings", [])
        total = sum(f.get("size") or 0 for f in files
                    if f["rfilename"].endswith(".safetensors"))
        size = round(total / GIB, 1) or None
    except (urllib.error.URLError, json.JSONDecodeError):
        pass
    return {"config": config, "size": size,
            "mtp": mtp_weights(repo, config, files, fetch)}


def mtp_weights(repo: str, config: dict, files: list[dict],
                fetch: Fetch = _fetch) -> str | None:
    """'in-checkpoint', 'sidecar', or None.

    The config is not evidence. mlx-community conversions keep
    `mtp_num_hidden_layers` from the base model and drop the tensors: the
    shipped 8-bit says 1 and has none, which is why it needs an external
    drafter (§19). So this reads the weight index. A shard such as
    model-mtp.safetensors is in the index and loads with the checkpoint, as
    the shipped oQ4e does; an MTP file outside the index is a sidecar that
    some other runtime is expected to wire up.
    """
    named = any("mtp" in f["rfilename"].lower() and f["rfilename"].endswith(".safetensors")
                for f in files)
    if not (named or claims_mtp(config)):
        return None
    try:
        index = fetch(f"https://huggingface.co/{repo}/resolve/main/"
                      "model.safetensors.index.json")
        keys = (index or {}).get("weight_map", {})
    except (urllib.error.URLError, json.JSONDecodeError):
        keys = {}
    if any("mtp" in k or "nextn" in k for k in keys):
        return "in-checkpoint"
    return "sidecar" if named else None


# ---- the rules ------------------------------------------------------------

def model_type(config: dict) -> str | None:
    return config.get("model_type") or (config.get("text_config") or {}).get("model_type")


def _arch(mtype: str | None) -> str | None:
    """qwen3_5_text is the text-only half of qwen3_5: the same architecture."""
    return mtype[:-len("_text")] if mtype and mtype.endswith("_text") else mtype


# Quantisation and packaging suffixes, stripped to find the model a repo is a
# build of: "AREX-2-6bit" and "AREX-2-8bit" are one model, two builds.
_BUILD = re.compile(
    r"-(?:\d+(?:\.\d+)?bit|bf16|fp16|fp8|mxfp\d|nvfp\d|oQ\d+e?|OptiQ|DWQ|"
    r"MLX|mlx|mtp|MTP|chat|REAP-\d+B|qx\d+n?|q\d|Q\d)$", re.I)


def family(repo: str) -> str:
    name = repo.split("/", 1)[1]
    while (stripped := _BUILD.sub("", name)) != name:
        name = stripped
    return name


def params_b(repo: str, size: float, bits: int | None) -> float:
    """Total parameters in billions: from the name if it says, else estimated.

    The estimate adds half a bit per weight for the quantisation scales and
    reads an unquantized repo as 16-bit.
    """
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)B(?![a-z])", repo.split("/", 1)[1])
    if m:
        return float(m.group(1))
    per_weight = (bits + 0.5) if bits else 16
    return round(size * GIB * 8 / per_weight / 1e9, 1)


def claims_mtp(config: dict) -> bool:
    for holder in (config, config.get("text_config") or {}):
        for key in ("mtp_num_hidden_layers", "num_nextn_predict_layers"):
            if (holder.get(key) or 0) > 0:
                return True
    return False


def headroom(policy: dict, setup: dict) -> tuple[float, dict[str, float]]:
    """GiB free beside the core lineup after the serve margin, and tier sizes."""
    sizes = {m["id"]: m["gib"] for m in setup["models"]}
    tiers = {name: sizes.get(t["model"], 0.0) for name, t in policy["lineup"].items()}
    core = sum(tiers[n] for n, t in policy["lineup"].items() if t.get("core"))
    b = policy["budget"]
    return round(b["prefill_ceiling_gib"] - core - b["serve_margin_gib"], 1), tiers


def fit(size: float, policy: dict, setup: dict) -> str | None:
    """'alongside', 'replaces <tier>', or None when it fits nowhere."""
    free, tiers = headroom(policy, setup)
    if size <= free:
        return "alongside"
    for name, t in policy["lineup"].items():
        if t.get("core") and tiers[name] and size <= free + tiers[name]:
            return f"replaces {name}"
    return None


def _exclusion(repo: str, mtype: str | None, policy: dict) -> dict | None:
    for rule in policy.get("exclude", []):
        if "pattern" in rule and re.search(rule["pattern"], repo, re.I):
            return rule
        if "model_type" in rule and rule["model_type"] == mtype:
            return rule
    return None


def prefilter(entry: dict, policy: dict, setup: dict) -> str | None:
    """A skip reason from listing metadata alone, before any per-repo request."""
    repo = entry["id"]
    if entry.get("pipeline_tag") not in policy["pipeline_tags"]:
        return f"not a chat model ({entry.get('pipeline_tag') or 'no pipeline tag'})"
    # oMLX names a download after the repo, and rarely records the source.
    local = {m["id"] for m in setup["models"]}
    if repo in setup.get("source_repos", []) or repo.split("/", 1)[1] in local:
        return "already on the Mac"
    rule = _exclusion(repo, model_type(entry.get("config") or {}), policy)
    if rule and rule["action"] == "skip":
        return f"closed by §{rule['section']}: {rule['why']}"
    return None


def evaluate(repo: str, facts: dict, policy: dict, setup: dict) -> dict:
    """Verdict for one repo, from inspect()'s facts. Pure: no network, no clock."""
    config, size = facts["config"], facts["size"]
    q = classify(repo, config, size)
    mtype = model_type(config)
    out = {"repo": repo, "url": f"https://huggingface.co/{repo}",
           "model_type": mtype, "size_gib": size, "build": q["build"],
           "bits": q.get("base_bits"), "fit": None, "signals": [],
           "warnings": [], "next_steps": []}

    def skip(why: str) -> dict:
        return {**out, "verdict": "skip", "why": why}

    b = policy["budget"]
    rule = _exclusion(repo, mtype, policy)
    if rule and rule["action"] == "skip":
        return skip(f"closed by §{rule['section']}: {rule['why']}")
    if size is None:
        return skip("no safetensors on the repo")
    if size < b["min_size_gib"]:
        return skip(f"{size} GiB: a drafter, adapter or sidecar, not a model")
    params = params_b(repo, size, q.get("base_bits") if q["quantized"] else None)
    out["params_b"] = params
    if params < b["min_params_b"]:
        return skip(f"~{params}B parameters is below the lineup's class")
    if q["quantized"] and (q.get("base_bits") or 0) < b["min_bits"]:
        return skip(f"{q.get('base_bits')}-bit is below {b['min_bits']} bits")
    out["fit"] = fit(size, policy, setup)
    if out["fit"] is None:
        free, _ = headroom(policy, setup)
        return skip(f"{size} GiB does not fit (free beside the lineup: {free} GiB)")

    sig = policy["signals"]
    if "dwq" in q.get("name_signal", []):
        out["signals"].append(sig["dwq"])
    if facts.get("mtp"):
        out["signals"].append(sig["mtp"])
        if facts["mtp"] == "sidecar":
            out["warnings"].append("MTP weights are a sidecar file; "
                                   "not yet seen loading in oMLX")
    if _arch(mtype) not in {_arch(t) for t in setup.get("local_model_types", [])}:
        out["signals"].append(sig["new_architecture"])
    if not q["quantized"]:
        out["warnings"].append("unquantized: size is the full-precision weights")
    if rule:
        out["warnings"].append(f"§{rule['section']}: {rule['why']}")

    if mtype not in setup.get("architectures", []) and \
            _arch(mtype) not in setup.get("architectures", []):
        out.update(verdict="blocked",
                   why=f"oMLX (mlx_lm {setup.get('mlx_lm')}, mlx_vlm "
                       f"{setup.get('mlx_vlm')}) has no '{mtype}' architecture")
        return out
    if out["signals"] and not rule:
        out.update(verdict="promising", why="; ".join(out["signals"]))
    else:
        out.update(verdict="consider",
                   why=f"§{rule['section']}: {rule['why']}" if rule
                   else "fits and loads; no signal")
    name = repo.split("/", 1)[1]
    out["next_steps"] = [
        f"read the leaderboard first (backlog D3): {LEADERBOARD.format(name=name)}",
        f"on the Mac: python3 bench/model_scout.py download {repo}",
        f"on the Mac: python3 bench/candidate_gate.py {name} "
        f"--label gate_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}",
    ]
    return out


# ---- scan -----------------------------------------------------------------

def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_scan": None, "seen": {}}


def scan(policy: dict, setup: dict, state: dict, since: datetime,
         now: datetime, fetch: Fetch = _fetch, include_seen: bool = False) -> dict:
    """Evaluate everything new and update `state` in place."""
    results, errors = [], []
    recheck = state.get("setup_generated") != setup.get("generated")
    for author in policy["authors"]:
        try:
            entries = list_new(author, since, fetch)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            errors.append(f"{author}: {exc}")
            continue
        for entry in entries:
            repo = entry["id"]
            prior = state["seen"].get(repo)
            if prior and not include_seen and not (recheck and prior == "blocked"):
                continue
            why = prefilter(entry, policy, setup)
            if why:
                r = {"repo": repo, "verdict": "skip", "why": why}
            else:
                try:
                    r = evaluate(repo, inspect(repo, fetch), policy, setup)
                except LookupError as exc:
                    r = {"repo": repo, "verdict": "skip", "why": str(exc)}
                except urllib.error.URLError as exc:
                    errors.append(f"{repo}: {exc}")
                    continue
            r["created"] = entry.get("createdAt")
            state["seen"][repo] = r["verdict"]
            results.append(r)
    results.sort(key=lambda r: r.get("created") or "", reverse=True)
    results.sort(key=lambda r: ORDER[r["verdict"]])  # stable: newest first within
    state["last_scan"] = now.isoformat(timespec="seconds")
    state["setup_generated"] = setup.get("generated")

    warnings = []
    age = (now - _when(setup["generated"])).days
    if age > SETUP_STALE_DAYS:
        warnings.append(f"scout/setup.json is {age} days old: run "
                        "`python3 bench/model_scout.py export` on the Mac")
    families = group(results)
    free, _ = headroom(policy, setup)
    return {"generated": state["last_scan"], "since": since.isoformat(timespec="seconds"),
            "free_beside_lineup_gib": free, "warnings": warnings, "errors": errors,
            "counts": {v: sum(f["verdict"] == v for f in families) for v in ORDER
                       if v != "skip"} | {"skip": sum(r["verdict"] == "skip"
                                                       for r in results)},
            "families": families, "results": results}


def _pick_key(r: dict) -> tuple:
    """Which build of a family to gate first.

    Fits beside the lineup before needing a tier swapped out; then the build
    with more signals (an MTP head is per build); then more bits, because §19
    measured the 8-bit ahead of 4-bit on both axes. Unquantized ranks last: it
    is the source for builds, not a build to serve.
    """
    return (ORDER[r["verdict"]], r.get("fit") != "alongside",
            -len(r.get("signals", [])), -(r.get("bits") or 0))


def group(results: list[dict]) -> list[dict]:
    """Collapse the non-skipped builds of one model into one entry."""
    by: dict[str, list[dict]] = {}
    for r in results:
        if r["verdict"] != "skip":
            by.setdefault(family(r["repo"]), []).append(r)
    out = []
    for name, builds in by.items():
        builds.sort(key=_pick_key)
        pick = builds[0]
        out.append({
            "family": name, "verdict": pick["verdict"], "why": pick["why"],
            "pick": pick["repo"], "fit": pick.get("fit"),
            "params_b": pick.get("params_b"), "model_type": pick.get("model_type"),
            "warnings": pick.get("warnings", []), "next_steps": pick.get("next_steps", []),
            "builds": [{k: b.get(k) for k in ("repo", "verdict", "size_gib", "bits", "fit")}
                       for b in builds],
        })
    out.sort(key=lambda f: (ORDER[f["verdict"]], -len(f["builds"])))
    return out


def render_text(report: dict, show_skips: bool) -> str:
    c = report["counts"]
    lines = [f"model scout, new since {report['since']}: {c['promising']} promising, "
             f"{c['consider']} to consider, {c['blocked']} blocked "
             f"({c['skip']} repos skipped)",
             f"  free beside the lineup: {report['free_beside_lineup_gib']} GiB"]
    lines += [f"  ! {w}" for w in report["warnings"] + report["errors"]]
    for f in report["families"]:
        lines.append(f"\n[{f['verdict'].upper()}] {f['family']}  ~{f['params_b']}B  "
                     f"{f.get('model_type') or ''}")
        lines.append(f"  {f['why']}")
        lines += [f"  warning: {w}" for w in f["warnings"] if w != f["why"]]
        for b in f["builds"]:
            mark = "*" if b["repo"] == f["pick"] else " "
            lines.append(f"  {mark} {b['repo']:<58} {b['size_gib']!s:>6} GiB  "
                         f"{str(b['bits'] or 'bf16'):>4}  {b['fit'] or ''}")
        lines += [f"  -> {s}" for s in f["next_steps"]]
    if show_skips:
        lines.append("\nskipped:")
        lines += [f"  {r['repo']}: {r['why']}" for r in report["results"]
                  if r["verdict"] == "skip"]
    return "\n".join(lines)


# ---- notify ---------------------------------------------------------------

PUSHOVER = "https://api.pushover.net/1/messages.json"
NOTIFY_ON = ("promising", "blocked")


def _post(url: str, payload: dict, timeout: int = 30) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "local-llm/scout"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def notification(report: dict, verdicts: tuple[str, ...] = NOTIFY_ON) -> dict | None:
    """Title, markdown body and one-line summary, or None when nothing is new.

    Warnings (a stale setup.json) and errors are sent too, so a scout that has
    quietly stopped working says so instead of going silent.
    """
    fams = [f for f in report["families"] if f["verdict"] in verdicts]
    alerts = report["warnings"] + report["errors"]
    if not fams and not alerts:
        return None
    promising = [f["family"] for f in fams if f["verdict"] == "promising"]
    title = (f"{len(promising)} promising MLX model{'s' * (len(promising) != 1)}"
             if promising else "Model scout")
    lines = [f"**{title}** (new since {report['since'][:10]})"]
    lines += [f"> {a}" for a in alerts]
    for f in fams:
        pick = next(b for b in f["builds"] if b["repo"] == f["pick"])
        lines += ["", f"**{f['family']}** — {f['verdict']}, ~{f['params_b']}B "
                  f"`{f.get('model_type')}`", f"{f['why']}",
                  f"Try [{f['pick']}](https://huggingface.co/{f['pick']}): "
                  f"{pick['size_gib']} GiB, {pick['fit']}"
                  + (f" ({len(f['builds'])} builds)" if len(f["builds"]) > 1 else "")]
        lines += [f"- warning: {w}" for w in f["warnings"] if w != f["why"]]
        lines += [f"{i}. {s}" for i, s in enumerate(f["next_steps"], 1)]
    summary = ", ".join(f["family"] for f in fams) or alerts[0]
    return {"title": title, "markdown": "\n".join(lines), "summary": summary[:500]}


def send(note: dict, env: dict, post: Callable[[str, dict], None] = _post) -> list[str]:
    """Deliver to every sink configured in `env`. Raises if any of them fails.

      SCOUT_OPENWEBUI_WEBHOOK   Open WebUI channel webhook: the full report
      SCOUT_PUSHOVER_TOKEN      Pushover app token  } a one-line ping, so the
      SCOUT_PUSHOVER_USER       Pushover user key   } phone actually buzzes
      SCOUT_LINK                optional URL the ping opens (the channel)
    """
    sent = []
    if env.get("SCOUT_OPENWEBUI_WEBHOOK"):
        post(env["SCOUT_OPENWEBUI_WEBHOOK"], {"content": note["markdown"]})
        sent.append("openwebui")
    if env.get("SCOUT_PUSHOVER_TOKEN") and env.get("SCOUT_PUSHOVER_USER"):
        ping = {"token": env["SCOUT_PUSHOVER_TOKEN"], "user": env["SCOUT_PUSHOVER_USER"],
                "title": note["title"], "message": note["summary"]}
        if env.get("SCOUT_LINK"):
            ping.update(url=env["SCOUT_LINK"], url_title="Open the scout channel")
        post(PUSHOVER, ping)
        sent.append("pushover")
    return sent


# ---- on the Mac -------------------------------------------------------------

def export_setup() -> dict:
    """What this Mac can load and what each local model costs resident."""
    from candidate_gate import MLX_MODELS  # noqa: PLC0415 — Mac only
    from omlx_client import Omlx  # noqa: PLC0415

    site = MLX_MODELS.parent.parent
    archs = {p.stem for p in (site / "mlx_lm" / "models").glob("*.py")}
    archs |= {p.name for p in (site / "mlx_vlm" / "models").iterdir() if p.is_dir()}
    archs -= {"__pycache__"}

    def version(pkg: str) -> str | None:
        found = sorted(site.glob(f"{pkg}-*.dist-info"))
        return found[0].name[len(pkg) + 1:-len(".dist-info")] if found else None

    models = Omlx().models()
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mlx_lm": version("mlx_lm"), "mlx_vlm": version("mlx_vlm"),
        "architectures": sorted(archs),
        "local_model_types": sorted({m["config_model_type"] for m in models
                                     if m.get("config_model_type")}),
        "source_repos": sorted({m["source_repo_id"] for m in models
                                if m.get("source_repo_id")}),
        "models": [{"id": m["id"], "model_type": m.get("config_model_type"),
                    "engine": m.get("engine_type"),
                    "gib": round((m.get("estimated_size") or 0) / GIB, 1)}
                   for m in sorted(models, key=lambda m: m["id"])],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="evaluate new Hub repos (runs anywhere)")
    s.add_argument("--since", type=float, metavar="DAYS",
                   help="look back DAYS and report repos already seen too")
    s.add_argument("--format", choices=("text", "json"), default="text")
    s.add_argument("--show-skips", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="do not update the state file")
    s.add_argument("--notify", action="store_true",
                   help="send promising/blocked models to the sinks in the "
                        "environment (see send()); state is kept only if delivery works")
    s.add_argument("--notify-consider", action="store_true",
                   help="with --notify or --quiet, include 'consider' too")
    s.add_argument("--quiet", action="store_true",
                   help="print the notification markdown only when there is news, "
                        "else nothing: for schedulers that deliver stdout "
                        "(Hermes Agent --no-agent cron)")
    sub.add_parser("export", help="on the Mac: write scout/setup.json")
    d = sub.add_parser("download", help="on the Mac: queue a Hub download in oMLX")
    d.add_argument("repo")
    args = ap.parse_args()

    if args.cmd == "export":
        SETUP.write_text(json.dumps(export_setup(), indent=2) + "\n")
        print(f"wrote {SETUP}")
        return 0
    if args.cmd == "download":
        from omlx_client import Omlx  # noqa: PLC0415
        rec = Omlx().hf_download(args.repo)
        print(f"queued {args.repo}: {(rec.get('task') or {}).get('status')}")
        return 0

    now = datetime.now(timezone.utc)
    state = _load_state(STATE)
    if args.since is not None:
        since = now - timedelta(days=args.since)
    elif state.get("last_scan"):
        since = _when(state["last_scan"])
    else:
        since = now - timedelta(days=7)
    report = scan(load_policy(), load_setup(), state, since, now,
                  include_seen=args.since is not None)
    if args.notify:
        verdicts = NOTIFY_ON + (("consider",) if args.notify_consider else ())
        note = notification(report, verdicts)
        try:
            sent = send(note, os.environ) if note else []
        except (urllib.error.URLError, OSError) as exc:
            # Not saving the state means the next run reports these again.
            print(f"notify failed, state not saved: {exc}", file=sys.stderr)
            return 2
        if note and not sent:
            print("--notify: no sink configured (SCOUT_OPENWEBUI_WEBHOOK, "
                  "SCOUT_PUSHOVER_TOKEN + SCOUT_PUSHOVER_USER)", file=sys.stderr)
            return 2
        print(f"notified: {', '.join(sent) or 'nothing new'}", file=sys.stderr)
    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1) + "\n")
    if args.quiet:
        verdicts = NOTIFY_ON + (("consider",) if args.notify_consider else ())
        note = notification(report, verdicts)
        if note:
            print(note["markdown"])
    else:
        print(json.dumps(report, indent=2) if args.format == "json"
              else render_text(report, args.show_skips))
    return 1 if report["errors"] and not report["results"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
