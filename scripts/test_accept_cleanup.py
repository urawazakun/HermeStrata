# Offline cleanup regression for accept_cache.py cleanup.
# No taskkill/HTTP/GPU: subprocess, time.sleep, and the runner's server hooks are mocked.
# Run: `python scripts/test_accept_cleanup.py` (unittest, stdlib only).
import json
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accept_cache as ac


def _cp(rc=0, out=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr="")


class StopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pidfile = Path(self.tmp.name) / "server.pid"
        self.p_pidfile = mock.patch.object(ac, "PIDFILE", self.pidfile)
        self.p_sleep = mock.patch.object(ac.time, "sleep", lambda *a: None)
        self.p_owned = mock.patch.object(ac, "_OWNED_SERVER", None)
        self.p_pidfile.start()
        self.p_sleep.start()
        self.p_owned.start()

    def tearDown(self):
        self.p_sleep.stop()
        self.p_owned.stop()
        self.p_pidfile.stop()
        self.tmp.cleanup()

    def _run(self, **kw):
        return mock.patch.object(ac.subprocess, "run", **kw)

    def test_success_unlinks_without_tasklist(self):
        self.pidfile.write_text("27068")
        with self._run(return_value=_cp(0)) as run:
            self.assertTrue(ac.stop())
        self.assertFalse(self.pidfile.exists())
        self.assertEqual(run.call_count, 1)  # taskkill only; no tasklist probe

    def test_no_marker_is_success_without_subprocess(self):
        with self._run() as run:
            self.assertTrue(ac.stop())
        run.assert_not_called()

    def test_failure_with_live_pid_keeps_marker_and_raises(self):
        self.pidfile.write_text("27068")
        calls = [mock.call(["taskkill", "/PID", "27068", "/T", "/F"], capture_output=True),
                 mock.call(["tasklist", "/FI", "PID eq 27068", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True)]
        with self._run(side_effect=[_cp(1), _cp(0, '"python.exe","27068","Console","1","10,000 K"\r\n')]) as run:
            with self.assertRaises(RuntimeError):
                ac.stop()
            self.assertEqual(run.call_args_list, calls)
        self.assertTrue(self.pidfile.exists())  # marker preserved for exact retry

    def test_failure_with_exited_pid_unlinks_stale_marker(self):
        self.pidfile.write_text("21172")
        with self._run(side_effect=[_cp(1), _cp(0, "INFO: No tasks are running\r\n")]):
            self.assertTrue(ac.stop())
        self.assertFalse(self.pidfile.exists())

    def test_tasklist_failure_cannot_prove_exit(self):
        self.pidfile.write_text("27068")
        with self._run(side_effect=[_cp(1), _cp(1)]):
            with self.assertRaises(RuntimeError):
                ac.stop()
        self.assertTrue(self.pidfile.exists())

    def test_owned_handle_unloads_before_termination_without_taskkill(self):
        pr = mock.Mock(pid=27068)
        pr.poll.return_value = None
        events = []
        pr.terminate.side_effect = lambda: events.append("terminate")
        pr.wait.side_effect = lambda **kw: events.append("wait")
        ac._OWNED_SERVER = pr
        self.pidfile.write_text("27068")
        def unload(req, **kw):
            self.assertEqual(req.full_url, ac.URL + "/unload")
            self.assertEqual(req.get_method(), "POST")
            events.append("unload")
            return io.BytesIO(b'{"status":"unloaded"}')
        with mock.patch.object(ac.urllib.request, "urlopen", side_effect=unload), self._run() as run:
            self.assertTrue(ac.stop())
        run.assert_not_called()
        self.assertEqual(events, ["unload", "terminate", "wait"])
        self.assertFalse(self.pidfile.exists())
        self.assertIsNone(ac._OWNED_SERVER)

    def test_owned_busy_server_preserves_marker_and_handle(self):
        pr = mock.Mock(pid=27068)
        pr.poll.return_value = None
        ac._OWNED_SERVER = pr
        self.pidfile.write_text("27068")
        with mock.patch.object(ac.urllib.request, "urlopen", return_value=io.BytesIO(b'{"status":"busy"}')):
            with self.assertRaises(RuntimeError):
                ac.stop()
        pr.terminate.assert_not_called()
        self.assertTrue(self.pidfile.exists())
        self.assertIs(ac._OWNED_SERVER, pr)

    def test_owned_native_handle_terminates_scratch_child(self):
        # Real native child handle, with HTTP mocked: no engine, GPU or taskkill.
        pr = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        ac._OWNED_SERVER = pr
        self.pidfile.write_text(str(pr.pid))
        try:
            with mock.patch.object(ac.urllib.request, "urlopen", return_value=io.BytesIO(b'{"status":"not loaded"}')), self._run() as run:
                self.assertTrue(ac.stop())
            run.assert_not_called()
            self.assertIsNotNone(pr.poll())
            self.assertFalse(self.pidfile.exists())
        finally:
            if pr.poll() is None:
                pr.terminate()
            pr.wait(timeout=10)

    def test_taskkill_exception_keeps_marker_and_raises(self):
        self.pidfile.write_text("27068")
        with self._run(side_effect=OSError("no such binary")):
            with self.assertRaises(RuntimeError):
                ac.stop()
        self.assertTrue(self.pidfile.exists())

    def test_non_numeric_marker_refused_without_subprocess(self):
        self.pidfile.write_text("abc")
        with self._run() as run:
            with self.assertRaises(RuntimeError):
                ac.stop()
            run.assert_not_called()
        self.assertTrue(self.pidfile.exists())


class RunnerCleanupTest(unittest.TestCase):
    def _rep(self, ok=True):
        return [{"tag": f"twolines.r0.A{i}", "pass": ok} for i in range(12)]

    def test_cleanup_failure_persists_record_and_exits_nonzero(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        out = Path(scratch.name)
        a = SimpleNamespace(variant="cached", reps=1)
        with mock.patch.object(ac, "OUT", out), \
             mock.patch.object(ac, "start", return_value="log"), \
             mock.patch.object(ac, "TwolinesEngine", lambda log: object()), \
             mock.patch.object(ac, "case_twolines", lambda eng, rep: self._rep(True)), \
             mock.patch.object(ac, "stop", side_effect=RuntimeError("taskkill rc=1, still alive")), \
             mock.patch.object(ac.time, "sleep", lambda *x: None):
            with self.assertRaises(SystemExit) as cm:
                ac.run_twolines(a, [])
            self.assertEqual(cm.exception.code, 1)
        recs = [json.loads(l) for l in (out / "twolines-cached.jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 13)  # 12 measured + 1 cleanup failure
        cleanup = [r for r in recs if r["tag"] == "twolines.cleanup"]
        self.assertEqual(len(cleanup), 1)
        self.assertFalse(cleanup[0]["pass"])
        self.assertIn("taskkill", cleanup[0]["reason"])

    def test_clean_run_has_no_cleanup_record_and_exits_zero(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        out = Path(scratch.name)
        a = SimpleNamespace(variant="cached", reps=1)
        with mock.patch.object(ac, "OUT", out), \
             mock.patch.object(ac, "start", return_value="log"), \
             mock.patch.object(ac, "TwolinesEngine", lambda log: object()), \
             mock.patch.object(ac, "case_twolines", lambda eng, rep: self._rep(True)), \
             mock.patch.object(ac, "stop", return_value=True), \
             mock.patch.object(ac.time, "sleep", lambda *x: None):
            self.assertIsNone(ac.run_twolines(a, []))
        recs = [json.loads(l) for l in (out / "twolines-cached.jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 12)
        self.assertTrue(all(r.get("pass") for r in recs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
