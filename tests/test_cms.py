import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cms


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


if __name__ == "__main__":
    unittest.main()
