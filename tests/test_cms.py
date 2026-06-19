import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cms
import statestore


def quiet():
    """Swallow a function-under-test's user-facing stdout during a test."""
    return contextlib.redirect_stdout(io.StringIO())


class FmtUtilTest(unittest.TestCase):
    def test_none_data(self):
        self.assertEqual(cms._fmt_util(None, "five_hour"), "N/A")

    def test_missing_bucket(self):
        self.assertEqual(cms._fmt_util({"other": {}}, "five_hour"), "N/A")

    def test_null_bucket(self):
        self.assertEqual(cms._fmt_util({"five_hour": None}, "five_hour"), "N/A")

    def test_full_bucket(self):
        data = {"five_hour": {"utilization": 32.0, "resets_at": "2026-05-07T16:30:00Z"}}
        self.assertEqual(
            cms._fmt_util(data, "five_hour"), "32.0%  resets 2026-05-07T16:30:00Z"
        )

    def test_no_reset_time(self):
        self.assertEqual(cms._fmt_util({"five_hour": {"utilization": 5}}, "five_hour"), "5%")

    def test_null_utilization(self):
        # The web API can send an explicit null utilization, not just omit it.
        self.assertEqual(cms._fmt_util({"five_hour": {"utilization": None}}, "five_hour"), "N/A")

    def test_zero_utilization_is_shown(self):
        # 0% is a real value and must not be confused with "no data".
        self.assertEqual(cms._fmt_util({"five_hour": {"utilization": 0}}, "five_hour"), "0%")


class AccountScoreTest(unittest.TestCase):
    def test_none_data_scores_as_exhausted(self):
        self.assertEqual(cms._account_score(None), (1, 100.0))

    def test_empty_data_scores_as_exhausted(self):
        # A response with no buckets is as useless as None — deprioritize it.
        self.assertEqual(cms._account_score({}), (1, 100.0))

    def test_null_utilization_assumed_full(self):
        self.assertEqual(cms._account_score({"five_hour": {"utilization": None}}), (0, 100.0))

    def test_weekly_cap_is_flagged(self):
        score = cms._account_score(
            {"five_hour": {"utilization": 5.0}, "seven_day": {"utilization": 100.0}}
        )
        self.assertEqual(score, (1, 5.0))

    def test_weekly_headroom_is_not_flagged(self):
        score = cms._account_score(
            {"five_hour": {"utilization": 5.0}, "seven_day": {"utilization": 99.9}}
        )
        self.assertEqual(score, (0, 5.0))

    def test_boolean_utilization_is_ignored(self):
        # isinstance(True, int) is True in Python — a stray bool must not be
        # treated as 1.0% utilization.
        self.assertEqual(cms._account_score({"five_hour": {"utilization": True}}), (0, 100.0))


class ParsePaneOutputTest(unittest.TestCase):
    def test_full_output(self):
        self.assertEqual(
            cms._parse_pane_output("cms:1.0|%5|412"), ("cms:1.0", "%5", "412")
        )

    def test_session_name_containing_pipes(self):
        self.assertEqual(
            cms._parse_pane_output("a|b:1.0|%5|412"), ("a|b:1.0", "%5", "412")
        )

    def test_old_tmux_with_unknown_format_variables(self):
        # Unknown format variables expand to empty strings.
        self.assertEqual(cms._parse_pane_output("cms:1.0||"), ("cms:1.0", None, None))

    def test_unexpected_output_degrades_to_address_only(self):
        self.assertEqual(cms._parse_pane_output("cms:1.0"), ("cms:1.0", None, None))


def usage(pct):
    return {"five_hour": {"utilization": pct, "resets_at": "soon"}}


