# Claude Management System — Architecture

## Purpose

Auto-balance Claude Code token consumption across two Claude Max accounts,
and maintain prompt cache by sending keepalive pings to idle sessions.

## Components

### `cms.py` — CLI entry point
- `cms` (no args): queries every *configured* account (one or both), picks the
  one with most headroom, launches Claude in a new tmux window — a single-account
  (e.g. secondary-only) setup is supported, gated by `any_configured()`
- `cms status`: shows live quota for both accounts, a **daemon health line**, and
  each tracked session's live/idle state, reconciled against tmux via the daemon's
  pane matcher (a stale entry shows as `gone (daemon will prune)`, not as a phantom
  active session). The daemon line cross-checks launchd (`_daemon_loaded()`)
  against the daemon's own heartbeat (`daemon.read_status()`) so a not-running,
  *stalled* (no recent heartbeat), or *stale-code* daemon is called out with the
  fix — the visibility gap that previously hid a multi-day crash-loop and a
  6-day-idle ghost pane whose daemon was running pre-fix code
- `cms setup [--reauth account]`: first-run wizard + browser context setup;
  `--reauth secondary` redoes the browser login, `--reauth primary` clears the
  cached org uuid (primary scrapes with live Chrome cookies, so that uuid is the
  only thing that can go stale — e.g. after logging Chrome into a different org)
- `cms daemon start|stop|status|restart|logs`: manage keepalive daemon
- All tmux invocations run through `_tmux()`, which exits with a one-line
  install hint if tmux is missing (a fresh box where `brew install tmux` never
  ran) rather than dumping a `FileNotFoundError` traceback. `cms status` keeps
  working without tmux — `list_live_panes` returns `None` and liveness shows as
  `unknown`, while the quota section (which needs no tmux) still renders

### `statestore.py` — Shared state storage
- Both the CLI and the daemon mutate `state.json`, so all read-modify-write
  cycles go through `update_state(mutator)`: exclusive `flock` on `.state.lock`,
  then an atomic temp-file + `os.replace` write (a crash can never leave
  truncated JSON; a daemon prune can never clobber a session the CLI just added)
- Missing/corrupt state degrades to `{"sessions": {}, "next_session_id": 1}`
- Also provides `atomic_write_json` used for `cache.json`

