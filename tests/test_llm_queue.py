"""Tests for proxy/llm_queue.py: scheduler policy and HTTP relay behaviour.

Scheduler:
- immediate grant when idle; one slot globally
- group by model: the current model's waiters go first
- the max_streak bound hands the slot to the model that has waited longest
- 429 paths: depth cap, wait timeout
- a cancelled waiter leaves the queue; a cancel racing a grant hands the slot on

HTTP (fake upstream on an ephemeral port):
- a streaming response holds the slot until the last chunk is relayed
- GET passes through ungated while the slot is busy
- a timeout returns 429 with Retry-After
- a client disconnect mid-stream releases the slot
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))
import llm_queue as lq  # noqa: E402


class SchedulerTests(unittest.IsolatedAsyncioTestCase):

    async def test_idle_grant_is_immediate(self):
        q = lq.ModelQueue()
        self.assertEqual(await q.acquire("A", 1), 0.0)
        self.assertEqual(q.running, "A")

    async def test_one_slot_globally(self):
        q = lq.ModelQueue()
        await q.acquire("A", 1)
        t = asyncio.ensure_future(q.acquire("B", 5))
        await asyncio.sleep(0.01)
        self.assertFalse(t.done())
        q.release()
        await t
        self.assertEqual(q.running, "B")

    async def _drain_order(self, q, arrivals):
        """Hold the slot, queue `arrivals` in order, then record grant order."""
        await q.acquire(arrivals[0][0] if False else "HOLD", 1)
        q.current, q.streak = arrivals[0][1], 1   # pretend this model just ran
        order = []

        async def one(m):
            await q.acquire(m, 5)
            order.append(m)
            await asyncio.sleep(0)
            q.release()
        tasks = []
        for m, _ in arrivals:
            tasks.append(asyncio.ensure_future(one(m)))
            await asyncio.sleep(0)
        q.release()
        await asyncio.gather(*tasks)
        return order

    async def test_group_by_model(self):
        # A is resident. Arrivals interleave A and B; A's run first, then B's.
        q = lq.ModelQueue(max_streak=10)
        order = await self._drain_order(
            q, [("B", "A"), ("A", None), ("B", None), ("A", None)])
        self.assertEqual(order, ["A", "A", "B", "B"])

    async def test_streak_bound_prevents_starvation(self):
        q = lq.ModelQueue(max_streak=2)
        arrivals = [("B", "A")] + [("A", None)] * 4
        order = await self._drain_order(q, arrivals)
        # current=A streak=1: A gets one more (streak 2), then B, then the rest of A.
        self.assertEqual(order, ["A", "B", "A", "A", "A"])

    async def test_oldest_waiting_model_wins_switch(self):
        q = lq.ModelQueue(max_streak=1)
        order = await self._drain_order(q, [("C", "A"), ("B", None), ("A", None)])
        # streak exhausted for A: C arrived before B, so C, then B, then A.
        self.assertEqual(order[0], "C")

    async def test_depth_cap_429(self):
        q = lq.ModelQueue(max_depth=1)
        await q.acquire("A", 1)
        t = asyncio.ensure_future(q.acquire("A", 5))
        await asyncio.sleep(0)
        with self.assertRaises(lq.QueueFull):
            await q.acquire("B", 5)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        self.assertEqual(q.stats["rejected_full"], 1)

    async def test_wait_timeout_429_and_dequeued(self):
        q = lq.ModelQueue()
        await q.acquire("A", 1)
        with self.assertRaises(lq.QueueTimeout):
            await q.acquire("B", 0.05)
        self.assertEqual(q.depth(), 0)
        q.release()
        self.assertIsNone(q.running)

    async def test_cancelled_waiter_leaves_queue(self):
        q = lq.ModelQueue()
        await q.acquire("A", 1)
        t = asyncio.ensure_future(q.acquire("B", 5))
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        self.assertEqual(q.depth(), 0)
        q.release()
        self.assertIsNone(q.running)

    async def test_cancel_racing_grant_hands_slot_on(self):
        q = lq.ModelQueue()
        await q.acquire("A", 1)
        t1 = asyncio.ensure_future(q.acquire("B", 5))
        t2 = asyncio.ensure_future(q.acquire("C", 5))
        await asyncio.sleep(0)
        q.release()            # grants B's future...
        t1.cancel()            # ...but B's client is gone in the same tick
        await asyncio.gather(t1, return_exceptions=True)
        await asyncio.wait_for(t2, 1)
        self.assertEqual(q.running, "C")

    def test_model_key_keeps_profile(self):
        # Profiles can be distinct engine variants in oMLX (reload + 409).
        self.assertEqual(lq._model_key(b'{"model":"Qwen3.8-27B-8bit:qwen-reason"}'),
                         "Qwen3.8-27B-8bit:qwen-reason")
        self.assertEqual(lq._model_key(b"not json"), "?")


# --------------------------------------------------------------------------

class FakeUpstream:
    """Streams N SSE chunks with a delay; records concurrency high-water mark."""

    def __init__(self, chunks=5, delay=0.05):
        self.chunks, self.delay = chunks, delay
        self.active = self.peak = 0
        self.order = []

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        line = head.split(b"\r\n")[0].decode()
        hdrs = {ln.split(b":", 1)[0].strip().lower(): ln.split(b":", 1)[1].strip()
                for ln in head.split(b"\r\n")[1:] if b":" in ln}
        body = await reader.readexactly(int(hdrs.get(b"content-length", b"0")))
        if line.startswith("GET"):
            data = b'{"data":[]}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
                         % (len(data), data))
            await writer.drain()
            writer.close()
            return
        self.order.append(json.loads(body)["model"])
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            for i in range(self.chunks):
                await asyncio.sleep(self.delay)
                writer.write(b"data: {\"i\": %d}\n\n" % i)
                await writer.drain()
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.active -= 1
            writer.close()


async def _post(port, model, read_all=True):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps({"model": model, "stream": True}).encode()
    w.write(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
            % (len(body), body))
    await w.drain()
    if not read_all:
        return r, w
    data = await r.read()
    w.close()
    return data


class HttpTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.up = FakeUpstream()
        self.up_srv = await asyncio.start_server(self.up.handle, "127.0.0.1", 0)
        up_port = self.up_srv.sockets[0].getsockname()[1]
        self.q = lq.ModelQueue(max_streak=4)
        self.cfg = lq.Config("127.0.0.1", up_port, max_wait=5, retry_after=7)
        self.srv = await lq.serve("127.0.0.1", 0, self.cfg, self.q)
        self.port = self.srv.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.srv.close()
        self.up_srv.close()

    async def test_streams_are_serialised_end_to_end(self):
        res = await asyncio.gather(*[_post(self.port, m) for m in ("A", "B", "A")])
        for data in res:
            self.assertIn(b"[DONE]", data)
        self.assertEqual(self.up.peak, 1, "upstream saw overlapping generations")
        self.assertIsNone(self.q.running)

    async def test_get_passes_while_busy(self):
        busy = asyncio.ensure_future(_post(self.port, "A"))
        await asyncio.sleep(0.05)
        self.assertEqual(self.q.running, "A")
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(b"GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n")
        await w.drain()
        data = await asyncio.wait_for(r.read(), 1)
        w.close()
        self.assertIn(b'{"data":[]}', data)
        await busy

    async def test_timeout_returns_429_with_retry_after(self):
        self.cfg.max_wait = 0.05
        busy = asyncio.ensure_future(_post(self.port, "A"))
        await asyncio.sleep(0.02)
        data = await _post(self.port, "B")
        self.assertTrue(data.startswith(b"HTTP/1.1 429"), data[:80])
        self.assertIn(b"Retry-After: 7", data)
        await busy

    async def test_client_disconnect_mid_stream_releases(self):
        r, w = await _post(self.port, "A", read_all=False)
        await asyncio.sleep(0.08)
        self.assertEqual(self.q.running, "A")
        w.close()
        for _ in range(50):
            if self.q.running is None:
                break
            await asyncio.sleep(0.02)
        self.assertIsNone(self.q.running)
        data = await asyncio.wait_for(_post(self.port, "B"), 2)
        self.assertIn(b"[DONE]", data)

    async def test_queue_headers_injected(self):
        data = await _post(self.port, "A")
        head = data.split(b"\r\n\r\n", 1)[0]
        self.assertIn(b"X-LLM-Queue-Wait-Ms: ", head)
        self.assertIn(b"X-LLM-Queue-Depth: ", head)
        self.assertIn(b"[DONE]", data)

    async def test_metrics_and_event_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.srv.app.event_log = f"{d}/events.jsonl"
            self.cfg.max_wait = 0.05
            busy = asyncio.ensure_future(_post(self.port, "A"))
            await asyncio.sleep(0.02)
            await _post(self.port, "B")                 # 429 timeout
            await busy
            events = [json.loads(l) for l in open(f"{d}/events.jsonl")]
        outcomes = sorted((e["model"], e["outcome"]) for e in events)
        self.assertEqual(outcomes, [("A", "ok"), ("B", "rejected_timeout")])
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
        await w.drain()
        text = (await r.read()).decode()
        w.close()
        self.assertIn('llm_queue_requests_total{model="A",outcome="ok"} 1', text)
        self.assertIn('llm_queue_requests_total{model="B",outcome="rejected_timeout"} 1',
                      text)
        self.assertIn('llm_queue_wait_seconds_count{model="A"} 1', text)
        self.assertIn("llm_queue_busy 0", text)

    async def test_status_endpoint(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(b"GET /queue/status HTTP/1.1\r\nHost: x\r\n\r\n")
        await w.drain()
        data = await r.read()
        w.close()
        body = json.loads(data.split(b"\r\n\r\n", 1)[1])
        self.assertIn("stats", body)


if __name__ == "__main__":
    unittest.main()