class BestAccountTest(unittest.TestCase):
    def best(self, configured, usage_by_account):
        with mock.patch.object(cms, "is_configured", side_effect=lambda a: a in configured), \
             mock.patch.object(cms, "fetch_usage",
                               side_effect=lambda a: usage_by_account.get(a)):
            return cms.best_account()

    def test_picks_lower_utilization(self):
        best, _ = self.best({"primary", "secondary"},
                            {"primary": usage(80.0), "secondary": usage(20.0)})
        self.assertEqual(best, "secondary")

    def test_tie_goes_to_primary(self):
        best, _ = self.best({"primary", "secondary"},
                            {"primary": usage(50.0), "secondary": usage(50.0)})
        self.assertEqual(best, "primary")

    def test_failed_fetch_counts_as_full(self):
        best, _ = self.best({"primary", "secondary"},
                            {"primary": None, "secondary": usage(99.0)})
        self.assertEqual(best, "secondary")

    def test_both_failed_falls_back_to_primary(self):
        best, _ = self.best({"primary", "secondary"}, {"primary": None, "secondary": None})
        self.assertEqual(best, "primary")

    def test_only_secondary_configured(self):
        best, _ = self.best({"secondary"}, {"secondary": usage(99.0)})
        self.assertEqual(best, "secondary")

    def test_no_accounts_exits(self):
        with self.assertRaises(SystemExit), quiet():
            self.best(set(), {})

    def test_avoids_weekly_capped_account(self):
        # primary's 5-hour bucket is fresh but its weekly cap is exhausted;
        # secondary is busier on 5h but has weekly headroom → pick secondary,
        # since launching onto a weekly-dead account would fail immediately.
        best, _ = self.best(
            {"primary", "secondary"},
            {
                "primary": {"five_hour": {"utilization": 5.0},
                            "seven_day": {"utilization": 100.0}},
                "secondary": {"five_hour": {"utilization": 90.0},
                              "seven_day": {"utilization": 30.0}},
            },
        )
        self.assertEqual(best, "secondary")

    def test_both_weekly_capped_falls_back_to_lower_five_hour(self):
        best, _ = self.best(
            {"primary", "secondary"},
            {
                "primary": {"five_hour": {"utilization": 80.0},
                            "seven_day": {"utilization": 100.0}},
                "secondary": {"five_hour": {"utilization": 10.0},
                              "seven_day": {"utilization": 100.0}},
            },
        )
        self.assertEqual(best, "secondary")

    def test_null_utilization_does_not_crash(self):
        best, _ = self.best(
            {"primary", "secondary"},
            {"primary": {"five_hour": {"utilization": None}}, "secondary": usage(20.0)},
        )
        self.assertEqual(best, "secondary")


class FetchUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(cms, "CACHE_FILE", Path(self.tmp.name) / "cache.json")
        p.start()
        self.addCleanup(p.stop)

        self.scraper = types.ModuleType("scraper")
        self.calls = []

        def get_usage(account):
            self.calls.append(account)
            return usage(10.0)

        self.scraper.get_usage = get_usage
        p = mock.patch.dict(sys.modules, {"scraper": self.scraper})
        p.start()
        self.addCleanup(p.stop)

    def test_fresh_fetch_is_cached(self):
        data1 = cms.fetch_usage("primary")
        data2 = cms.fetch_usage("primary")
        self.assertEqual(data1, usage(10.0))
        self.assertEqual(data2, usage(10.0))
        self.assertEqual(self.calls, ["primary"])  # second hit served from cache

    def test_scraper_failure_returns_stale_data(self):
        cms.fetch_usage("primary")

        def boom(account):
            raise RuntimeError("network down")

        self.scraper.get_usage = boom
        with mock.patch.object(cms, "CACHE_TTL", 0), quiet():  # force cache expiry
            self.assertEqual(cms.fetch_usage("primary"), usage(10.0))

    def test_scraper_failure_with_no_cache_returns_none(self):
        self.scraper.get_usage = mock.Mock(side_effect=RuntimeError("down"))
        with quiet():
            self.assertIsNone(cms.fetch_usage("primary"))


class PlistTest(unittest.TestCase):
    def test_plist_gives_daemon_a_path_with_homebrew(self):
        plist = cms._plist_content()
        self.assertIn("<key>PATH</key>", plist)
        self.assertIn("/opt/homebrew/bin", plist)
        self.assertIn(cms.DAEMON_LABEL, plist)
        self.assertIn("daemon.py", plist)


