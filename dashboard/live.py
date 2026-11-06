#!/usr/bin/env python3
"""Snapshot what is running now into site/running.html (untracked).

    python3 dashboard/live.py          # one snapshot; launchd runs it every 60 s

Read-only against the stack, so it is safe beside a benchmark: three GETs per
run (oMLX /api/status, the accuracy queue status, llm_queue /queue/status),
each with a timeout, none retried. A failed read shows as "unavailable" on the
page instead of failing the job.

oMLX reports a running suite's position (bench_current / bench_total) but not
when it started, so the rate and ETA are measured from the first snapshot that
saw this benchmark id. They are kept in site/.running_state.json.

The page needs no JS: <meta refresh> reloads it and <progress> draws the bar.
One small script only flags the snapshot as stale when launchd has stopped
writing it.

A chain that declared its steps with bench/run_plan.py also gets a lineup:
each step done, running or pending, worked out from the result files that
already exist, plus progress and an ETA over the whole chain.
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "dashboard"))

from assets import STYLE  # noqa: E402

PLAN = ROOT / "results" / ".run_plan.json"
REFRESH_S = 60
STALE_S = 180
# Batches finish several questions at once, so a rate over one or two
# questions is noise. Show none until the window is this wide.
MIN_WINDOW_S = 300
MIN_DONE = 8
HISTORY = 8


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


# ---------------- reads ----------------

def collect() -> dict:
    """One snapshot of the stack. Every read is independent and may fail."""
    snap: dict = {"at": time.time(), "accuracy": None, "omlx": None, "queue": None,
                  "errors": []}
    try:
        from omlx_client import Omlx
        o = Omlx()
        try:
            snap["omlx"] = o.get("/api/status", admin=False, timeout=10)
        except Exception as e:  # noqa: BLE001
            snap["errors"].append(f"oMLX status: {type(e).__name__}")
        try:
            # Slow while a suite is running (bench/accuracy.py:38), hence 30 s.
            snap["accuracy"] = o.get("/admin/api/bench/accuracy/queue/status", timeout=30)
        except Exception as e:  # noqa: BLE001
            snap["errors"].append(f"accuracy queue: {type(e).__name__}")
    except Exception as e:  # noqa: BLE001
        snap["errors"].append(f"oMLX client: {type(e).__name__}")
    snap["plan"] = read_plan(PLAN, ROOT / "results")
    base = os.environ.get("LLM_QUEUE_BASE", "http://127.0.0.1:8001")
    try:
        with urllib.request.urlopen(f"{base}/queue/status", timeout=5) as r:
            snap["queue"] = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        snap["errors"].append(f"llm_queue: {type(e).__name__}")
    return snap


def _valid_step(step) -> bool:
    return (isinstance(step, dict) and isinstance(step.get("label"), str)
            and isinstance(step.get("model"), str)
            and isinstance(step.get("suites"), list) and step["suites"]
            and isinstance(step.get("samples"), int) and step["samples"] >= 0)


def read_plan(path: Path, results: Path) -> dict | None:
    """The declared plan, each step annotated with its result file if one was
    written after the plan started. Local files only. A malformed plan or
    step is dropped, never raised: the rest of the page must still render."""
    try:
        plan = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return None
    if not isinstance(plan.get("started"), (int, float)):
        plan["started"] = 0
    plan["steps"] = [st for st in plan["steps"] if _valid_step(st)]
    for step in plan["steps"]:
        f = results / f"acc_{step['label']}.json"
        step["result"] = None
        try:
            if f.stat().st_mtime >= plan["started"] - 60:
                rs = json.loads(f.read_text()).get("results") or []
                step["result"] = {
                    "scores": {r["benchmark"]: r.get("accuracy") for r in rs},
                    "total": sum(int(r.get("total") or 0) for r in rs),
                    "file": f.name}
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            pass
    return plan


def plan_status(plan: dict, acc: dict | None, cur: dict | None) -> list[dict]:
    """Per step: state done / running / pending / missed, and questions done.

    Totals come from what oMLX reports where it can: `samples` 0 means the
    whole suite, and a suite smaller than `samples` (HumanEval has 164) runs
    fewer. Only the first unfinished step that matches the running model and
    suite is running; an identical later step stays pending. A step is missed
    when it has no result but a later one has started."""
    acc = acc or {}
    prog = acc.get("last_progress") or {}
    run_model = prog.get("model_id") or acc.get("current_model")
    run_suite = prog.get("benchmark")
    bench_total = int(prog.get("bench_total") or 0)
    claimed = False
    rows = []
    for step in plan.get("steps") or []:
        planned = step["samples"] * len(step["suites"])
        if step.get("result"):
            total = step["result"].get("total") or planned
            rows.append({**step, "state": "done", "done_q": total, "total_q": total})
            continue
        if (not claimed and acc.get("running") and run_model == step["model"]
                and run_suite in step["suites"]):
            claimed = True
            idx = step["suites"].index(run_suite)
            per = step["samples"] or bench_total
            n = cur["last_n"] if cur else int(prog.get("bench_current") or 0)
            total = sum((bench_total if i == idx else per) or 0
                        for i in range(len(step["suites"])))
            done = idx * per + n
            rows.append({**step, "state": "running", "done_q": min(done, total or done),
                         "total_q": max(total, done), "suite": run_suite})
        else:
            rows.append({**step, "state": "pending", "done_q": 0, "total_q": planned})
    started = [i for i, r in enumerate(rows) if r["state"] in ("done", "running")]
    last = max(started) if started else -1
    for i, r in enumerate(rows):
        if r["state"] == "pending" and i < last:
            r["state"] = "missed"
    return rows


# ---------------- rate and ETA ----------------

def update_state(state: dict, snap: dict) -> dict:
    """Track the running benchmark across snapshots; move it to history when
    its id changes or the queue goes idle."""
    acc = snap.get("accuracy") or {}
    prog = acc.get("last_progress") or {}
    bid = acc.get("current_bench_id") if acc.get("running") else None
    cur = state.get("current")
    if cur and cur["id"] != bid and snap.get("accuracy") is not None:
        state.setdefault("history", []).insert(0, {
            "benchmark": cur["benchmark"], "model": cur["model"],
            "last_n": cur["last_n"], "total": cur["total"],
            "first_seen": cur["first_t"], "last_seen": cur["last_t"]})
        state["history"] = state["history"][:HISTORY]
        state["current"] = cur = None
    if bid and prog.get("bench_total"):
        n = int(prog.get("bench_current") or 0)
        if cur is None:
            cur = state["current"] = {
                "id": bid, "benchmark": prog.get("benchmark"),
                "model": prog.get("model_id") or acc.get("current_model"),
                "total": int(prog["bench_total"]),
                "first_t": snap["at"], "first_n": n}
        cur.update(last_t=snap["at"], last_n=n, total=int(prog["bench_total"]))
    return state


def rate_eta(cur: dict) -> tuple[float | None, float | None]:
    """(seconds per question, epoch of the expected finish) or Nones."""
    done = cur["last_n"] - cur["first_n"]
    if done < MIN_DONE or cur["last_t"] - cur["first_t"] < MIN_WINDOW_S:
        return None, None
    spq = (cur["last_t"] - cur["first_t"]) / done
    return spq, cur["last_t"] + spq * (cur["total"] - cur["last_n"])


# ---------------- page ----------------

def _hm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _dur(s: float) -> str:
    m = int(s // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def render(snap: dict, state: dict) -> str:
    at = snap["at"]
    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           f'<meta http-equiv="refresh" content="{REFRESH_S}">',
           "<title>Running now — local-LLM stack</title>", STYLE, "</head>", "<body>",
           '<a class="skip" href="#main">Skip to content</a>',
           "<header>", '<div class="head-row">',
           f'<h1>Running now <span class="stamp">[updated: '
           f'{esc(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)))}, '
           f"refreshes every {REFRESH_S} s]</span></h1>",
           '<a class="running-link" href="index.html">Findings</a>',
           "</div>", "</header>", '<main id="main">',
           f'<p class="banner" id="stale" data-at="{int(at)}" hidden>This snapshot is '
           "more than 3 minutes old; the job that writes it may have stopped.</p>"]

    acc0 = snap.get("accuracy")
    plan = snap.get("plan")
    if plan and plan.get("steps"):
        out.append(_plan_html(plan, acc0, state.get("current"), at))

    out.append('<section id="benchmarks-running" aria-labelledby="run-h">')
    out.append('<h2 id="run-h" tabindex="-1">Benchmarks</h2>')
    acc = snap.get("accuracy")
    cur = state.get("current")
    if acc is None:
        out.append('<p class="empty-state">accuracy queue status unavailable</p>')
    elif not acc.get("running") or not cur:
        out.append('<p class="empty-state">no accuracy suite running</p>')
    if acc is not None and acc.get("running") and cur:
        prog = acc.get("last_progress") or {}
        n, total = cur["last_n"], cur["total"]
        pct = 100 * n / total if total else 0
        spq, eta = rate_eta(cur)
        bench = cur["benchmark"] or "?"
        out.append('<div class="cards"><article class="suite run" aria-labelledby="cur-h">')
        out.append(f'<h3 id="cur-h"><a href="index.html#suite-{esc(bench)}">{esc(bench)}</a>'
                   f' on {esc(cur["model"])}</h3>')
        out.append(f'<label for="cur-p" class="meta">{n} of {total} questions</label>')
        out.append(f'<progress id="cur-p" max="{total}" value="{n}">{pct:.0f}%</progress>')
        nums = [f"<span><b>{pct:.1f}%</b> done</span>"]
        if spq is not None:
            nums.append(f"<span><b>{spq:.0f} s</b> per question</span>")
            nums.append(f"<span>ETA <b>{_hm(eta)}</b> (in {_dur(eta - at)})</span>")
        else:
            nums.append(f"<span>rate and ETA after {MIN_WINDOW_S // 60} min and "
                        f"{MIN_DONE} questions of watching</span>")
        out.append(f'<div class="nums">{"".join(nums)}</div>')
        out.append(f'<p class="meta">Phase {esc(prog.get("phase") or "?")}. Watched since '
                   f'{_hm(cur["first_t"])} at question {cur["first_n"]}; rate and ETA are '
                   "measured from then.</p>")
        out.append("</article></div>")
    queued = (acc or {}).get("queue") or []
    if queued:
        out.append(f"<h3>Queued ({len(queued)})</h3><ul>")
        for q in queued:
            if isinstance(q, dict):
                label = " · ".join(str(q[k]) for k in ("model_id", "benchmark", "benchmarks")
                                   if q.get(k))
            else:
                label = str(q)
            out.append(f"<li>{esc(label or 'queued run')}</li>")
        out.append("</ul>")
    out.append('<p class="details">Shows oMLX accuracy suites only. Throughput runs, '
               "the candidate gate and taskbench appear as engine activity below.</p>")
    out.append("</section>")

    out.append('<section id="engine" aria-labelledby="engine-h">')
    out.append('<h2 id="engine-h" tabindex="-1">Engine</h2>')
    st = snap.get("omlx")
    q = snap.get("queue")
    rows = []
    if st:
        loaded = ", ".join(esc(m) for m in st.get("loaded_models") or []) or "none"
        rows += [("Loaded models", loaded),
                 ("Requests", f'{esc(st.get("active_requests"))} active, '
                              f'{esc(st.get("waiting_requests"))} waiting'
                              + (f', {esc(st["models_loading"])} loading'
                                 if st.get("models_loading") else "")),
                 ("Model memory", f'{esc(st.get("model_memory_used_formatted"))} of '
                                  f'{esc(st.get("model_memory_max_formatted"))}'),
                 ("oMLX", esc(st.get("version")))]
    else:
        rows.append(("oMLX", "status unavailable"))
    if q:
        rows.append(("llm_queue", f'{esc(q.get("depth"))} queued, last model '
                                  f'{esc(q.get("current"))}'))
    else:
        rows.append(("llm_queue", "status unavailable"))
    out.append("<table>" + "".join(f"<tr><th scope=\"row\">{k}</th><td>{v}</td></tr>"
                                   for k, v in rows) + "</table>")
    out.append("</section>")

    hist = state.get("history") or []
    out.append('<section id="recent" aria-labelledby="recent-h">')
    out.append('<h2 id="recent-h" tabindex="-1">Recently finished</h2>')
    if not hist:
        out.append('<p class="empty-state">none seen since this page started</p>')
    else:
        out.append("<table><tr><th>suite</th><th>model</th><th>last seen</th>"
                   "<th>questions</th></tr>")
        for h in hist:
            out.append(f'<tr><td>{esc(h["benchmark"])}</td><td>{esc(h["model"])}</td>'
                       f'<td>{esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(h["last_seen"])))}'
                       f'</td><td>{h["last_n"]} of {h["total"]} seen</td></tr>')
        out.append("</table>")
        out.append('<p class="details">Scores are in the result JSON and on the findings '
                   "page once they are written up.</p>")
    out.append("</section>")

    if snap.get("errors"):
        out.append('<p class="details">Unavailable this snapshot: '
                   + esc("; ".join(snap["errors"])) + "</p>")
    out.append("</main>")
    out.append("<script>(function(){var b=document.getElementById('stale');"
               f"if(b&&Date.now()/1000-Number(b.getAttribute('data-at'))>{STALE_S})"
               "b.hidden=false;})();</script>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


GLYPH = {"done": "✓", "running": "▶", "pending": "–", "missed": "✗"}
WORD = {"done": "done", "running": "running", "pending": "pending",
        "missed": "no result; a later step has started"}


def _plan_html(plan: dict, acc: dict | None, cur: dict | None, at: float) -> str:
    rows = plan_status(plan, acc, cur)
    done_q = sum(r["done_q"] for r in rows)
    total_q = sum(r["total_q"] for r in rows) or 1
    pos = next((i for i, r in enumerate(rows) if r["state"] == "running"), None)
    finished = sum(r["state"] == "done" for r in rows)
    out = ['<section id="plan" aria-labelledby="plan-h">',
           f'<h2 id="plan-h" tabindex="-1">Lineup: {esc(plan.get("name") or "plan")}</h2>',
           '<div class="cards"><article class="suite run">',
           f'<p class="meta">{"step " + str(pos + 1) + " of " + str(len(rows)) if pos is not None else str(finished) + " of " + str(len(rows)) + " steps done"}'
           f' · started {_hm(plan.get("started", at))}</p>',
           f'<label for="plan-p" class="meta">{done_q} of {total_q} questions across the lineup</label>',
           f'<progress id="plan-p" max="{total_q}" value="{done_q}">'
           f'{100 * done_q / total_q:.0f}%</progress>']
    spq = rate_eta(cur)[0] if cur else None
    unknown = any(r["state"] == "pending" and r["samples"] == 0 for r in rows)
    if spq is not None and pos is not None and not unknown:
        eta = at + spq * (total_q - done_q)
        out.append(f'<div class="nums"><span><b>{100 * done_q / total_q:.1f}%</b> of the '
                   f'lineup</span><span>lineup ETA <b>{_hm(eta)}</b> (in {_dur(eta - at)}), '
                   "if later steps run at this step's rate</span></div>")
    out.append('<ol class="plan-steps">')
    for r in rows:
        scores = ""
        if r["state"] == "done" and r["result"]:
            sc = [f"{b} <b>{v * 100:.1f}</b>" for b, v in r["result"]["scores"].items()
                  if isinstance(v, (int, float))]
            scores = (f' <span class="meta">{", ".join(sc)} · '
                      f'<a href="../results/{esc(r["result"]["file"])}">raw</a></span>')
        elif r["state"] == "running":
            scores = (f' <span class="meta">{r["done_q"]} of {r["total_q"]} questions'
                      f' · now {esc(r["suite"])}</span>')
        suites = ", ".join(f'<a href="index.html#suite-{esc(x)}">{esc(x)}</a>' for x in r["suites"])
        out.append(f'<li class="plan-step plan-{r["state"]}"><span class="plan-glyph" '
                   f'aria-hidden="true">{GLYPH[r["state"]]}</span><span>'
                   f'<b>{esc(r["model"])}</b> · {suites} × {r["samples"]}'
                   f'<span class="sr"> ({WORD[r["state"]]})</span>{scores}</span></li>')
    out.append("</ol></article></div></section>")
    return "\n".join(out)


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main() -> int:
    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    state_path = site / ".running_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    snap = collect()
    state = update_state(state, snap)
    _write(state_path, json.dumps(state, indent=1))
    _write(site / "running.html", render(snap, state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
