import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daemon
import statestore


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class ListLivePanesTest(unittest.TestCase):
    def test_parses_pane_rows(self):
        out = (
            "%0\t100\t1781156029\tcms:0.0\n"
            "%1\t200\t1781156043\tmy session:1.0\n"
        )
        with mock.patch.object(daemon.subprocess, "run", return_value=completed(stdout=out)):
            panes = daemon.list_live_panes()
        self.assertEqual(
            panes,
            [
                {"uid": "%0", "pid": "100", "activity": 1781156029, "addr": "cms:0.0"},
                {"uid": "%1", "pid": "200", "activity": 1781156043, "addr": "my session:1.0"},
            ],
        )

    def test_blank_activity_becomes_none(self):
        out = "%0\t100\t\tcms:0.0\n"
        with mock.patch.object(daemon.subprocess, "run", return_value=completed(stdout=out)):
            panes = daemon.list_live_panes()
        self.assertIsNone(panes[0]["activity"])

    def test_no_server_means_no_panes(self):
        cp = completed(returncode=1, stderr="no server running on /tmp/tmux-501/default")
        with mock.patch.object(daemon.subprocess, "run", return_value=cp):
            self.assertEqual(daemon.list_live_panes(), [])

    def test_stale_socket_error_means_no_panes(self):
        cp = completed(returncode=1,
                       stderr="error connecting to /tmp/tmux-501/default (No such file or directory)")
        with mock.patch.object(daemon.subprocess, "run", return_value=cp):
            self.assertEqual(daemon.list_live_panes(), [])

    def test_unexpected_error_returns_none(self):
        cp = completed(returncode=1, stderr="lost server")
        with mock.patch.object(daemon.subprocess, "run", return_value=cp):
            self.assertIsNone(daemon.list_live_panes())

    def test_missing_tmux_binary_skips_cycle(self):
        # tmux not installed → FileNotFoundError. Treat as "can't tell": return
        # None (so the daemon skips rather than prunes) and log it, instead of
        # letting the traceback escape.
        with mock.patch.object(daemon.subprocess, "run", side_effect=FileNotFoundError), \
             self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(daemon.list_live_panes())
        self.assertTrue(any("tmux" in m for m in logs.output))

    def test_non_integer_activity_becomes_none(self):
        # '²'.isdigit() is True but int('²') raises — must not crash the scan.
        out = "%0\t100\t²\tcms:0.0\n"
        with mock.patch.object(daemon.subprocess, "run", return_value=completed(stdout=out)):
            self.assertIsNone(daemon.list_live_panes()[0]["activity"])


