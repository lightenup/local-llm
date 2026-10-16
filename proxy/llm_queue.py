#!/usr/bin/env python3
"""One-at-a-time, group-by-model queue in front of oMLX.

    client -> LiteLLM :4000 -> llm_queue :8001 -> oMLX :8000

Neither oMLX nor LiteLLM can order work across models. oMLX queues per model
and runs different models at the same time. LiteLLM >= 1.103 answers 429 when
a deployment is full instead of queueing, and its beta `/queue` endpoint
queues per model name only. Bifrost hands a streaming request's worker back as
soon as the response headers arrive, so its queue does not serialise
generation. See FINDINGS §38.

Policy:
  * Exactly one inference request runs at a time, across all models. The slot
    is held until the upstream response has been fully relayed (the last SSE
    chunk), or until the client disconnects.
  * Group by model. When the slot frees, the next request for the model that
    just ran goes first, so a resident model is not swapped out between two
    requests for it. After --max-streak consecutive grants for one model, if
    another model is waiting, the model whose oldest request arrived first
    runs next. That bound stops starvation.
  * A full queue, or a wait longer than --max-wait seconds, gets HTTP 429
    with Retry-After.
  * Models are never pinned or loaded here. oMLX's own LRU and TTLs do that.
    The group key is the FULL model id, profile included. A profile can carry
    load-time settings (MTP, ANE prefill, turboquant...), and oMLX then treats
    it as a different engine variant: switching variants unloads and reloads
    the model, and returns 409 if the model is busy (§38).

Only POST /v1/* is gated. Everything else (GET /v1/models, /health, the admin
API) passes straight through. Stdlib only.

Monitoring: GET /metrics (Prometheus text), GET /queue/status (JSON),
--event-log (one JSON line per gated request), and X-LLM-Queue-Wait-Ms /
X-LLM-Queue-Depth on every relayed response. LiteLLM keeps those as
`llm_provider-x-llm-queue-*` in its logging payload.

    python3 proxy/llm_queue.py                       # :8001 -> 127.0.0.1:8000
    curl -s 127.0.0.1:8001/queue/status              # running, waiting, counters
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("llm_queue")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding",
              "te", "trailer", "upgrade", "content-length", "host"}


# --------------------------------------------------------------------------
# Scheduler: no I/O, so it can be tested directly
# --------------------------------------------------------------------------

class QueueFull(Exception):
    pass


class QueueTimeout(Exception):
    pass


@dataclass
class _Waiter:
    model: str
    seq: int
    enqueued: float
    fut: asyncio.Future


@dataclass
class ModelQueue:
    max_streak: int = 4
    max_depth: int = 64
    clock: callable = time.monotonic
    running: str | None = None
    current: str | None = None        # model of the most recent grant
    streak: int = 0
    waiting: dict[str, deque] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=lambda: {
        "granted": 0, "rejected_full": 0, "rejected_timeout": 0,
        "abandoned": 0, "switches": 0})
    _seq: itertools.count = field(default_factory=itertools.count)

    def depth(self) -> int:
        return sum(len(q) for q in self.waiting.values())

    def _take(self, model: str) -> None:
        if model != self.current:
            if self.current is not None:
                self.stats["switches"] += 1
            self.current, self.streak = model, 1
        else:
            self.streak += 1
        self.running = model
        self.stats["granted"] += 1

    async def acquire(self, model: str, timeout: float) -> float:
        """Wait for the slot. Returns seconds waited. Raises QueueFull/QueueTimeout."""
        t0 = self.clock()
        if self.running is None and self.depth() == 0:
            self._take(model)
            return 0.0
        if self.depth() >= self.max_depth:
            self.stats["rejected_full"] += 1
            raise QueueFull(f"queue depth {self.depth()} >= {self.max_depth}")
        fut = asyncio.get_running_loop().create_future()
        w = _Waiter(model, next(self._seq), t0, fut)
        self.waiting.setdefault(model, deque()).append(w)
        try:
            done, _ = await asyncio.wait({fut}, timeout=timeout)
        except asyncio.CancelledError:
            # Client went away while queued. If the slot was handed over in the
            # same tick, hand it on; otherwise just leave the queue.
            if fut.done() and not fut.cancelled():
                self.release()
            else:
                self._remove(w)
            self.stats["abandoned"] += 1
            raise
        if not done:
            self._remove(w)
            fut.cancel()
            self.stats["rejected_timeout"] += 1
            raise QueueTimeout(f"waited {timeout:.0f}s for {model}")
        return self.clock() - t0

    def _remove(self, w: _Waiter) -> None:
        q = self.waiting.get(w.model)
        if q and w in q:
            q.remove(w)
            if not q:
                del self.waiting[w.model]

    def _pick(self) -> str | None:
        models = [m for m, q in self.waiting.items() if q]
        if not models:
            return None
        others = [m for m in models if m != self.current]
        if self.current in models and (self.streak < self.max_streak or not others):
            return self.current
        pool = others or models
        return min(pool, key=lambda m: self.waiting[m][0].seq)

    def release(self) -> None:
        self.running = None
        while (model := self._pick()) is not None:
            w = self.waiting[model].popleft()
            if not self.waiting[model]:
                del self.waiting[model]
            if w.fut.done():           # timed out or cancelled already
                continue
            self._take(model)
            w.fut.set_result(True)
            return

    def snapshot(self) -> dict:
        now = self.clock()
        return {
            "running": self.running, "current": self.current, "streak": self.streak,
            "max_streak": self.max_streak, "depth": self.depth(),
            "waiting": {m: {"n": len(q), "oldest_s": round(now - q[0].enqueued, 1)}
                        for m, q in self.waiting.items() if q},
            "stats": dict(self.stats),
        }


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

@dataclass
class Config:
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8000
    max_wait: float = 600.0
    retry_after: int = 30


async def _read_request(reader: asyncio.StreamReader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = []
    for ln in lines[1:]:
        if ln:
            k, v = ln.split(":", 1)
            headers.append((k.strip(), v.strip()))
    hmap = {k.lower(): v for k, v in headers}
    if "chunked" in hmap.get("transfer-encoding", "").lower():
        body = bytearray()
        while True:
            size = int((await reader.readuntil(b"\r\n")).split(b";")[0], 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                break
            body += await reader.readexactly(size)
            await reader.readexactly(2)
        body = bytes(body)
    else:
        body = await reader.readexactly(int(hmap.get("content-length", "0") or 0))
    return method, target, headers, body


def _model_key(body: bytes) -> str:
    try:
        return str(json.loads(body).get("model") or "?")
    except (ValueError, AttributeError):
        return "?"


def _json_response(status: int, reason: str, obj: dict, extra: str = "") -> bytes:
    data = json.dumps(obj).encode()
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\n{extra}Connection: close\r\n\r\n"
            ).encode() + data


async def _wait_for_eof(reader: asyncio.StreamReader) -> None:
    """Returns when the client closes its side. It sends nothing after the body."""
    while await reader.read(4096):
        pass


BUCKETS = (0.5, 1, 5, 15, 30, 60, 120, 300, 600)


class Metrics:
    """Prometheus text exposition, no client library."""

    def __init__(self):
        self.wait = {}                 # model -> [bucket counts..., +Inf], sum
        self.held = {}                 # model -> [count, sum]
        self.outcomes = {}             # (model, outcome) -> n

    def observe_wait(self, model, s):
        b = self.wait.setdefault(model, [[0] * (len(BUCKETS) + 1), 0.0])
        for i, le in enumerate(BUCKETS):
            if s <= le:
                b[0][i] += 1
        b[0][-1] += 1
        b[1] += s

    def observe_held(self, model, s):
        h = self.held.setdefault(model, [0, 0.0])
        h[0] += 1
        h[1] += s

    def outcome(self, model, outcome):
        k = (model, outcome)
        self.outcomes[k] = self.outcomes.get(k, 0) + 1

    def render(self, q: "ModelQueue") -> str:
        snap = q.snapshot()
        out = ["# HELP llm_queue_depth Requests waiting for the slot.",
               "# TYPE llm_queue_depth gauge", f"llm_queue_depth {snap['depth']}",
               "# HELP llm_queue_busy 1 while a request holds the slot.",
               "# TYPE llm_queue_busy gauge",
               f"llm_queue_busy {0 if snap['running'] is None else 1}",
               "# HELP llm_queue_running Model holding the slot (1) by model.",
               "# TYPE llm_queue_running gauge"]
        if snap["running"]:
            out.append(f'llm_queue_running{{model="{snap["running"]}"}} 1')
        out += ["# HELP llm_queue_oldest_wait_seconds Age of the oldest waiter.",
                "# TYPE llm_queue_oldest_wait_seconds gauge",
                "llm_queue_oldest_wait_seconds "
                f"{max([w['oldest_s'] for w in snap['waiting'].values()] or [0])}",
                "# HELP llm_queue_switches_total Grants that changed model.",
                "# TYPE llm_queue_switches_total counter",
                f"llm_queue_switches_total {snap['stats']['switches']}",
                "# HELP llm_queue_requests_total Finished gated requests by outcome.",
                "# TYPE llm_queue_requests_total counter"]
        for (m, o), n in sorted(self.outcomes.items()):
            out.append(f'llm_queue_requests_total{{model="{m}",outcome="{o}"}} {n}')
        out += ["# HELP llm_queue_wait_seconds Time queued before the slot.",
                "# TYPE llm_queue_wait_seconds histogram"]
        for m, (counts, total) in sorted(self.wait.items()):
            for le, c in zip([*map(str, BUCKETS), "+Inf"], counts):
                out.append(f'llm_queue_wait_seconds_bucket{{model="{m}",le="{le}"}} {c}')
            out.append(f'llm_queue_wait_seconds_sum{{model="{m}"}} {total:.3f}')
            out.append(f'llm_queue_wait_seconds_count{{model="{m}"}} {counts[-1]}')
        out += ["# HELP llm_queue_held_seconds Time holding the slot.",
                "# TYPE llm_queue_held_seconds summary"]
        for m, (n, total) in sorted(self.held.items()):
            out.append(f'llm_queue_held_seconds_sum{{model="{m}"}} {total:.3f}')
            out.append(f'llm_queue_held_seconds_count{{model="{m}"}} {n}')
        return "\n".join(out) + "\n"


def _text_response(text: str, ctype="text/plain; version=0.0.4") -> bytes:
    data = text.encode()
    return (f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data


async def _race(fut_coro, eof):
    """Await fut_coro unless the client closes first. Returns (done, result)."""
    task = asyncio.ensure_future(fut_coro)
    waits = {task} | ({eof} if eof and not eof.done() else set())
    done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    if task not in done:
        task.cancel()
        return False, None
    return True, task.result()


class Server:
    def __init__(self, cfg: Config, queue: ModelQueue, event_log: str | None = None):
        self.cfg, self.q, self.event_log = cfg, queue, event_log
        self.metrics = Metrics()

    def _event(self, **kw):
        if not self.event_log:
            return
        kw = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw}
        try:
            with open(self.event_log, "a") as f:
                f.write(json.dumps(kw) + "\n")
        except OSError as e:
            log.warning("event log write failed: %s", e)

    async def handle(self, reader, writer):
        granted = False
        model, waited, held, status, outcome = None, 0.0, 0.0, None, None
        try:
            try:
                method, target, headers, body = await _read_request(reader)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
                return
            if method == "GET" and target.startswith("/queue/status"):
                writer.write(_json_response(200, "OK", self.q.snapshot()))
                await writer.drain()
                return
            if method == "GET" and target.startswith("/metrics"):
                writer.write(_text_response(self.metrics.render(self.q)))
                await writer.drain()
                return

            gated = method == "POST" and target.startswith("/v1/")
            eof = None
            if gated:
                model = _model_key(body)
                eof = asyncio.ensure_future(_wait_for_eof(reader))
                acq = asyncio.ensure_future(self.q.acquire(model, self.cfg.max_wait))
                done, _ = await asyncio.wait({acq, eof},
                                             return_when=asyncio.FIRST_COMPLETED)
                if acq not in done:                 # client left while queued
                    acq.cancel()
                    await asyncio.gather(acq, return_exceptions=True)
                    outcome = "abandoned_queued"
                    log.info("abandoned  %s (client closed while queued)", model)
                    return
                try:
                    waited = acq.result()
                except (QueueFull, QueueTimeout) as e:
                    eof.cancel()
                    outcome = "rejected_full" if isinstance(e, QueueFull) \
                        else "rejected_timeout"
                    status = 429
                    log.warning("429        %s: %s", model, e)
                    writer.write(_json_response(
                        429, "Too Many Requests",
                        {"error": {"message": f"llm_queue: {e}", "type": "queue_busy",
                                   "code": 429, "queue": self.q.snapshot()}},
                        f"Retry-After: {self.cfg.retry_after}\r\n"))
                    await writer.drain()
                    return
                granted = True
                self.metrics.observe_wait(model, waited)
                log.info("grant      %s waited=%.1fs depth=%d streak=%d",
                         model, waited, self.q.depth(), self.q.streak)

            t0 = time.monotonic()
            extra = (f"X-LLM-Queue-Wait-Ms: {int(waited * 1000)}\r\n"
                     f"X-LLM-Queue-Depth: {self.q.depth()}\r\n") if gated else ""
            status, client_gone = await self._relay(
                method, target, headers, body, writer, eof, extra)
            held = time.monotonic() - t0
            if gated:
                outcome = "client_gone" if client_gone else "ok"
                log.info("release    %s held=%.1fs status=%s%s", model, held, status,
                         " (client gone)" if client_gone else "")
        except (ConnectionError, OSError) as e:
            if model is not None and outcome is None:
                outcome = "upstream_error" if granted and status is None else "client_gone"
                log.warning("error      %s: %s", model, e)
        except asyncio.CancelledError:
            pass
        finally:
            if granted:
                self.q.release()
                self.metrics.observe_held(model, held)
            if model is not None and outcome is not None:
                self.metrics.outcome(model, outcome)
                self._event(model=model, outcome=outcome, status=status,
                            waited_s=round(waited, 2), held_s=round(held, 2),
                            depth=self.q.depth())
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _relay(self, method, target, headers, body, writer, eof, extra=""):
        """Returns (upstream status or None, client_gone)."""
        ur, uw = await asyncio.open_connection(self.cfg.upstream_host,
                                               self.cfg.upstream_port)
        try:
            out = [f"{method} {target} HTTP/1.1",
                   f"Host: {self.cfg.upstream_host}:{self.cfg.upstream_port}"]
            out += [f"{k}: {v}" for k, v in headers if k.lower() not in HOP_BY_HOP]
            out += [f"Content-Length: {len(body)}", "Connection: close", "", ""]
            uw.write("\r\n".join(out).encode("latin-1") + body)
            await uw.drain()
            # Non-streaming responses send headers only at the end, so the head
            # read races the client's EOF too.
            ok, head = await _race(ur.readuntil(b"\r\n\r\n"), eof)
            if not ok:
                return None, True
            status = int(head.split(b" ", 2)[1])
            writer.write(head[:-2] + extra.encode() + b"\r\n")
            await writer.drain()
            # Pipe the rest until upstream closes. Connection: close makes
            # upstream EOF the end of the response, whatever its framing.
            while True:
                ok, chunk = await _race(ur.read(65536), eof)
                if not ok:
                    return status, True
                if not chunk:
                    return status, False
                writer.write(chunk)
                await writer.drain()
        finally:
            if eof:
                eof.cancel()
            uw.close()


async def serve(host: str, port: int, cfg: Config, queue: ModelQueue,
                event_log: str | None = None):
    srv = Server(cfg, queue, event_log)
    server = await asyncio.start_server(srv.handle, host, port, limit=1 << 20)
    server.app = srv
    return server


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--upstream", default="127.0.0.1:8000")
    ap.add_argument("--max-wait", type=float, default=600.0,
                    help="seconds a request may queue before 429 (default 600)")
    ap.add_argument("--max-depth", type=int, default=64)
    ap.add_argument("--max-streak", type=int, default=4,
                    help="consecutive grants for one model before a waiting "
                         "model gets a turn (default 4)")
    ap.add_argument("--retry-after", type=int, default=30)
    ap.add_argument("--event-log", default=None,
                    help="append one JSON line per gated request to this file")
    ap.add_argument("--metrics-listen", default=None, metavar="HOST:PORT",
                    help="extra listener serving only GET /metrics and "
                         "/queue/status, e.g. 0.0.0.0:9464 for a LAN Prometheus")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    uh, up = args.upstream.rsplit(":", 1)
    cfg = Config(uh, int(up), args.max_wait, args.retry_after)
    q = ModelQueue(max_streak=args.max_streak, max_depth=args.max_depth)

    async def run():
        server = await serve(args.listen, args.port, cfg, q, args.event_log)
        if args.metrics_listen:
            mh, mp = args.metrics_listen.rsplit(":", 1)
            app = server.app

            async def metrics_only(reader, writer):
                try:
                    method, target, _, _ = await _read_request(reader)
                    if method == "GET" and target.startswith("/metrics"):
                        writer.write(_text_response(app.metrics.render(q)))
                    elif method == "GET" and target.startswith("/queue/status"):
                        writer.write(_json_response(200, "OK", q.snapshot()))
                    else:
                        writer.write(_json_response(404, "Not Found", {}))
                    await writer.drain()
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    writer.close()
            await asyncio.start_server(metrics_only, mh, int(mp))
            log.info("metrics on %s", args.metrics_listen)
        log.info("llm_queue on %s:%d -> %s (max_wait=%.0fs streak=%d depth=%d)",
                 args.listen, args.port, args.upstream, args.max_wait,
                 args.max_streak, args.max_depth)
        async with server:
            await server.serve_forever()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
