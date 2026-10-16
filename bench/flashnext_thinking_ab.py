#!/usr/bin/env python3
"""Within-model A/B: what thinking costs and earns on Qwen3.8-Flash-Next.

Every other model in this repo is benchmarked with thinking OFF, because the
27B's chat template defaults it off and `accuracy.py --thinking` defaults to
False. `qwen4_exp` inverts that: the template turns thinking ON at
`reasoning_effort: xhigh` unless something pins it off. That single difference
can silently produce two incomparable kinds of run, so it gets measured directly
and cheaply here rather than being assumed inside an eight-hour suite.

Three arms, one model, one load, identical prompts and identical token cap:

  off        vendor non-thinking sampling MINUS the presence penalty
             (temp 0.7, top_p 0.80, top_k 20, presence_penalty 0.0)
  on         vendor thinking sampling (temp 1.0, top_p 0.95, top_k 20)
  vendor_pp  identical to `off` but presence_penalty 1.5, the value the vendor
             card actually specifies for non-thinking

The third arm exists to price an invariant, not to find a winner. FINDINGS §7
established that a non-zero presence penalty defeats MTP speculation in oMLX:
the draft head proposes tokens that the penalised distribution then rejects, so
acceptance collapses and decode falls back to one token per forward pass. The
vendor asks for 1.5. Either we disobey the card and keep the MTP win, or we obey
it and pay for it — this arm says how much, in tok/s, on this machine.

WHAT WOULD FALSIFY THE WHOLE ARM
--------------------------------
If `off` still returns reasoning content, `chat_template_kwargs` is not reaching
the template and no thinking-off measurement anywhere in this arm means what it
says — including `accuracy.py`'s, which has no `chat_template_kwargs` plumbing
at all and relies entirely on oMLX honouring the `enable_thinking` setting. That
is checked first, in preflight, and the script aborts on failure instead of
producing numbers. Spending suite hours before this check passes is the mistake
this file exists to prevent.

The cap is deliberately the same for all three arms. A thinking arm that spends
its whole budget in `reasoning_content` and returns empty `content` has not
answered the prompt, and its cap-hit rate is the finding, not a nuisance to be
tuned away.

    python3 bench/flashnext_thinking_ab.py --label fn_thinking
    python3 bench/flashnext_thinking_ab.py --arms off on --repeats 2
    python3 bench/flashnext_thinking_ab.py --max-tokens 4096 --skip-verify
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx, OmlxError  # noqa: E402
from taskbench import PY, pycode_verifier  # noqa: E402
from throughput import VARIANT_KEYS  # noqa: E402

FN = "Qwen3.8-Flash-Next-oQ4e-mtp"

# Sampling per the vendor card. presence_penalty is split out of the non-thinking
# recipe on purpose so the MTP-hostile part is one arm rather than a hidden term.
ARMS: dict[str, dict] = {
    "off": {
        "enable_thinking": False,
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "presence_penalty": 0.0,
    },
    "on": {
        "enable_thinking": True,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "presence_penalty": 0.0,
    },
    "vendor_pp": {
        "enable_thinking": False,
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "presence_penalty": 1.5,
    },
}

def build_tasks() -> list[dict]:
    """Small, verifiable, and shaped like the work this model would actually do.

    This measures token economics and template behaviour, not capability —
    capability is what the paired accuracy suites are for. But the tasks are
    verified anyway, because an arm that emits prose, or emits nothing, has to
    score zero rather than score blank.

    The prompt prefix and the verifier chain are imported from taskbench rather
    than rewritten, so a pass here means the same thing a pass there means.
    Assertions are single-line: `CommandVerifier` shells out through
    `shlex.split` of `python3 -c "..."`, so a multi-line assertion would not
    survive quoting. Braces are safe — `_substitute` replaces only {file}/{dir}.
    """
    return [
        {
            "id": "py-merge",
            "prompt": PY
            + "Write `merge_intervals(intervals)` taking a list of [start, end] "
            "pairs and returning them merged and sorted by start.",
            "assertion": "assert merge_intervals([[1,3],[2,6],[8,10]])==[[1,6],[8,10]];"
            "assert merge_intervals([])==[]",
        },
        {
            "id": "py-chunk",
            "prompt": PY
            + "Write `chunk(xs, n)` splitting a list into consecutive lists of "
            "length n, the last one shorter if it does not divide evenly.",
            "assertion": "assert chunk([1,2,3,4,5],2)==[[1,2],[3,4],[5]];"
            "assert chunk([],3)==[]",
        },
        {
            "id": "py-parse",
            "prompt": PY
            + "Write `parse_kv(text)` parsing lines of `key=value` into a dict, "
            "ignoring blank lines and lines starting with #, and stripping "
            "whitespace around both key and value.",
            "assertion": "assert parse_kv('a = 1\\n\\n#c\\nb=2')=={'a':'1','b':'2'}",
        },
        {
            "id": "py-topk",
            "prompt": PY
            + "Write `top_k_frequent(words, k)` returning the k most frequent "
            "words, most frequent first, ties broken alphabetically.",
            "assertion": "assert top_k_frequent(['b','a','b','c','a','b'],2)==['b','a']",
        },
    ]


def ask(
    o: Omlx,
    model: str,
    arm: dict,
    prompt: str,
    max_tokens: int,
    template_kwargs: bool = True,
) -> dict:
    """One completion, with thinking pinned in both places it can be read.

    `chat_template_kwargs` drives the template; the bare `enable_thinking` field
    drives oMLX's own handling. They are set to the same value because a
    disagreement between them is not resolvable from the outside, and pinning
    only one leaves the other at the template's ON default.

    `template_kwargs=False` drops the template field, for the case where the
    server rejects it as unknown — see `preflight`.
    """
    body = {
        "max_tokens": max_tokens,
        "temperature": arm["temperature"],
        "top_p": arm["top_p"],
        "top_k": arm["top_k"],
        "presence_penalty": arm["presence_penalty"],
        "enable_thinking": arm["enable_thinking"],
    }
    if template_kwargs:
        body["chat_template_kwargs"] = {"enable_thinking": arm["enable_thinking"]}
    t0 = time.perf_counter()
    resp = o.chat([{"role": "user", "content": prompt}], model=model, **body)
    wall = time.perf_counter() - t0

    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = resp.get("usage") or {}
    content = msg.get("content") or ""
    # Servers disagree on the field name, and the count matters more than the text.
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    ct = usage.get("completion_tokens", 0)

    return {
        "wall_s": round(wall, 3),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": ct,
        "reasoning_chars": len(reasoning),
        "content_chars": len(content),
        "tok_per_s": round(ct / wall, 2) if wall else 0.0,
        "finish_reason": choice.get("finish_reason"),
        "cap_hit": choice.get("finish_reason") == "length",
        "empty_content": not content.strip(),
        "text": content,
    }


def preflight(o: Omlx, model: str, max_tokens: int) -> dict:
    """Prove the thinking switch is actually connected before measuring anything.

    Two calls, same trivial prompt, opposite settings. The pass condition is
    asymmetry: thinking-off must not produce reasoning tokens. If both arms look
    the same, the switch is decorative and every downstream thinking-off number
    in this repo is mislabelled for this model.
    """
    print("\n== preflight: is the thinking switch connected?", flush=True)
    probe = "In one short sentence, what does a hash table do?"

    # Does the server even accept chat_template_kwargs? An unknown-field 400 and
    # a dead server look the same from a traceback, and they need opposite
    # responses, so they get told apart here rather than guessed at later.
    template_kwargs = True
    try:
        ask(o, model, ARMS["off"], "hi", 16, template_kwargs=True)
    except (OmlxError, OSError) as e:
        print(f"  chat_template_kwargs rejected: {type(e).__name__}: {e}")
        try:
            ask(o, model, ARMS["off"], "hi", 16, template_kwargs=False)
        except (OmlxError, OSError) as e2:
            print(f"  !! plain request also failed: {type(e2).__name__}: {e2}")
            return {"ok": False, "reason": f"server not answering: {e2}"}
        template_kwargs = False
        print(
            "  -> falling back to the bare enable_thinking field; thinking now "
            "depends entirely on oMLX honouring it, which is the thing being tested"
        )

    out = {}
    for name in ("off", "on"):
        try:
            r = ask(o, model, ARMS[name], probe, max_tokens, template_kwargs)
        except (OmlxError, OSError) as e:
            print(f"  !! {name}: {type(e).__name__}: {e}")
            return {"ok": False, "reason": f"{name} probe failed: {e}"}
        out[name] = r
        print(
            f"  {name:<4} reasoning_chars={r['reasoning_chars']:<7} "
            f"content_chars={r['content_chars']:<7} "
            f"completion_tokens={r['completion_tokens']:<6} "
            f"{r['tok_per_s']} tok/s",
            flush=True,
        )

    off_thinks = out["off"]["reasoning_chars"] > 0
    off_empty = out["off"]["empty_content"]
    verdict = {
        "ok": not off_thinks and not off_empty,
        "template_kwargs_accepted": template_kwargs,
        "off_reasoning_chars": out["off"]["reasoning_chars"],
        "on_reasoning_chars": out["on"]["reasoning_chars"],
        "off_empty_content": off_empty,
    }
    if off_thinks:
        verdict["reason"] = (
            "thinking-off still emitted reasoning content: chat_template_kwargs "
            "is not reaching the template, so no thinking-off arm on this model "
            "is trustworthy (accuracy.py has no chat_template_kwargs path at all)"
        )
    elif off_empty:
        verdict["reason"] = "thinking-off returned empty content on a trivial prompt"
    else:
        verdict["reason"] = "thinking-off is genuinely off"
        if out["on"]["reasoning_chars"] == 0:
            verdict["reason"] += (
                "; NOTE thinking-on emitted no reasoning either, so the ON arm may "
                "be silently disabled rather than the OFF arm being honoured"
            )
    print(f"  -> {verdict['reason']}")
    return verdict


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for arm in ARMS:
        rs = [r for r in rows if r["arm"] == arm and r.get("ok_call")]
        if not rs:
            continue
        toks = [r["completion_tokens"] for r in rs]
        verified = [r for r in rs if r.get("ran")]
        out.append(
            {
                "arm": arm,
                "n": len(rs),
                "tok_per_s_median": round(statistics.median(r["tok_per_s"] for r in rs), 2),
                "completion_tokens_median": int(statistics.median(toks)),
                "completion_tokens_total": sum(toks),
                "wall_s_median": round(statistics.median(r["wall_s"] for r in rs), 2),
                "cap_hit_rate": round(sum(r["cap_hit"] for r in rs) / len(rs), 3),
                "empty_content_rate": round(
                    sum(r["empty_content"] for r in rs) / len(rs), 3
                ),
                "reasoning_chars_median": int(
                    statistics.median(r["reasoning_chars"] for r in rs)
                ),
                "verified_n": len(verified),
                "passed": sum(1 for r in verified if r["ok"]),
                "pass_rate": (
                    round(sum(1 for r in verified if r["ok"]) / len(verified), 3)
                    if verified
                    else None
                ),
            }
        )
    return out


def print_table(summary: list[dict]) -> None:
    hdr = (
        f"  {'arm':<10} {'n':>3} {'tok/s':>7} {'tokens':>7} {'wall s':>7} "
        f"{'cap%':>6} {'empty%':>7} {'think ch':>9} {'pass':>7}"
    )
    print("\n" + hdr)
    print("  " + "-" * (len(hdr) - 2))
    for s in summary:
        pr = f"{s['pass_rate'] * 100:.0f}%" if s["pass_rate"] is not None else "-"
        print(
            f"  {s['arm']:<10} {s['n']:>3} {s['tok_per_s_median']:>7} "
            f"{s['completion_tokens_median']:>7} {s['wall_s_median']:>7} "
            f"{s['cap_hit_rate'] * 100:>5.0f}% {s['empty_content_rate'] * 100:>6.0f}% "
            f"{s['reasoning_chars_median']:>9} {pr:>7}"
        )

    by = {s["arm"]: s for s in summary}
    if "off" in by and "vendor_pp" in by:
        a, b = by["off"]["tok_per_s_median"], by["vendor_pp"]["tok_per_s_median"]
        if a:
            # The sentence used to assert a cost whichever way the numbers went,
            # so the 2026-09-16 run printed "costs -15% of decode (41.23 ->
            # 47.35 tok/s)" -- a 15% GAIN, described as a cost. The magnitude
            # was right and only the prose was wrong, but it is the line someone
            # reads instead of the table, so it decides direction explicitly.
            delta = (b / a - 1) * 100
            verdict = (
                f"costs {-delta:.0f}% of decode"
                if delta < 0
                else f"gains {delta:.0f}% of decode"
            )
            print(
                f"\n  presence_penalty 1.5 {verdict} ({a} -> {b} tok/s)."
                f"{' That is the price of obeying the vendor card.' if delta < 0 else ''}"
            )
            if abs(delta) < 5:
                print(
                    "  At n=4 tasks x 1 repeat that is inside the noise either "
                    "way; do not read a direction off it."
                )
    if "off" in by and "on" in by:
        ratio = by["on"]["completion_tokens_total"] / max(
            by["off"]["completion_tokens_total"], 1
        )
        print(
            f"  thinking-on spends {ratio:.1f}x the completion tokens of "
            f"thinking-off at the same cap."
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", default=FN)
    ap.add_argument("--label", default="fn_thinking")
    ap.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help="measure even if preflight says the thinking switch is not connected",
    )
    args = ap.parse_args()

    o = Omlx()
    tasks = build_tasks()

    print(f"== flashnext thinking A/B on {args.model}")
    print(f"   arms: {', '.join(args.arms)}")
    print(f"   {len(tasks)} tasks x {args.repeats} repeat(s), cap {args.max_tokens}")

    try:
        settings = o.settings(args.model)
    except (OmlxError, OSError) as e:
        print(f"!! cannot read settings for {args.model}: {type(e).__name__}: {e}")
        print("   is the checkpoint downloaded and the model known to oMLX?")
        return 2
    variant = {k: settings.get(k) for k in VARIANT_KEYS}
    print(
        f"   mtp_enabled={variant.get('mtp_enabled')} "
        f"qwen4_ple_ssd_offload={variant.get('qwen4_ple_ssd_offload')}"
    )

    pf = preflight(o, args.model, args.max_tokens)
    if not pf["ok"] and not args.force:
        print("\n!! aborting before measurement: the A/B would be meaningless.")
        print("   re-run with --force only to capture the failure itself.")
        Omlx.save(
            args.label,
            {
                "label": args.label,
                "model_id": args.model,
                "aborted": "preflight",
                "preflight": pf,
                "variant": variant,
            },
        )
        return 3

    rows: list[dict] = []
    for arm in args.arms:
        print(f"\n== arm {arm}  {json.dumps(ARMS[arm])}", flush=True)
        for rep in range(args.repeats):
            for t in tasks:
                row = {"arm": arm, "id": t["id"], "rep": rep}
                try:
                    r = ask(
                        o,
                        args.model,
                        ARMS[arm],
                        t["prompt"],
                        args.max_tokens,
                        pf.get("template_kwargs_accepted", True),
                    )
                    row.update(r)
                    row["ok_call"] = True
                except (OmlxError, OSError) as e:
                    row.update(
                        {"ok_call": False, "error": f"{type(e).__name__}: {e}"}
                    )
                    print(f"  !! {t['id']} rep{rep}: {row['error']}", flush=True)
                    rows.append(row)
                    continue

                if args.skip_verify:
                    row["ran"] = False
                    row["ok"] = False
                    row["reason"] = "verification skipped"
                else:
                    v = pycode_verifier(t["assertion"]).verify(row.pop("text", ""))
                    row.update(
                        {
                            "ok": v.ok,
                            "ran": v.ran,
                            "reason": v.reason,
                            "detail": v.detail[:200],
                        }
                    )
                row.pop("text", None)
                print(
                    f"  {t['id']:<10} rep{rep} "
                    f"{'PASS' if row.get('ok') else ('FAIL' if row.get('ran') else '----')} "
                    f"{row['completion_tokens']:>5} tok  {row['tok_per_s']:>6} tok/s"
                    f"{'  CAP' if row['cap_hit'] else ''}"
                    f"{'  EMPTY' if row['empty_content'] else ''}",
                    flush=True,
                )
                rows.append(row)

    summary = summarize(rows)
    print_table(summary)

    payload = {
        "label": args.label,
        "model_id": args.model,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "request": {
            "arms": args.arms,
            "arm_sampling": {k: ARMS[k] for k in args.arms},
            "repeats": args.repeats,
            "max_tokens": args.max_tokens,
            "tasks": [t["id"] for t in tasks],
            "verified": not args.skip_verify,
        },
        "variant": variant,
        "preflight": pf,
        "summary": summary,
        "rows": rows,
    }
    Omlx.save(args.label, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