class FmtPctTest(unittest.TestCase):
    def test_number(self):
        self.assertEqual(cms._fmt_pct({"five_hour": {"utilization": 32.0}}, "five_hour"), "32.0%")

    def test_zero_is_shown(self):
        self.assertEqual(cms._fmt_pct({"five_hour": {"utilization": 0}}, "five_hour"), "0%")

    def test_null_becomes_question_mark(self):
        # The web API can send an explicit null — must never render as 'None%'.
        self.assertEqual(cms._fmt_pct({"five_hour": {"utilization": None}}, "five_hour"), "?")

    def test_missing_bucket(self):
        self.assertEqual(cms._fmt_pct({}, "five_hour"), "?")

    def test_none_data(self):
        self.assertEqual(cms._fmt_pct(None, "five_hour"), "?")

    def test_bool_is_ignored(self):
        self.assertEqual(cms._fmt_pct({"five_hour": {"utilization": True}}, "five_hour"), "?")


class AnyConfiguredTest(unittest.TestCase):
    def test_true_when_only_secondary(self):
        with mock.patch.object(cms, "is_configured", side_effect=lambda a: a == "secondary"):
            self.assertTrue(cms.any_configured())

    def test_false_when_none(self):
        with mock.patch.object(cms, "is_configured", return_value=False):
            self.assertFalse(cms.any_configured())


class ForgetOrgUuidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "primary").mkdir()
        p = mock.patch.object(cms, "ACCOUNTS_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def cfg(self):
        return self.dir / "primary" / "scraper_config.json"

    def test_clears_org_uuid_but_keeps_other_keys(self):
        self.cfg().write_text(json.dumps({"org_uuid": "u-1", "playwright": True}))
        cms._forget_org_uuid("primary")
        self.assertEqual(json.loads(self.cfg().read_text()), {"playwright": True})

    def test_missing_file_is_a_noop(self):
        cms._forget_org_uuid("primary")
        self.assertFalse(self.cfg().exists())

    def test_no_org_uuid_does_not_rewrite(self):
        self.cfg().write_text(json.dumps({"playwright": True}))
        cms._forget_org_uuid("primary")
        self.assertEqual(json.loads(self.cfg().read_text()), {"playwright": True})


class SessionLivenessTest(unittest.TestCase):
    def test_live_pane_reports_idle_seconds(self):
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100", "account": "primary",
                                "started_at": "2026-06-17T00:00:00+00:00"}}
        panes = [{"uid": "%5", "pid": "100", "activity": 1000, "addr": "cms:1.0"}]
        row = cms._session_liveness(sessions, panes, now=1000 + 600)[0]
        self.assertEqual(row["status"], "live")
        self.assertEqual(row["idle_secs"], 600)
        self.assertEqual(row["account"], "primary")
        self.assertEqual(row["started"], "2026-06-17T00:00:00")

    def test_missing_pane_is_gone(self):
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}}
        row = cms._session_liveness(sessions, [], now=10_000)[0]
        self.assertEqual(row["status"], "gone")
        self.assertIsNone(row["idle_secs"])

    def test_none_panes_is_unknown(self):
        # A tmux query failure must not masquerade as 'gone'.
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}}
        row = cms._session_liveness(sessions, None, now=10_000)[0]
        self.assertEqual(row["status"], "unknown")

    def test_live_without_activity_has_no_idle(self):
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}}
        panes = [{"uid": "%5", "pid": "100", "activity": None, "addr": "cms:1.0"}]
        row = cms._session_liveness(sessions, panes, now=10_000)[0]
        self.assertEqual(row["status"], "live")
        self.assertIsNone(row["idle_secs"])

    def test_recycled_pane_is_gone_not_live(self):
        # Reuses the daemon's matcher: a recycled uid with a new pid is dead.
        sessions = {"cms:1.0": {"pane_uid": "%5", "pane_pid": "100"}}
        panes = [{"uid": "%5", "pid": "999", "activity": 1000, "addr": "cms:1.0"}]
        row = cms._session_liveness(sessions, panes, now=10_000)[0]
        self.assertEqual(row["status"], "gone")


