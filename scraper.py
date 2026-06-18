"""
Usage scraper for claude.ai/api/organizations/{uuid}/usage.

Authentication strategy:
  - primary:   browser_cookie3 reads the default Chrome profile (user is already logged in)
  - secondary: browser_cookie3 reads a specified Chrome profile path, OR playwright context

Account config stored in ~/.claude-accounts/{account}/scraper_config.json:
  {"chrome_cookie_file": "/path/to/Cookies"}   — point at a Chrome profile
  {"playwright": true}                           — use playwright persistent context (fallback)
  {}                                             — use default Chrome profile (primary default)
"""
from __future__ import annotations

import json
from pathlib import Path

import statestore

ACCOUNTS_DIR = Path.home() / ".claude-accounts"
BASE_URL = "https://claude.ai"
REQUEST_TIMEOUT = 30  # seconds — a hung connection should not block `cms` forever

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://claude.ai/settings/usage",
    "anthropic-client-platform": "web_claude_ai",
}


def _load_config(account: str) -> dict:
    cfg = ACCOUNTS_DIR / account / "scraper_config.json"
    try:
        config = json.loads(cfg.read_text())
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def _session_from_chrome(cookie_file: str | None = None):
    import browser_cookie3
    import requests

    kwargs = {"domain_name": ".claude.ai"}
    if cookie_file:
        kwargs["cookie_file"] = cookie_file

    cj = browser_cookie3.chrome(**kwargs)
    s = requests.Session()
    s.headers.update(_HEADERS)
    s.cookies = cj
    return s


def _session_from_playwright(account: str):
    """Build a requests session by fetching cookies from a playwright persistent context."""
    import requests
    from playwright.sync_api import sync_playwright

    context_dir = ACCOUNTS_DIR / account / "browser-context"
    if not context_dir.exists():
        raise RuntimeError(
            f"No playwright context for '{account}'. Run: cms setup"
        )

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(context_dir), headless=True)
        page = ctx.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded")
        cookies = ctx.cookies(BASE_URL)
        ctx.close()

    s = requests.Session()
    s.headers.update(_HEADERS)
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", ".claude.ai"))
    return s


def _build_session(account: str):
    cfg = _load_config(account)

    if cfg.get("chrome_cookie_file"):
        return _session_from_chrome(cookie_file=cfg["chrome_cookie_file"])
    elif cfg.get("playwright"):
        return _session_from_playwright(account)
    else:
        # Default: use Chrome's default profile
        return _session_from_chrome()


def _auth_checked_get(account: str, s, url: str):
    """GET ``url``, turning an auth failure into an actionable reauth hint.

    A stale browser cookie makes the claude.ai web API answer 401/403. Routing
    *both* the org-discovery and usage requests through here means that hint
    surfaces even when the org uuid is already cached — on that common path the
    usage call is the only request made, so a bare ``HTTP 403`` from
    raise_for_status() would otherwise be the only thing the user sees.
    """
    r = s.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"Auth failed for '{account}' ({r.status_code}). "
            f"Check Chrome login or run: cms setup --reauth {account}"
        )
    r.raise_for_status()
    return r


def _get_org_uuid(account: str, s) -> str:
    """Fetch and cache the org UUID (it never changes)."""
    cfg_file = ACCOUNTS_DIR / account / "scraper_config.json"
    cfg = _load_config(account)

    if "org_uuid" in cfg:
        return cfg["org_uuid"]

    r = _auth_checked_get(account, s, f"{BASE_URL}/api/organizations")
    orgs = r.json()
    if not orgs:
        raise RuntimeError(f"No organizations returned for '{account}'")

    uuid = orgs[0]["uuid"]
    cfg["org_uuid"] = uuid
    statestore.atomic_write_json(cfg_file, cfg)
    return uuid


def get_usage(account: str) -> dict:
    """
    Return usage dict for the account:
      {"five_hour": {"utilization": float, "resets_at": str}, "seven_day": {...}, ...}
    Raises RuntimeError on auth failure or network error.
    """
    s = _build_session(account)
    org_uuid = _get_org_uuid(account, s)
    r = _auth_checked_get(account, s, f"{BASE_URL}/api/organizations/{org_uuid}/usage")
    return r.json()


def setup_browser_context(account: str):
    """
    Open a visible playwright browser so the user can log in and save the context.
    Used as fallback when the account isn't in a Chrome profile.
    """
    from playwright.sync_api import sync_playwright

    context_dir = ACCOUNTS_DIR / account / "browser-context"
    context_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nOpening browser for '{account}' — log into claude.ai then close the window.\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(context_dir),
            headless=False,
            args=["--start-maximized"],
        )
        page = ctx.new_page()
        page.goto(f"{BASE_URL}/login")
        try:
            ctx.wait_for_event("close", timeout=600_000)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass

    # After login, record that this account uses playwright. Deliberately
    # drop any cached org_uuid: a reauth may have logged into a different
    # account, so the org must be re-discovered with the new cookies.
    cfg_file = ACCOUNTS_DIR / account / "scraper_config.json"
    statestore.atomic_write_json(cfg_file, {"playwright": True})