class ScanTest(unittest.TestCase):
    IDLE = daemon.IDLE_THRESHOLD_SECS

    def pane(self, uid="%5", pid="100", activity=1000, addr="cms:1.0"):
        return {"uid": uid, "pid": pid, "activity": activity, "addr": addr}

    def test_idle_pane_gets_keepalive_targeted_at_pane_uid(self):
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100", "account": "primary"}}
        now = 1000 + self.IDLE
        to_ping, dead = daemon.scan(sessions, [self.pane()], now)
        self.assertEqual(dead, [])
        self.assertEqual(len(to_ping), 1)
        self.assertEqual(to_ping[0]["target"], "%5")
        self.assertEqual(to_ping[0]["idle"], self.IDLE)
        self.assertEqual(to_ping[0]["account"], "primary")

    def test_active_pane_is_left_alone(self):
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}}
        now = 1000 + self.IDLE - 1
        to_ping, dead = daemon.scan(sessions, [self.pane()], now)
        self.assertEqual((to_ping, dead), ([], []))

    def test_recycled_pane_uid_with_new_pid_is_dead_not_pinged(self):
        # The tracked pane's pid no longer matches: tmux recycled the id for
        # an unrelated pane. We must NOT type into it.
        info = {"pane_uid": "%5", "pane_pid": "100"}
        recycled = self.pane(pid="999")
        to_ping, dead = daemon.scan({"cms:1.0": info}, [recycled], 10_000_000)
        self.assertEqual(to_ping, [])
        self.assertEqual(dead, [("cms:1.0", info)])

    def test_missing_pane_is_dead(self):
        info = {"pane_uid": "%5", "pane_pid": "100"}
        to_ping, dead = daemon.scan({"cms:1.0": info}, [], 10_000_000)
        self.assertEqual(to_ping, [])
        self.assertEqual(dead, [("cms:1.0", info)])

    def test_uid_entry_never_falls_back_to_address_matching(self):
        # pane_uid recorded but pid missing: a different pane now sits at the
        # tracked address. It must not be matched (and not be typed into).
        info = {"pane_uid": "%5", "pane_pid": None}
        imposter = self.pane(uid="%9", pid="999", addr="cms:1.0")
        to_ping, dead = daemon.scan({"cms:1.0": info}, [imposter], 10_000_000)
        self.assertEqual(to_ping, [])
        self.assertEqual(dead, [("cms:1.0", info)])

    def test_uid_entry_with_missing_pid_still_matches_by_uid(self):
        info = {"pane_uid": "%5", "pane_pid": None}
        now = 1000 + self.IDLE
        to_ping, dead = daemon.scan({"cms:1.0": info}, [self.pane()], now)
        self.assertEqual(dead, [])
        self.assertEqual(to_ping[0]["target"], "%5")

    def test_legacy_entry_matches_by_address(self):
        # Entries written before pane ids were recorded carry no pane_uid.
        sessions = {"cms:1.0": {"account": "secondary"}}
        now = 1000 + self.IDLE
        to_ping, dead = daemon.scan(sessions, [self.pane()], now)
        self.assertEqual(dead, [])
        self.assertEqual(to_ping[0]["target"], "%5")  # still pings the precise pane

    def test_legacy_entry_with_no_matching_address_is_dead(self):
        info = {"account": "secondary"}
        to_ping, dead = daemon.scan({"cms:9.0": info}, [self.pane()], 10_000_000)
        self.assertEqual(to_ping, [])
        self.assertEqual(dead, [("cms:9.0", info)])

    def test_unknown_activity_is_skipped_but_kept(self):
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}}
        pane = self.pane(activity=None)
        to_ping, dead = daemon.scan(sessions, [pane], 10_000_000)
        self.assertEqual((to_ping, dead), ([], []))


class LoggingTest(unittest.TestCase):
    def test_rotation_caps_log_growth(self):
        # A failure loop must not be able to grow daemon.log without bound.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            log = base / "daemon.log"
            root = logging.getLogger()
            prev_level = root.level
            with mock.patch.object(daemon, "LOG_FILE", log):
                handler = daemon._configure_logging(max_bytes=1000, backup_count=2)
            try:
                for _ in range(500):
                    root.info("x" * 80)  # ~62 KB written in total
            finally:
                root.removeHandler(handler)
                handler.close()
                root.setLevel(prev_level)

            # The active log stays near its cap instead of holding everything...
            self.assertLessEqual(log.stat().st_size, 2000)
            # ...rotation actually happened...
            self.assertTrue((base / "daemon.log.1").exists())
            # ...and backup_count is honored (no unbounded .3/.4/...).
            self.assertFalse((base / "daemon.log.3").exists())


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.status_file = Path(self.tmp.name) / "daemon_status.json"
        p = mock.patch.object(daemon, "DAEMON_STATUS_FILE", self.status_file)
        p.start()
        self.addCleanup(p.stop)

    def test_write_then_read_round_trips(self):
        daemon.write_heartbeat(started_at=1000, code_mtime=42.5, now=1234)
        status = daemon.read_status()
        self.assertEqual(status["started_at"], 1000)
        self.assertEqual(status["code_mtime"], 42.5)
        self.assertEqual(status["last_run"], 1234)
        self.assertIn("pid", status)

    def test_read_status_missing_file_is_none(self):
        self.assertIsNone(daemon.read_status())

    def test_read_status_corrupt_file_is_none(self):
        self.status_file.write_text("{truncated")
        self.assertIsNone(daemon.read_status())

    def test_read_status_non_object_is_none(self):
        self.status_file.write_text('["not", "a", "dict"]')
        self.assertIsNone(daemon.read_status())

    def test_write_heartbeat_swallows_disk_error(self):
        # A heartbeat write failure must never crash the daemon loop.
        with mock.patch.object(daemon.statestore, "atomic_write_json",
                               side_effect=OSError("disk full")), \
             self.assertLogs(level="WARNING") as logs:
            daemon.write_heartbeat(started_at=1, code_mtime=None, now=2)
        self.assertTrue(any("heartbeat" in m.lower() for m in logs.output))