### `scraper.py` — Usage scraper
- **Primary account**: uses `browser_cookie3` to extract session cookies from the default Chrome profile (no browser needed — Chrome's `.claude.ai` session is read directly)
- **Secondary account**: uses playwright persistent context (`~/.claude-accounts/secondary/browser-context/`) — user logs in once via `cms setup`
- Required header: `anthropic-client-platform: web_claude_ai` (Bearer token → 403; cookies + this header → 200)
- API: `GET https://claude.ai/api/organizations` → org UUID, then
       `GET https://claude.ai/api/organizations/{uuid}/usage`
- Both requests go through `_auth_checked_get`, which maps a 401/403 to an
  actionable `RuntimeError` ("run `cms setup --reauth {account}`"). The org
  uuid is cached, so on the common path the usage call is the *only* request —
  routing it through the same check means a stale-cookie failure still explains
  the fix instead of surfacing a bare `HTTP 403`
- Returns: `{five_hour: {utilization, resets_at}, seven_day: {...}, seven_day_sonnet: {...}, ...}`
- All requests carry a 30s timeout; results cached in `cache.json` for 5 minutes (CACHE_TTL)
- Heavy deps (`requests`, `browser_cookie3`, `playwright`) import lazily so the module always imports

### `daemon.py` — Keepalive daemon
- Runs as a launchd service (`com.discordwell.cms-daemon`); the plist sets PATH
  to include Homebrew so the daemon can find tmux (launchd's default PATH cannot)
- Every 60 seconds, runs **one** `tmux list-panes -a` with a tab-separated
  format (`#{pane_id}`, `#{pane_pid}`, `#{window_activity}`, then
  `session:window.pane` last since session names may contain spaces),
  covering existence, identity, and idle time for all panes at once
- `#{window_activity}` is the documented last-activity timestamp
  (`#{pane_activity}` does not exist in tmux and expands empty — using it
  means keepalives never fire)
- If idle >= 55 minutes: `tmux send-keys -t {pane_id} . Enter`, targeting the
  immutable pane id (`%N`) — and only if the recorded pane PID still matches,
  so a recycled window index never receives stray keystrokes
- Entries written by older versions (no pane id recorded) fall back to matching
  the `session:window.pane` address
- Auto-removes dead panes via `statestore.update_state` (fresh re-read under
  the lock); unexpected tmux failures — including a missing tmux binary
  (`FileNotFoundError`) — skip the cycle rather than pruning
- Logs to `daemon.log` (size-capped: a `RotatingFileHandler` keeps it under
  ~1 MB with 3 rotations, so a failure loop can't fill the disk) and
  `daemon-error.log`
- **Writes a heartbeat** to `daemon_status.json` at the top of every cycle
  (`write_heartbeat`): `{pid, started_at, code_mtime, last_run}`. `code_mtime`
  is snapshotted **once at startup** from the daemon's source files
  (`source_mtime()` over `daemon.py` + `statestore.py`), so when the live source
  is later edited the CLI can detect the daemon is running stale code. The file
  is single-writer (only the daemon), so an atomic replace suffices — no lock —
  and a write failure is logged and swallowed rather than crashing the loop
- **Code changes take effect on the next `cms daemon restart`** — the running
  daemon keeps executing the old code until then; the heartbeat's `code_mtime`
  makes that staleness visible in `cms status` instead of silent

### `launch.sh` — Per-session launcher (called by tmux)
- Takes account name as arg
- Reads `~/.claude-accounts/{account}/oauth_token.json`
- Sets `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CONFIG_DIR`, then runs `claude`

## Account Storage

```
~/.claude-accounts/
├── primary/
│   ├── oauth_token.json       # {accessToken, refreshToken, expiresAt}
│   ├── browser-context/       # playwright persistent context (for scraping)
│   └── settings.json          # copied from ~/.claude on first setup
└── secondary/
    ├── oauth_token.json
    └── browser-context/
```

## State Files (in project dir, all gitignored)

- `state.json`: active tmux pane registry
  `{sessions: {"session:window.pane": {account, started_at, session_id, pane_name, pane_uid, pane_pid}}, next_session_id: N}`
  (keyed by address for backward compatibility; `pane_uid`/`pane_pid` identify the pane robustly)
- `.state.lock`: flock sidecar serializing state writers
- `cache.json`: quota cache `{usage_primary: {ts, data}, usage_secondary: {ts, data}}`
- `daemon.log`: keepalive daemon log (last 50 lines via `cms daemon logs`)
- `daemon_status.json`: daemon heartbeat `{pid, started_at, code_mtime, last_run}`,
  rewritten each cycle; read by `cms status` to report daemon health

## Auth Architecture

Claude Code authenticates via `CLAUDE_CODE_OAUTH_TOKEN` env var (JSON with
accessToken/refreshToken/expiresAt, extracted from macOS Keychain on first setup).

The usage scraper uses separate **browser session cookies** managed by playwright
persistent contexts — the OAuth Bearer token returns 403 from the claude.ai web API.

## Selection Algorithm

Each configured account is scored by `cms._account_score(usage)`, which returns
the sort key `(weekly_capped, five_hour_utilization)` — lower is better:

1. Fetch usage for each configured account (one or both; cached 5 min)
2. `weekly_capped` is 1 when `seven_day.utilization >= 100` — an account whose
   7-day cap is exhausted is avoided *before* 5-hour headroom is compared, since
   launching onto a weekly-dead account would fail immediately even though its
   5-hour bucket looks fresh
3. Among accounts with weekly headroom, pick the lower `five_hour.utilization`
4. Comparison is strict (`<`) over the fallback order (primary, secondary), so
   the first account wins ties — i.e. primary by default
5. Missing/None/non-numeric usage (a failed fetch, or an explicit `null`
   utilization from the API) is treated as fully exhausted, so a degraded
   account never wins the selection by accident

## Keepalive Rationale

Claude Max subscribers retain the 1-hour prompt cache TTL. Sending a `.` message
every 55 minutes (5 min before TTL expiry) keeps the context warm indefinitely
for as long as the session is open.

## Tests

`python3 -m unittest discover -s tests -t .` — covers state locking/atomicity,
daemon scan/prune/keepalive decisions (including the recycled-pane guard),
daemon heartbeat read/write + the `_daemon_status_line` health classifier
(not-running / stalled / stale-code / healthy), account selection, usage caching,
and scraper auth/caching, all with tmux, launchctl, and HTTP mocked.
