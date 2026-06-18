# Claudepad — Claude Management System

## Session Summaries

### 2026-06-18T00:39Z
Robustness pass — 2 real fixes + 4 tests (101 total green; code-review sub-agent
verdict SHIP, no defects). (1) **Scraper auth errors were unhelpful on the
common path**: `_get_org_uuid` mapped a 403 to the friendly "run `cms setup
--reauth`" hint, but once `org_uuid` is cached (the normal case) the *only*
request is to `/usage`, whose 403 went through a bare `raise_for_status()` — so
a stale-cookie failure surfaced as `could not fetch usage: 403 Client Error`
instead of the actionable hint. Extracted `_auth_checked_get(account, s, url)`
(maps 401/403 → reauth `RuntimeError`, else `raise_for_status`) and routed both
the org-discovery and usage GETs through it; now a stale cookie always explains
the fix regardless of cache state. Also newly handles 401, not just 403. (2)
**`cms` crashed with a raw traceback when tmux is absent**: `launch_session`
and `list_live_panes` called tmux with no `FileNotFoundError` guard (the daemon
resolves tmux robustly via `_tmux_bin`, but the CLI didn't) — a fresh box where
`brew install tmux` failed got a stack trace. Added `cms._tmux()` wrapper
(prints a one-line install hint + `sys.exit(1)` on `FileNotFoundError`) and
routed all four launch-path tmux calls through it; `daemon.list_live_panes` now
catches `FileNotFoundError` → logs + returns None, so `cms status` degrades to
liveness "unknown" (quota still renders) and the daemon skips the cycle instead
of pruning. Verified: both fixes are strict supersets of prior behavior (no
happy-path change, other HTTP errors still raise), new tests fail against old
code. Docs updated (ARCHITECTURE scraper + CLI sections). One commit on main,
unpushed.

### 2026-06-17T19:31Z
Polished the user-facing CLI in `cms.py` (4 real fixes + 31 new tests, 97 total
green; code-review sub-agent verdict SHIP). (1) **`None%` banner bug**: the
launch banner did `chosen_pct.get('utilization', '?')`, so an explicit `null`
utilization from the web API printed `None%` — the same null footgun a prior
session fixed in `_fmt_util` but missed here. Extracted a terse `_fmt_pct()`
(returns `?` for null/missing/non-numeric, guards the `isinstance(True,int)`
bool trap) and rebuilt `_fmt_util()` on top of it (verified behavior-identical
on all prior cases, plus now N/A on bool/string/non-dict instead of crashing).
(2) **secondary-only launch**: no-arg `cms` gated on `is_configured("primary")`
only, so a secondary-only setup printed "First time?" and exited even though
`best_account()` supports one account — added `any_configured()` and fixed the
gate. (3) **`cms status` ghost sessions**: it listed every tracked pane as
active with no liveness check (the committed `state.json` has a June-11 ghost).
Now reconciles against tmux by reusing the daemon's `match_pane` via a new pure
`_session_liveness()`, rendering live/idle/gone (+ human `_fmt_duration`). (4)
**`cms setup --reauth primary`** did nothing yet the help said "Re-authenticate
primary browser context" (primary has no browser context) — now clears the
cached org uuid (`_forget_org_uuid`) and the help/README/ARCH wording is fixed.
Wet test of the real `show_status` surfaced that the **running daemon still runs
pre-fix code**: the ghost pane is genuinely alive and idle **6d 13h** (no
keepalives firing), and `daemon.log` is still 5.5 MB — i.e. the PATH/rotation/
selection fixes from prior sessions haven't been picked up. **User should run
`cms daemon restart`** to load them. One commit on main, unpushed.

### 2026-06-17T13:07Z
Hardened account selection in `cms.py`. Found a latent crash: `best_account()`
did `pct = util.get("utilization", 100.0)` then `pct < best_util`, so an
explicit `null` utilization from the web API (valid JSON, not just a missing
key) raised `TypeError: '<' not supported between NoneType and float` — the
whole `cms` launch would abort. Refactored selection into two helpers:
`_utilization(bucket, default)` (tolerates null/missing/non-numeric, and guards
the `isinstance(True, int)` bool footgun) and `_account_score(data)` returning
the sort key `(weekly_capped, five_hour_utilization)`. This also closes a
correctness gap: selection now skips an account whose **7-day cap** is exhausted
(`seven_day.utilization >= 100`) *before* comparing 5-hour headroom — previously
a weekly-dead account with a fresh 5h bucket would be picked and fail instantly.
`_fmt_util` now renders null utilization as `N/A` instead of `None%`. Existing
behavior preserved (lower-5h wins, ties → primary, failed fetch = full); all
verified by 11 new tests (67 total, all green) and a code-review pass (no
defects). Also quieted leaked stdout/log noise in the suite. Docs updated
(README + ARCHITECTURE selection-algorithm section). One commit on main, unpushed.

### 2026-06-17T08:29Z
Landed the in-progress reliability rework and added log rotation. Root cause
found in `daemon.log`: the running daemon was crash-looping every 60s on
`FileNotFoundError: 'tmux'` (launchd's PATH lacks Homebrew) — 4173 identical
tracebacks, 4.7 MB. The WIP fixes it two ways: `daemon._tmux_bin()` resolves
the binary explicitly, and the generated plist now sets PATH. Also landed:
`statestore.py` (flock + atomic-write state shared by CLI/daemon), the daemon
rewrite (one `tmux list-panes` per cycle, `#{window_activity}` not the
non-existent `#{pane_activity}`, pane-id+PID matching with a recycled-pane
guard, race-safe prune), lazy scraper imports + 30s timeouts, install to
`~/.local/bin`, and a 56-test suite. New work this session: a
`RotatingFileHandler` (1 MB x 3) so the log can never balloon again, with a
test. Two commits on main; left unpushed (orchestrator pushes). Daemon picks
up the fixes on the next `cms daemon restart`.