class FmtDurationTest(unittest.TestCase):
    def test_under_a_minute(self):
        self.assertEqual(cms._fmt_duration(30), "<1m")

    def test_minutes(self):
        self.assertEqual(cms._fmt_duration(600), "10m")

    def test_hours_and_minutes(self):
        self.assertEqual(cms._fmt_duration(3 * 3600 + 4 * 60), "3h 4m")

    def test_days_and_hours(self):
        # The real ghost pane this surfaced: ~6.5 days idle.
        self.assertEqual(cms._fmt_duration(9459 * 60), "6d 13h")


class FmtSessionStatusTest(unittest.TestCase):
    def test_gone(self):
        self.assertIn("prune", cms._fmt_session_status({"status": "gone", "idle_secs": None}))

    def test_unknown(self):
        self.assertIn("unknown", cms._fmt_session_status({"status": "unknown", "idle_secs": None}))

    def test_live_idle_minutes(self):
        self.assertEqual(cms._fmt_session_status({"status": "live", "idle_secs": 600}), "live, idle 10m")

    def test_live_under_a_minute(self):
        self.assertEqual(cms._fmt_session_status({"status": "live", "idle_secs": 30}), "live, idle <1m")

    def test_live_without_activity(self):
        self.assertEqual(cms._fmt_session_status({"status": "live", "idle_secs": None}), "live")


class DaemonStatusLineTest(unittest.TestCase):
    """Pins the exact human wording of each daemon-health message."""

    STALE = 180

    def line(self, loaded, status, current_mtime=100.0, now=1000):
        return cms._classify_daemon(loaded, status, current_mtime, now, self.STALE)[1]

    def test_not_loaded(self):
        self.assertIn("not running", self.line(False, None))

    def test_loaded_without_heartbeat_suggests_restart(self):
        # Fires right after this feature lands: the still-running old daemon
        # writes no heartbeat, so the (accurate) nudge is to restart it.
        msg = self.line(True, None)
        self.assertIn("stale code", msg)
        self.assertIn("restart", msg)

    def test_fresh_heartbeat_reports_last_check_and_uptime(self):
        status = {"last_run": 940, "code_mtime": 100.0, "started_at": 400}
        msg = self.line(True, status, current_mtime=100.0, now=1000)
        self.assertIn("running, last check", msg)
        self.assertIn("up", msg)  # started_at present → uptime shown

    def test_stale_heartbeat_is_stalled(self):
        # last check 10 min ago, threshold 3 min → wedged loop.
        status = {"last_run": 1000 - 600, "code_mtime": 100.0}
        self.assertIn("STALLED", self.line(True, status, now=1000))

    def test_missing_last_run_is_flagged(self):
        self.assertIn("unreadable", self.line(True, {"code_mtime": 100.0}))

    def test_newer_source_means_stale_code(self):
        status = {"last_run": 990, "code_mtime": 100.0, "started_at": 500}
        msg = self.line(True, status, current_mtime=500.0, now=1000)
        self.assertIn("STALE code", msg)

    def test_equal_mtime_is_not_stale(self):
        status = {"last_run": 990, "code_mtime": 100.0, "started_at": 500}
        msg = self.line(True, status, current_mtime=100.0, now=1000)
        self.assertNotIn("STALE", msg)
        self.assertIn("running, last check", msg)

    def test_unknown_current_mtime_skips_stale_check(self):
        # If we can't stat the live source, don't cry "stale" — just report up.
        status = {"last_run": 990, "code_mtime": 100.0}
        msg = self.line(True, status, current_mtime=None, now=1000)
        self.assertNotIn("STALE", msg)
        self.assertIn("running, last check", msg)

    def test_stalled_takes_precedence_over_stale_code(self):
        # A daemon that isn't even heartbeating: "STALLED" is the useful signal.
        status = {"last_run": 0, "code_mtime": 100.0}
        msg = self.line(True, status, current_mtime=500.0, now=1000)
        self.assertIn("STALLED", msg)

    def test_boolean_last_run_reads_as_unreadable_not_one(self):
        # isinstance(True, int) is True — a corrupt heartbeat with a JSON bool
        # in last_run must not be treated as the epoch second 1.
        msg = self.line(True, {"last_run": True, "code_mtime": 100.0}, now=1000)
        self.assertIn("unreadable", msg)
        self.assertNotIn("STALLED", msg)

    def test_boolean_mtimes_do_not_trigger_false_stale_code(self):
        status = {"last_run": 990, "code_mtime": True, "started_at": 500}
        msg = self.line(True, status, current_mtime=True, now=1000)
        self.assertNotIn("STALE", msg)
        self.assertIn("running, last check", msg)


