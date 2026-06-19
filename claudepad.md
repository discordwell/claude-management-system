# Claudepad — Claude Management System

## Session Summaries

### 2026-06-19T03:58Z
Closed the loop on this project's #1 recurring incident — the keepalive daemon
silently not working (down or running pre-edit code) so idle sessions go cold
(the 6d-idle ghost pane). Prior sessions made daemon health *visible in `cms
status`*; this session surfaces it at the two moments it's most actionable.
(1) **Launch-time warning**: `cms` / `cms primary` / `cms secondary` now print a
one-line `⚠ keepalive daemon …` right after recording the new pane (before the
blocking attach) whenever the daemon isn't *healthy* — the session is tracked
for keepalive, but a sick daemon means its cache still goes cold. Healthy daemon
stays silent; the check is best-effort (`except Exception: pass`) so a health
hint can never break the launch. (2) **`cms daemon status` now exits non-zero**
on any unhealthy state, so it's a scriptable health check —
`cms daemon status || cms daemon restart` heals a stale-code daemon in cron
without a human reading the log first. (3) Refactor enabling both: extracted the
pure `_classify_daemon(...) -> (state, message)` (states: not_running /
no_heartbeat / unreadable / stalled / stale_code / healthy) as the single source
of truth, with `_live_daemon_state()` the one place that assembles live inputs
(shared by `cms status` + the launch warning); `manage_daemon("status")` calls
the classifier directly since it also needs the verbose `launchctl` dump. The
old `_daemon_status_line` wrapper, now unused in production, was dropped and its
message-wording tests repointed at `_classify_daemon(...)[1]`. Byte-identical
messages → all prior daemon status-line assertions pass unchanged. 14 new tests
(138 total green): the 6 classifier states, the launch warning (warns unhealthy /
silent healthy / never raises), the launch-path integration (warn-when-down,
silent-when-healthy), and the scriptable exit code. Code-review pass (3 parallel
finder agents: correctness / regression / cleanup+conventions) found no
bugs/regressions; acted on the one cleanup nit (extracted `_live_daemon_state()`
to dedupe the live-arg assembly, then removed the redundant wrapper it exposed).
**Wet-tested read-only against the live box**:
`cms daemon status` exits 1 and the launch path prints the warning — both
because the real daemon (PID 29274) predates heartbeats and is running stale
code. **User still needs `cms daemon restart`** to load all prior fixes and
start emitting heartbeats. Docs updated (README, ARCHITECTURE CLI/classifier/
tests). One commit on main, unpushed.

### 2026-06-18T12:01Z
Added **daemon health visibility** — the gap behind this project's two worst
recurring incidents. Both were silent: a daemon crash-looping for days on a
missing-tmux `FileNotFoundError`, and a daemon left running pre-fix code while a
ghost pane sat idle 6d 13h with no keepalives. `cms status` showed quota and
sessions but *nothing* about the daemon itself, so neither was noticed until a
manual log dive. Fix: the daemon now writes a heartbeat to `daemon_status.json`
at the top of every cycle — `{pid, started_at, code_mtime, last_run}` — where
`code_mtime` is snapshotted **once at startup** from `daemon.py`+`statestore.py`
(`daemon.source_mtime()`). `cms status` (and `cms daemon status`) render a new
"Daemon:" line via the pure `cms._daemon_status_line()`, which cross-checks
launchd (`_daemon_loaded()`, real `launchctl list`) against the heartbeat and
classifies: **not running** / **STALLED** (no heartbeat in 3 cycles) / **STALE
code** (live source mtime > recorded) / **running, last check Ns ago, up Hh Mm**.
Single-writer file (daemon only) → atomic write, no lock; write failures logged
and swallowed so a disk error can't kill the loop. Heartbeat is written in
`main()`'s loop (not `run_once`) so a wedged `run_once` still reports alive.
23 new tests (124 total green): heartbeat round-trip/corruption, `source_mtime`,
the 6-state classifier, `_daemon_loaded`, and a `main()`-writes-heartbeat check.
Code-review pass (3 parallel finder agents: correctness / regression / convention
+cleanup) found no bugs/regressions; acted on one consistency nit — extracted
`cms._is_number()` so the classifier honors the repo's documented
`isinstance(True, int)` bool-footgun guard (a hand-corrupted heartbeat with a
JSON bool in a numeric field now reads "unreadable", not epoch-second 1).
Wet-tested read-only against the live box: the running daemon is loaded but
emits no heartbeat → line correctly reads "running, but not reporting a heartbeat
— likely stale code; restart: cms daemon restart" (matches the standing claudepad
note that the daemon needs a restart to pick up prior fixes). Docs updated
(README, ARCHITECTURE daemon/status/state-files/tests). **User still needs
`cms daemon restart`** to start emitting heartbeats (and to load all prior
fixes). One commit on main, unpushed.

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