### 2026-05-07T13:10Z
Built the Claude Management System (cms) from scratch. Full design → build → test → deploy cycle. Key discoveries: Bearer tokens 403 on claude.ai API (needs cookie auth); `anthropic-client-platform: web_claude_ai` header required; usage endpoint is `GET /api/organizations/{uuid}/usage`. Switched from playwright to browser_cookie3 for primary account scraping (extracts Chrome cookies directly, much simpler). Primary account fully working; secondary account setup path documented. Committed to GitHub: https://github.com/discordwell/claude-management-system

---

## Key Findings

### API
- Usage endpoint: `GET https://claude.ai/api/organizations/{uuid}/usage`
- Org discovery: `GET https://claude.ai/api/organizations` (returns array, first item has `uuid`)
- Required headers: `anthropic-client-platform: web_claude_ai`, browser User-Agent, `Referer: https://claude.ai/settings/usage`
- OAuth Bearer tokens return 403 — only session cookies work for the web API

### Auth
- Claude Code credentials: macOS Keychain service `Claude Code-credentials`, JSON key `claudeAiOauth` → `{accessToken, refreshToken, expiresAt, scopes, subscriptionType, rateLimitTier}`
- Env var `CLAUDE_CODE_OAUTH_TOKEN` overrides Keychain — pass JSON `{accessToken, refreshToken, expiresAt}`
- `CLAUDE_CONFIG_DIR` separates per-account data storage

### Scraping
- Primary: `browser_cookie3.chrome(domain_name='.claude.ai')` pulls Chrome default profile cookies — works if user is logged in to chrome.ai in Chrome
- Secondary: playwright persistent context at `~/.claude-accounts/secondary/browser-context/`

### Keepalive
- Cache TTL for Max subscribers: 1 hour
- Idle detection: one `tmux list-panes -a -F '...#{window_activity}...'` per cycle
  → Unix timestamp. **`#{pane_activity}` does NOT exist in tmux** (expands to an
  empty string, so keepalives never fire) — use `#{window_activity}`.
- Daemon checks every 60s, pings at 55min threshold
- `cms status` reuses the daemon's `list_live_panes`/`match_pane` to show each
  tracked session's live/idle/gone state (so status and the daemon agree)

### Infrastructure
- Daemon runs as launchd service: `com.discordwell.cms-daemon`
- Installed: `~/.local/bin/cms` → `~/Projects/claude-management-system/cms.py`
- Account data: `~/.claude-accounts/{primary,secondary}/`
- tmux required: installed via homebrew