class ClassifyDaemonTest(unittest.TestCase):
    STALE = 180

    def state(self, loaded, status, current_mtime=100.0, now=1000):
        return cms._classify_daemon(loaded, status, current_mtime, now, self.STALE)[0]

    def test_not_running(self):
        self.assertEqual(self.state(False, None), "not_running")

    def test_no_heartbeat(self):
        self.assertEqual(self.state(True, None), "no_heartbeat")

    def test_unreadable_last_run(self):
        self.assertEqual(self.state(True, {"code_mtime": 100.0}), "unreadable")

    def test_stalled(self):
        self.assertEqual(self.state(True, {"last_run": 1000 - 600, "code_mtime": 100.0}), "stalled")

    def test_stale_code(self):
        status = {"last_run": 990, "code_mtime": 100.0, "started_at": 500}
        self.assertEqual(self.state(True, status, current_mtime=500.0), "stale_code")

    def test_healthy(self):
        status = {"last_run": 990, "code_mtime": 100.0, "started_at": 500}
        self.assertEqual(self.state(True, status), "healthy")


class WarnKeepaliveDaemonTest(unittest.TestCase):
    def warn(self, loaded, status, source_mtime=100.0, now=1000):
        buf = io.StringIO()
        with mock.patch.object(cms, "_daemon_loaded", return_value=loaded), \
             mock.patch("daemon.read_status", return_value=status), \
             mock.patch("daemon.source_mtime", return_value=source_mtime), \
             mock.patch.object(cms.time, "time", return_value=now), \
             contextlib.redirect_stdout(buf):
            cms._warn_if_keepalive_daemon_down()
        return buf.getvalue()

    def test_healthy_daemon_is_silent(self):
        # A working daemon must not clutter the launch banner.
        status = {"last_run": 990, "code_mtime": 100.0, "started_at": 500}
        self.assertEqual(self.warn(True, status), "")

    def test_down_daemon_warns(self):
        out = self.warn(False, None)
        self.assertIn("keepalive daemon", out)
        self.assertIn("not running", out)

    def test_stale_code_daemon_warns(self):
        # The #1 recurring incident: daemon up but running pre-edit code.
        status = {"last_run": 990, "code_mtime": 100.0, "started_at": 500}
        self.assertIn("STALE code", self.warn(True, status, source_mtime=500.0))

    def test_never_raises_on_internal_error(self):
        # A health hint must never break the actual launch — swallow and stay silent.
        buf = io.StringIO()
        with mock.patch.object(cms, "_daemon_loaded", side_effect=RuntimeError("boom")), \
             contextlib.redirect_stdout(buf):
            cms._warn_if_keepalive_daemon_down()  # must not raise
        self.assertEqual(buf.getvalue(), "")