class SourceMtimeTest(unittest.TestCase):
    def test_returns_newest_mtime_of_real_sources(self):
        # daemon.py and statestore.py exist; mtime is a positive float.
        mt = daemon.source_mtime()
        self.assertIsInstance(mt, float)
        self.assertGreater(mt, 0)

    def test_returns_none_when_no_sources_readable(self):
        with mock.patch.object(daemon, "_SOURCE_FILES",
                               (Path("/no/such/daemon.py"),)):
            self.assertIsNone(daemon.source_mtime())


class MainWritesHeartbeatTest(unittest.TestCase):
    def test_loop_body_writes_heartbeat_before_running(self):
        # main() must stamp a heartbeat each cycle so a wedged run_once still
        # shows the daemon as alive. Drive exactly one iteration then bail.
        calls = {"heartbeat": 0, "run_once": 0}

        def fake_heartbeat(*a, **k):
            calls["heartbeat"] += 1

        def fake_run_once():
            calls["run_once"] += 1

        def stop(_secs):
            raise KeyboardInterrupt  # break out after the first cycle

        with mock.patch.object(daemon, "_configure_logging"), \
             mock.patch.object(daemon, "_ensure_tmux_env"), \
             mock.patch.object(daemon, "write_heartbeat", side_effect=fake_heartbeat), \
             mock.patch.object(daemon, "run_once", side_effect=fake_run_once), \
             mock.patch.object(daemon.time, "sleep", side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                daemon.main()
        self.assertEqual(calls["heartbeat"], 1)
        self.assertEqual(calls["run_once"], 1)


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name, value in (("STATE_FILE", base / "state.json"),
                            ("LOCK_FILE", base / ".state.lock")):
            p = mock.patch.object(statestore, name, value)
            p.start()
            self.addCleanup(p.stop)

    def seed(self, sessions):
        statestore.update_state(lambda s: s["sessions"].update(sessions))

    def test_dead_panes_are_pruned(self):
        self.seed({"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}})
        with mock.patch.object(daemon, "list_live_panes", return_value=[]):
            daemon.run_once(now=10_000_000)
        self.assertEqual(statestore.load_state()["sessions"], {})

    def test_tmux_error_does_not_prune(self):
        self.seed({"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}})
        with mock.patch.object(daemon, "list_live_panes", return_value=None), \
             self.assertLogs(level="WARNING") as logs:
            daemon.run_once(now=10_000_000)
        self.assertIn("cms:1.0", statestore.load_state()["sessions"])
        # The skipped cycle must be announced so a silent stall is visible.
        self.assertTrue(any("skipping this cycle" in m for m in logs.output))

    def test_prune_spares_session_replaced_under_a_recycled_key(self):
        # cms.py can record a brand-new session under the same address key
        # while the daemon is mid-scan; the prune must not evict it.
        old = {"pane_uid": "%5", "pane_pid": "100"}
        new = {"pane_uid": "%9", "pane_pid": "200"}
        self.seed({"cms:1.0": old})

        def listing():
            # Simulate cms.py winning the race during the daemon's tmux call.
            statestore.update_state(lambda s: s["sessions"].update({"cms:1.0": new}))
            return []

        with mock.patch.object(daemon, "list_live_panes", side_effect=listing):
            daemon.run_once(now=10_000_000)
        self.assertEqual(statestore.load_state()["sessions"]["cms:1.0"], new)

    def test_keepalive_sent_for_idle_pane(self):
        self.seed({"cms:1.0": {"pane_uid": "%5", "pane_pid": "100", "account": "primary"}})
        pane = {"uid": "%5", "pid": "100", "activity": 1000, "addr": "cms:1.0"}
        sent = []
        with mock.patch.object(daemon, "list_live_panes", return_value=[pane]), \
             mock.patch.object(daemon, "send_keepalive",
                               side_effect=lambda t: sent.append(t) or True):
            daemon.run_once(now=1000 + daemon.IDLE_THRESHOLD_SECS)
        self.assertEqual(sent, ["%5"])
        # Pane is alive — it must remain tracked.
        self.assertIn("cms:1.0", statestore.load_state()["sessions"])

    def test_send_keepalive_invokes_tmux_send_keys(self):
        with mock.patch.object(daemon.subprocess, "run",
                               return_value=completed()) as run:
            self.assertTrue(daemon.send_keepalive("%7"))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:], ["send-keys", "-t", "%7", ".", "Enter"])


if __name__ == "__main__":
    unittest.main()
