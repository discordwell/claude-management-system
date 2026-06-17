# Claudepad — Claude Management System

## Session Summaries

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
- Idle detection: `tmux display-message -p -t {pane_id} '#{pane_activity}'` → Unix timestamp
- Daemon checks every 60s, pings at 55min threshold

### Infrastructure
- Daemon runs as launchd service: `com.discordwell.cms-daemon`
- Installed: `~/.local/bin/cms` → `~/Projects/claude-management-system/cms.py`
- Account data: `~/.claude-accounts/{primary,secondary}/`
- tmux required: installed via homebrew
