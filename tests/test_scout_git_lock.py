"""Tests for the scout's git lock in scout/hermes-cron.sh and scout/hermes-assess.sh.

Run: python3 -m unittest discover -s tests -v
The two Hermes jobs update one shallow clone, and git aborts rather than waits
when two shallow fetches overlap ("shallow file has changed since we read it",
2026-11-09). These tests run the real scripts against a local origin with a
stub bench/model_scout.py, so no network and no Hub.
"""

import fcntl
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRON = ROOT / "scout" / "hermes-cron.sh"
ASSESS = ROOT / "scout" / "hermes-assess.sh"
SCRIPTS = {"hermes-cron.sh": CRON, "hermes-assess.sh": ASSESS}

STUB = """\
import os, time
mark = os.environ.get("STUB_MARK")
if mark:
    open(mark, "w").close()
print("SCAN", flush=True)
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
"""


@unittest.skipUnless(shutil.which("flock") and shutil.which("bash"), "needs flock and bash")
class ScoutGitLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scout-lock-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = test\n\temail = test@example.invalid\n"
                             "[init]\n\tdefaultBranch = main\n")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("SCOUT_", "GIT_", "STUB_")) and k != "GITHUB_PAT_HERMES"}
        self.env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"

        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self.home = self.tmp / "home"
        self.repo = self.home / "workspace" / "local-llm"
        self.lock_path = self.home / "state" / "scout-git.lock"
        (self.home / "state").mkdir(parents=True)
        self.run_git("init", "-q", "--bare", str(self.origin))
        self.run_git("init", "-q", str(self.work))
        (self.work / "bench").mkdir()
        (self.work / "bench" / "model_scout.py").write_text(STUB)
        self.round = 0
        self.push_commit()
        self.env.update(HERMES_HOME=str(self.home), SCOUT_REPO=str(self.repo),
                        SCOUT_REPO_URL=self.origin.as_uri(), SCOUT_GIT_LOCK_WAIT="30")

    # fixture helpers

    def run_git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def push_commit(self):
        self.round += 1
        (self.work / "round.txt").write_text(f"{self.round}\n")
        self.run_git("add", "-A", cwd=self.work)
        self.run_git("commit", "-q", "-m", f"round {self.round}", cwd=self.work)
        self.run_git("push", "-q", str(self.origin), "HEAD:main", cwd=self.work)
        return self.origin_head()

    def origin_head(self):
        return self.run_git("--git-dir", str(self.origin), "rev-parse", "main")

    def clone_head(self):
        return self.run_git("-C", str(self.repo), "rev-parse", "HEAD")

    def make_clone(self):
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        self.run_git("clone", "-q", "--depth", "1", self.origin.as_uri(), str(self.repo))

    def hold_lock(self):
        # Non-blocking: a lock this process failed to release must fail the
        # test, not hang it.
        f = open(self.lock_path, "w")
        self.addCleanup(f.close)
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f

    def start(self, script, **env):
        proc = subprocess.Popen(["bash", str(script)], env={**self.env, **env},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.communicate()

    def finish(self, proc, timeout=60):
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err

    def run_script(self, script, **env):
        return self.finish(self.start(script, **env))

    # tests

    def test_clone_comes_from_scout_repo_url(self):
        code, out, err = self.run_script(CRON)
        self.assertEqual(code, 0, err)
        self.assertIn("SCAN", out)
        self.assertEqual(self.clone_head(), self.origin_head())

    def test_update_waits_for_the_lock(self):
        for name, script in SCRIPTS.items():
            with self.subTest(script=name):
                if not self.repo.exists():
                    self.make_clone()
                before = self.clone_head()
                want = self.push_commit()
                lock = self.hold_lock()
                proc = self.start(script)
                try:
                    time.sleep(1)
                    self.assertIsNone(proc.poll(), "finished while the lock was held")
                    self.assertEqual(self.clone_head(), before)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                code, out, err = self.finish(proc)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.clone_head(), want)
                self.assertNotIn("warning:", out)

    def test_lock_timeout_scans_the_old_clone(self):
        self.make_clone()
        before = self.clone_head()
        self.push_commit()
        self.hold_lock()

        code, out, err = self.run_script(CRON, SCOUT_GIT_LOCK_WAIT="1")
        self.assertEqual(code, 0, err)
        self.assertIn("SCAN", out)
        self.assertIn("warning: git pull failed", out)
        self.assertIn("timed out", out)
        self.assertEqual(self.clone_head(), before)

        code, out, err = self.run_script(ASSESS, SCOUT_GIT_LOCK_WAIT="1")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "SCAN\n")  # any stdout here would start a model call
        self.assertIn("timed out", err)
        self.assertEqual(self.clone_head(), before)

    def test_parallel_first_clone(self):
        lock = self.hold_lock()  # line both runs up behind the lock
        procs = [self.start(CRON) for _ in range(2)]
        time.sleep(0.5)
        fcntl.flock(lock, fcntl.LOCK_UN)
        for proc in procs:
            code, out, err = self.finish(proc)
            self.assertEqual(code, 0, err)
            self.assertIn("SCAN", out)
            self.assertNotIn("warning:", out)
        self.assertEqual(self.clone_head(), self.origin_head())
        self.assertEqual(self.run_git("-C", str(self.repo), "rev-list", "--count", "HEAD"), "1")

    def test_concurrent_updates_do_not_fail(self):
        # The reported symptom: both jobs fetched at 07:30 and one aborted.
        self.make_clone()
        for _ in range(10):
            want = self.push_commit()
            procs = [self.start(CRON), self.start(ASSESS)]
            for proc in procs:
                code, out, err = self.finish(proc)
                self.assertEqual(code, 0, err)
                self.assertNotIn("warning:", out)
                self.assertNotIn("fatal:", out + err)
            self.assertEqual(self.clone_head(), want)

    def test_lock_is_released_before_the_scan(self):
        self.make_clone()
        for name, script in SCRIPTS.items():
            with self.subTest(script=name):
                mark = self.tmp / f"{name}.started"
                proc = self.start(script, STUB_SLEEP="3", STUB_MARK=str(mark))
                deadline = time.monotonic() + 30
                while not mark.exists():
                    self.assertLess(time.monotonic(), deadline, "the scan never started")
                    time.sleep(0.05)
                got = False
                with open(self.lock_path, "w") as f:
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        try:
                            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            got = True
                            break
                        except BlockingIOError:
                            time.sleep(0.05)
                self.assertIsNone(proc.poll(), "the scan should still be running")
                self.assertTrue(got, "the scan holds the git lock")
                code, _, err = self.finish(proc)
                self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
