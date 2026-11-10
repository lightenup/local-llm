"""Tests for the stack gauges in proxy/llm_queue.py (FINDINGS §43).

- render_stack: one local_stack_component_up per probed component, supervisor
  counters and timestamps, doctor success and timestamp; absent inputs emit
  no series rather than a fake 0
- doctor_summary: success only when every check passed; at_epoch preferred
- StackHealth.refresh: a crashing probe reads as down; files are re-read
- a scrape over HTTP returns the cached text without running a probe
"""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))
import llm_queue as lq  # noqa: E402


class RenderTests(unittest.TestCase):

    def test_components(self):
        text = lq.render_stack({"omlx": True, "litellm": False}, None, None)
        self.assertIn('local_stack_component_up{component="omlx"} 1', text)
        self.assertIn('local_stack_component_up{component="litellm"} 0', text)
        self.assertNotIn('component="tls_terminator"', text)
        self.assertNotIn("local_doctor", text)
        self.assertNotIn("local_stack_supervisor", text)

    def test_supervisor_and_doctor(self):
        sup = {"actions_total": {"up": 3, "open_docker": 1},
               "last_action": {"action": "up", "at": 1700000000.4},
               "heartbeat": 1700000060.0}
        text = lq.render_stack({}, sup, {"success": 0, "at": 1700000100})
        for line in ('local_stack_supervisor_actions_total{action="up"} 3',
                     'local_stack_supervisor_actions_total{action="open_docker"} 1',
                     "local_stack_supervisor_last_action_timestamp_seconds 1700000000",
                     "local_stack_supervisor_heartbeat_timestamp_seconds 1700000060",
                     "local_doctor_success 0",
                     "local_doctor_last_run_timestamp_seconds 1700000100"):
            self.assertIn(line, text)

    def test_doctor_summary(self):
        ok = {"at_epoch": 5, "results": [{"ok": True}, {"ok": True}]}
        bad = {"at_epoch": 5, "results": [{"ok": True}, {"ok": False}]}
        self.assertEqual(lq.doctor_summary(ok), {"success": 1, "at": 5})
        self.assertEqual(lq.doctor_summary(bad)["success"], 0)
        self.assertEqual(lq.doctor_summary({"at_epoch": 5, "results": []})["success"], 0)
        self.assertIsNone(lq.doctor_summary(None))
        self.assertIsNone(lq.doctor_summary({"at": "x"}))
        old = lq.doctor_summary({"at": "2026-11-10T06:00:00", "results": [{"ok": True}]})
        self.assertEqual(old["at"], time.mktime(time.strptime(
            "2026-11-10T06:00:00", "%Y-%m-%dT%H:%M:%S")))


class StackHealthTests(unittest.TestCase):

    def test_refresh(self):
        def boom():
            raise RuntimeError("x")
        with tempfile.TemporaryDirectory() as d:
            doc = Path(d) / "doctor.json"
            doc.write_text(json.dumps({"at_epoch": 9, "results": [{"ok": True}]}))
            sh = lq.StackHealth({"omlx": lambda: True, "litellm": boom},
                                doctor_json=str(doc),
                                supervisor_state=str(Path(d) / "missing.json"))
            text = sh.refresh()
            self.assertIn('component="omlx"} 1', text)
            self.assertIn('component="litellm"} 0', text)
            self.assertIn("local_doctor_success 1", text)
            doc.write_text(json.dumps({"at_epoch": 10, "results": [{"ok": False}]}))
            self.assertIn("local_doctor_success 0", sh.refresh())

    def test_tcp_probe(self):
        self.assertFalse(lq._tcp_probe("127.0.0.1", 1, timeout=0.2))


class ScrapeTests(unittest.IsolatedAsyncioTestCase):

    async def test_scrape_reads_cache_only(self):
        calls = []
        cfg = lq.Config("127.0.0.1", 1, 1.0, 1)
        server = await lq.serve("127.0.0.1", 0, cfg, lq.ModelQueue())
        port = server.sockets[0].getsockname()[1]
        server.app.stack = lq.StackHealth({"omlx": lambda: calls.append(1) or True})
        server.app.stack.refresh()
        async with server:
            for _ in range(3):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
                await w.drain()
                body = (await r.read()).decode()
                w.close()
                self.assertIn('local_stack_component_up{component="omlx"} 1', body)
                self.assertIn("llm_queue_depth 0", body)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
