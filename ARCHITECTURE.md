# Claude Management System — Architecture

## Purpose

Auto-balance Claude Code token consumption across two Claude Max accounts,
and maintain prompt cache by sending keepalive pings to idle sessions.

## Components

### `cms.py` — CLI entry point
- `cms` (no args): queries both accounts, picks the one with lower 5-hour utilization, launches Claude in a new tmux window
- `cms status`: shows live quota for both accounts
- `cms setup [--reauth account]`: first-run wizard + browser context setup
- `cms daemon start|stop|status|restart|logs`: manage keepalive daemon

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
  the lock); unexpected tmux failures skip the cycle rather than pruning
- Logs to `daemon.log` / `daemon-error.log`
- **Code changes take effect on the next `cms daemon restart`** — the running
  daemon keeps executing the old code until then

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

## Auth Architecture

Claude Code authenticates via `CLAUDE_CODE_OAUTH_TOKEN` env var (JSON with
accessToken/refreshToken/expiresAt, extracted from macOS Keychain on first setup).

The usage scraper uses separate **browser session cookies** managed by playwright
persistent contexts — the OAuth Bearer token returns 403 from the claude.ai web API.

## Selection Algorithm

1. Fetch 5-hour session utilization for both accounts (cached 5 min)
2. Select account with lower `five_hour.utilization`
3. Fallback to primary if secondary not configured or both equal

## Keepalive Rationale

Claude Max subscribers retain the 1-hour prompt cache TTL. Sending a `.` message
every 55 minutes (5 min before TTL expiry) keeps the context warm indefinitely
for as long as the session is open.

## Tests

`python3 -m unittest discover -s tests -t .` — covers state locking/atomicity,
daemon scan/prune/keepalive decisions (including the recycled-pane guard),
account selection, usage caching, and scraper auth/caching, all with tmux and
HTTP mocked.
