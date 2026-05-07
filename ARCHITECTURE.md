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

### `scraper.py` — Usage scraper
- **Primary account**: uses `browser_cookie3` to extract session cookies from the default Chrome profile (no browser needed — Chrome's `.claude.ai` session is read directly)
- **Secondary account**: uses playwright persistent context (`~/.claude-accounts/secondary/browser-context/`) — user logs in once via `cms setup`
- Required header: `anthropic-client-platform: web_claude_ai` (Bearer token → 403; cookies + this header → 200)
- API: `GET https://claude.ai/api/organizations` → org UUID, then
       `GET https://claude.ai/api/organizations/{uuid}/usage`
- Returns: `{five_hour: {utilization, resets_at}, seven_day: {...}, seven_day_sonnet: {...}, ...}`
- Results cached in `cache.json` for 5 minutes (CACHE_TTL)

### `daemon.py` — Keepalive daemon
- Runs as a launchd service (`com.discordwell.cms-daemon`)
- Every 60 seconds, iterates over tracked panes in `state.json`
- Uses `tmux display-message -p -t {pane_id} '#{pane_activity}'` to get last activity timestamp
- If idle >= 55 minutes: `tmux send-keys -t {pane_id} ". " Enter`
- Auto-removes panes from state when they close
- Logs to `daemon.log` / `daemon-error.log`

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

## State Files (in project dir)

- `state.json`: active tmux pane registry `{sessions: {pane_id: {account, started_at, ...}}}`
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
