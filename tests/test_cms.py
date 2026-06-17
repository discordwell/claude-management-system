import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cms


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
        with self.assertRaises(SystemExit):
            self.best(set(), {})


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
        with mock.patch.object(cms, "CACHE_TTL", 0):  # force cache expiry
            self.assertEqual(cms.fetch_usage("primary"), usage(10.0))

    def test_scraper_failure_with_no_cache_returns_none(self):
        self.scraper.get_usage = mock.Mock(side_effect=RuntimeError("down"))
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
