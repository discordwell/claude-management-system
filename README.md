# Claude Management System (cms)

Personal CLI that balances Claude Code usage across **two Claude Max accounts**
and keeps idle sessions' prompt caches warm.

- `cms` — checks both accounts' quota and launches Claude Code (in a tmux
  window) on the account with the most headroom: it skips an account whose
  weekly cap is exhausted, then picks the lower 5-hour usage
- `cms status` — live quota for both accounts (5-hour, 7-day, Sonnet buckets),
  a daemon health line (running / stalled / running stale code, with the fix),
  plus each tracked session's live/idle state (reconciled against tmux, so
  stale entries show as `gone` rather than lingering)
- A launchd keepalive daemon sends a tiny `.` message to any tracked session
  idle for 55 minutes, so the 1-hour prompt cache never goes cold

This is a single-user macOS tool for managing accounts you own. It reads usage
from claude.ai's web API with your own browser session cookies; that endpoint
is internal and may change without notice.

## Install

```sh
./install.sh     # symlinks cms into ~/.local/bin, installs deps + tmux
cms setup        # extracts primary token from Keychain, walks through secondary
```

Requirements: macOS, Python 3.10+, Homebrew (for tmux), Chrome logged into
claude.ai for the primary account.

## Usage

```sh
cms                  # launch Claude on the account with most headroom
cms primary          # force a specific account
cms secondary
cms status           # quota for both accounts + live/idle of tracked sessions
cms daemon logs      # tail the keepalive daemon log
cms daemon restart   # reload the daemon (needed after updating daemon.py)
cms setup --reauth secondary   # redo browser login for scraping
cms setup --reauth primary     # clear primary's cached org id (after Chrome re-login)
```

## How it works

| Piece | Job |
|---|---|
| `cms.py` | CLI: account selection, tmux launch, setup wizard, daemon control |
| `scraper.py` | Fetches quota from `claude.ai/api/organizations/{uuid}/usage` using browser cookies |
| `daemon.py` | launchd service; scans tracked tmux panes every 60s, pings idle ones |
| `statestore.py` | Locked, atomic access to `state.json` shared by CLI and daemon |
| `launch.sh` | Runs inside the tmux pane: exports the account's OAuth token, starts `claude` |

Each launched session runs in its own tmux window and is tracked in
`state.json` by tmux pane id **and** pane PID — the daemon refuses to type
into a pane whose PID no longer matches, so recycled window indices can never
receive stray keystrokes.

Account credentials live outside the repo in `~/.claude-accounts/{primary,secondary}/`
(`oauth_token.json` is chmod 600; browser contexts hold claude.ai cookies).
Never commit that directory.

## Development

```sh
python3 -m unittest discover -s tests -t .   # run the test suite
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for design details.
