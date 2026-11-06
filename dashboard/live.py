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
    base = os.environ.get("LLM_QUEUE_BASE", "http://127.0.0.1:8001")
    try:
        with urllib.request.urlopen(f"{base}/queue/status", timeout=5) as r:
            snap["queue"] = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        snap["errors"].append(f"llm_queue: {type(e).__name__}")
    return snap


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
