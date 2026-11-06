"""Every link in the generated site lands somewhere: each relative href's file
exists, and each #fragment is an id in the page it points at. Runs the real
generator on a copy of the repo inputs, so it checks what gets published."""

import html
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(site: Path) -> list[str]:
    pages = {p.name: p.read_text() for p in site.glob("*.html")}
    ids = {n: set(re.findall(r'\bid="([^"]+)"', h)) for n, h in pages.items()}
    bad = []
    for name, h in pages.items():
        hrefs = re.findall(r'href="([^"]+)"', h)
        for tip in re.findall(r'data-tooltip="([^"]*)"', h):
            hrefs += re.findall(r'href="([^"]+)"', html.unescape(tip))
        for u in map(html.unescape, hrefs):
            if re.match(r"[a-z]+:", u):
                continue
            path, _, frag = u.partition("#")
            target = (site / path).resolve() if path else site / name
            if not target.exists():
                bad.append(f"{name}: {u} (no file)")
            elif frag and target.name in ids and frag not in ids[target.name]:
                bad.append(f"{name}: {u} (no id)")
    return bad


class SiteLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        for d in ("findings", "docs", "bench"):
            shutil.copytree(ROOT / d, repo / d, ignore=shutil.ignore_patterns("__pycache__"))
        (repo / "results").mkdir()
        for p in ROOT.glob("results/*.json"):
            shutil.copy(p, repo / "results")
        (repo / "proxy").mkdir()
        shutil.copy(ROOT / "proxy" / "litellm-config.yaml", repo / "proxy")
        r = subprocess.run([sys.executable, str(ROOT / "dashboard" / "generate.py")],
                           cwd=repo, capture_output=True, text=True, timeout=60)
        assert r.returncode in (0, 2), r.stderr
        cls.site = repo / "site"
        # running.html is written by the live job, not the generator; render one
        # from a fake snapshot so its links are checked too
        sys.path.insert(0, str(ROOT / "dashboard"))
        import live
        snap = {"at": 1000.0, "errors": [], "omlx": None, "queue": None,
                "accuracy": {"running": True, "current_bench_id": "b", "queue": [],
                             "last_progress": {"benchmark": "livecodebench",
                                               "model_id": "m", "bench_current": 5,
                                               "bench_total": 500}}}
        (cls.site / "running.html").write_text(live.render(snap, live.update_state({}, snap)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_both_pages_written(self):
        self.assertTrue((self.site / "index.html").is_file())
        self.assertTrue((self.site / "findings.html").is_file())

    def test_no_dead_links_or_anchors(self):
        self.assertEqual(check(self.site), [])

    def test_findings_links_point_at_the_findings_page(self):
        index = (self.site / "index.html").read_text()
        self.assertNotIn("docs/FINDINGS.md#", index)
        self.assertIn('href="findings.html#', index)


if __name__ == "__main__":
    unittest.main()
