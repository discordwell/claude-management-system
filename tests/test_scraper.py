import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scraper


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        matches = [fragment for fragment in self.responses if fragment in url]
        if not matches:
            raise AssertionError(f"unexpected URL: {url}")
        return self.responses[max(matches, key=len)]


class ScraperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts_dir = Path(self.tmp.name)
        (self.accounts_dir / "primary").mkdir()
        p = mock.patch.object(scraper, "ACCOUNTS_DIR", self.accounts_dir)
        p.start()
        self.addCleanup(p.stop)

    def cfg_path(self):
        return self.accounts_dir / "primary" / "scraper_config.json"

    def test_org_uuid_fetched_and_cached(self):
        session = FakeSession({"/api/organizations": FakeResponse(payload=[{"uuid": "u-1"}])})
        self.assertEqual(scraper._get_org_uuid("primary", session), "u-1")
        self.assertEqual(json.loads(self.cfg_path().read_text())["org_uuid"], "u-1")

        # Second call must come from the config cache, with no HTTP at all.
        burning = FakeSession({})
        self.assertEqual(scraper._get_org_uuid("primary", burning), "u-1")
        self.assertEqual(burning.requests, [])

    def test_org_fetch_403_explains_reauth(self):
        session = FakeSession({"/api/organizations": FakeResponse(status_code=403)})
        with self.assertRaisesRegex(RuntimeError, "reauth"):
            scraper._get_org_uuid("primary", session)

    def test_org_fetch_401_explains_reauth(self):
        # A stale cookie can answer 401 as well as 403 — both are auth failures.
        session = FakeSession({"/api/organizations": FakeResponse(status_code=401)})
        with self.assertRaisesRegex(RuntimeError, "reauth"):
            scraper._get_org_uuid("primary", session)

    def test_usage_403_with_cached_org_surfaces_reauth_hint(self):
        # The common path: org uuid is cached, so /usage is the only request.
        # A 403 there must still produce the actionable reauth hint, not a bare
        # HTTP error from raise_for_status().
        self.cfg_path().write_text(json.dumps({"org_uuid": "u-1"}))
        session = FakeSession(
            {"/api/organizations/u-1/usage": FakeResponse(status_code=403)}
        )
        with mock.patch.object(scraper, "_build_session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "reauth"):
                scraper.get_usage("primary")

    def test_empty_org_list_is_an_error(self):
        session = FakeSession({"/api/organizations": FakeResponse(payload=[])})
        with self.assertRaisesRegex(RuntimeError, "No organizations"):
            scraper._get_org_uuid("primary", session)

    def test_corrupt_config_degrades_to_empty_and_refetches(self):
        self.cfg_path().write_text("{truncated")
        self.assertEqual(scraper._load_config("primary"), {})
        session = FakeSession({"/api/organizations": FakeResponse(payload=[{"uuid": "u-2"}])})
        self.assertEqual(scraper._get_org_uuid("primary", session), "u-2")
        self.assertEqual(json.loads(self.cfg_path().read_text())["org_uuid"], "u-2")

    def test_get_usage_hits_usage_endpoint_with_timeout(self):
        usage_payload = {"five_hour": {"utilization": 12.0}}
        session = FakeSession({
            "/api/organizations/u-1/usage": FakeResponse(payload=usage_payload),
            "/api/organizations": FakeResponse(payload=[{"uuid": "u-1"}]),
        })
        with mock.patch.object(scraper, "_build_session", return_value=session):
            self.assertEqual(scraper.get_usage("primary"), usage_payload)

        # Every request must carry a timeout so a wedged connection can't hang cms.
        self.assertTrue(session.requests)
        for url, kwargs in session.requests:
            self.assertEqual(kwargs.get("timeout"), scraper.REQUEST_TIMEOUT)

    def test_module_imports_without_optional_dependencies(self):
        # requests/browser_cookie3/playwright are imported lazily; the module
        # itself must always be importable (cms.py imports it for cached reads).
        self.assertTrue(hasattr(scraper, "get_usage"))


if __name__ == "__main__":
    unittest.main()