class DaemonLoadedTest(unittest.TestCase):
    def proc(self, returncode):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")

    def test_true_when_launchctl_succeeds(self):
        with mock.patch.object(cms.subprocess, "run", return_value=self.proc(0)):
            self.assertTrue(cms._daemon_loaded())

    def test_false_when_launchctl_fails(self):
        with mock.patch.object(cms.subprocess, "run", return_value=self.proc(1)):
            self.assertFalse(cms._daemon_loaded())

    def test_false_when_launchctl_missing(self):
        with mock.patch.object(cms.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(cms._daemon_loaded())


class DaemonStatusExitTest(unittest.TestCase):
    """`cms daemon status` exits non-zero when unhealthy → `... || cms daemon restart`."""

    def proc(self, returncode):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")

    def test_unhealthy_exits_nonzero(self):
        with mock.patch.object(cms.subprocess, "run", return_value=self.proc(1)), \
             mock.patch("daemon.read_status", return_value=None), \
             mock.patch("daemon.source_mtime", return_value=100.0), \
             quiet(), self.assertRaises(SystemExit) as cm:
            cms.manage_daemon("status")
        self.assertEqual(cm.exception.code, 1)

    def test_healthy_does_not_exit(self):
        status = {"last_run": 1000, "code_mtime": 100.0, "started_at": 400}
        with mock.patch.object(cms.subprocess, "run", return_value=self.proc(0)), \
             mock.patch("daemon.read_status", return_value=status), \
             mock.patch("daemon.source_mtime", return_value=100.0), \
             mock.patch.object(cms.time, "time", return_value=1000), \
             quiet():
            cms.manage_daemon("status")  # must not raise SystemExit


class ShowStatusTest(unittest.TestCase):
    def test_dead_session_is_reported_as_gone(self):
        # A stale tracked pane (no live tmux pane) shows as gone, not as active.
        sessions = {"cms:0.0": {"account": "primary", "pane_uid": "%0", "pane_pid": "1",
                                "started_at": "2026-06-11T00:00:00+00:00"}}
        buf = io.StringIO()
        with mock.patch.object(cms, "is_configured", return_value=False), \
             mock.patch.object(statestore, "load_state",
                               return_value={"sessions": sessions, "next_session_id": 2}), \
             mock.patch.object(cms, "_daemon_loaded", return_value=False), \
             mock.patch("daemon.read_status", return_value=None), \
             mock.patch("daemon.list_live_panes", return_value=[]), \
             contextlib.redirect_stdout(buf):
            cms.show_status()
        out = buf.getvalue()
        self.assertIn("cms:0.0", out)
        self.assertIn("gone", out)
        self.assertIn("0 live", out)

    def test_reports_daemon_health(self):
        # `cms status` must surface daemon health — the gap that hid a multi-day
        # crash-loop and a stale-code daemon in two prior incidents.
        status = {"last_run": 1000, "code_mtime": 50.0, "started_at": 400, "pid": 7}
        buf = io.StringIO()
        with mock.patch.object(cms, "is_configured", return_value=False), \
             mock.patch.object(statestore, "load_state",
                               return_value={"sessions": {}, "next_session_id": 1}), \
             mock.patch.object(cms, "_daemon_loaded", return_value=True), \
             mock.patch("daemon.read_status", return_value=status), \
             mock.patch("daemon.source_mtime", return_value=50.0), \
             mock.patch.object(cms.time, "time", return_value=1000), \
             contextlib.redirect_stdout(buf):
            cms.show_status()
        self.assertIn("Daemon: running", buf.getvalue())


class LaunchSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name, value in (("STATE_FILE", base / "state.json"),
                            ("LOCK_FILE", base / ".state.lock")):
            p = mock.patch.object(statestore, name, value)
            p.start()
            self.addCleanup(p.stop)

    def proc(self, returncode=0, stdout="cms:3.0|%7|999\n", stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode,
                                           stdout=stdout, stderr=stderr)

    def test_forced_account_records_pane_identity(self):
        with mock.patch.object(cms, "is_configured", return_value=True), \
             mock.patch.object(cms.subprocess, "run", return_value=self.proc()), \
             mock.patch.dict(os.environ, {}, clear=True), quiet():
            cms.launch_session(account="primary")
        entry = statestore.load_state()["sessions"]["cms:3.0"]
        self.assertEqual(entry["pane_uid"], "%7")
        self.assertEqual(entry["pane_pid"], "999")
        self.assertEqual(entry["account"], "primary")

    def test_unconfigured_forced_account_exits(self):
        with mock.patch.object(cms, "is_configured", return_value=False), \
             quiet(), self.assertRaises(SystemExit):
            cms.launch_session(account="secondary")

    def test_missing_tmux_exits_with_hint_not_traceback(self):
        # A box without tmux must get a one-line install hint, not a raw
        # FileNotFoundError traceback out of subprocess.run.
        buf = io.StringIO()
        with mock.patch.object(cms, "is_configured", return_value=True), \
             mock.patch.object(cms.subprocess, "run", side_effect=FileNotFoundError), \
             mock.patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cms.launch_session(account="primary")
        self.assertIn("tmux not found", buf.getvalue())

    def test_falls_back_to_new_session_when_no_window(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1] == "new-window":
                return self.proc(returncode=1, stdout="", stderr="no server")
            return self.proc(stdout="cms:0.0|%0|111\n")

        with mock.patch.object(cms, "is_configured", return_value=True), \
             mock.patch.object(cms.subprocess, "run", side_effect=run), \
             mock.patch.dict(os.environ, {}, clear=True), quiet():
            cms.launch_session(account="primary")
        self.assertTrue(any(c[1] == "new-session" for c in calls))
        self.assertIn("cms:0.0", statestore.load_state()["sessions"])

    def test_launch_warns_when_keepalive_daemon_down(self):
        # The new pane is recorded for keepalive, but a down daemon means the
        # cache will still go cold — say so at launch, when it's most actionable.
        buf = io.StringIO()
        with mock.patch.object(cms, "is_configured", return_value=True), \
             mock.patch.object(cms.subprocess, "run", return_value=self.proc()), \
             mock.patch.object(cms, "_daemon_loaded", return_value=False), \
             mock.patch("daemon.read_status", return_value=None), \
             mock.patch("daemon.source_mtime", return_value=100.0), \
             mock.patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stdout(buf):
            cms.launch_session(account="primary")
        out = buf.getvalue()
        self.assertIn("Launched pane", out)
        self.assertIn("keepalive daemon", out)
        self.assertIn("not running", out)

    def test_launch_is_silent_when_keepalive_daemon_healthy(self):
        status = {"last_run": 1000, "code_mtime": 100.0, "started_at": 400}
        buf = io.StringIO()
        with mock.patch.object(cms, "is_configured", return_value=True), \
             mock.patch.object(cms.subprocess, "run", return_value=self.proc()), \
             mock.patch.object(cms, "_daemon_loaded", return_value=True), \
             mock.patch("daemon.read_status", return_value=status), \
             mock.patch("daemon.source_mtime", return_value=100.0), \
             mock.patch.object(cms.time, "time", return_value=1000), \
             mock.patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stdout(buf):
            cms.launch_session(account="primary")
        out = buf.getvalue()
        self.assertIn("Launched pane", out)
        self.assertNotIn("keepalive daemon", out)

    def test_auto_select_banner_is_null_safe(self):
        # Regression: an explicit null utilization must not print 'None%'.
        usage = {"primary": {"five_hour": {"utilization": None}},
                 "secondary": {"five_hour": {"utilization": 20.0}}}
        buf = io.StringIO()
        with mock.patch.object(cms, "best_account", return_value=("secondary", usage)), \
             mock.patch.object(cms.subprocess, "run",
                               return_value=self.proc(stdout="cms:1.0|%1|222\n")), \
             mock.patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stdout(buf):
            cms.launch_session()
        out = buf.getvalue()
        self.assertNotIn("None%", out)
        self.assertIn("Selected: secondary", out)
        self.assertIn("primary: ? session used", out)


if __name__ == "__main__":
    unittest.main()
