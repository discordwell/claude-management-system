#!/usr/bin/env python3
"""
cms — Claude Management System
Auto-balances token usage across two Claude Max accounts and maintains
context cache with 55-minute keepalives via tmux.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
ACCOUNTS_DIR = Path.home() / ".claude-accounts"
STATE_FILE = BASE_DIR / "state.json"
CACHE_FILE = BASE_DIR / "cache.json"
LAUNCH_SCRIPT = BASE_DIR / "launch.sh"
DAEMON_PLIST_DEST = Path.home() / "Library/LaunchAgents/com.discordwell.cms-daemon.plist"
DAEMON_LABEL = "com.discordwell.cms-daemon"
CACHE_TTL = 300  # 5 minutes


# ── State / Cache ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"sessions": {}, "next_session_id": 1}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ── Account / Usage ──────────────────────────────────────────────────────────

def is_configured(account: str) -> bool:
    return (ACCOUNTS_DIR / account / "oauth_token.json").exists()


def fetch_usage(account: str) -> dict | None:
    """Fetch live usage, updating cache. Returns usage dict or None on error."""
    import scraper as sc

    cache = load_cache()
    now = time.time()
    key = f"usage_{account}"
    cached = cache.get(key, {})

    if cached.get("ts", 0) + CACHE_TTL > now:
        return cached["data"]

    try:
        data = sc.get_usage(account)
        cache[key] = {"ts": now, "data": data}
        save_cache(cache)
        return data
    except Exception as e:
        print(f"  [warn] could not fetch usage for {account}: {e}")
        return cached.get("data")  # return stale data if available


def best_account() -> tuple[str, dict]:
    """Return (account_name, {account: usage_dict}) for the account with most headroom."""
    usage = {}
    for acct in ("primary", "secondary"):
        if is_configured(acct):
            usage[acct] = fetch_usage(acct)

    if not usage:
        print("No accounts configured. Run: cms setup")
        sys.exit(1)

    # Score: lower five_hour utilization = more headroom. Unconfigured = skip.
    # Fallback order: primary, secondary.
    best = None
    best_util = float("inf")
    for acct, data in usage.items():
        util = (data or {}).get("five_hour", {}) or {}
        pct = util.get("utilization", 100.0)
        if pct < best_util:
            best_util = pct
            best = acct

    return best or "primary", usage


# ── tmux ─────────────────────────────────────────────────────────────────────

def tmux_running() -> bool:
    return subprocess.run(["tmux", "list-sessions"], capture_output=True).returncode == 0


def launch_session(account: str | None = None):
    if account is None:
        account, usage = best_account()
        other = "secondary" if account == "primary" else "primary"
        chosen_pct = (usage.get(account) or {}).get("five_hour", {}) or {}
        other_pct = (usage.get(other) or {}).get("five_hour", {}) or {}
        print(f"Selected: {account}  ({chosen_pct.get('utilization', '?')}% session used)")
        if other in usage:
            print(f"  {other}: {other_pct.get('utilization', '?')}% session used")
    else:
        if not is_configured(account):
            print(f"Account '{account}' not configured. Run: cms setup")
            sys.exit(1)

    state = load_state()
    session_id = state["next_session_id"]
    state["next_session_id"] = session_id + 1
    pane_name = f"claude-{session_id}"

    # Pass as list to avoid shell-splitting issues with paths containing spaces
    tmux_cmd = ["bash", str(LAUNCH_SCRIPT), account]

    # Create window in existing session, or bootstrap a new one
    result = subprocess.run(
        ["tmux", "new-window", "-n", pane_name, "-P", "-F",
         "#{session_name}:#{window_index}.#{pane_index}"] + tmux_cmd,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # No session running — create one
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", "cms", "-n", pane_name, "-P", "-F",
             "#{session_name}:#{window_index}.#{pane_index}"] + tmux_cmd,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Error creating tmux session: {result.stderr}")
            sys.exit(1)

    pane_id = result.stdout.strip()

    state["sessions"][pane_id] = {
        "account": account,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "pane_name": pane_name,
    }
    save_state(state)

    print(f"Launched pane: {pane_id}  (tracked for keepalive)")

    # Attach — split on ":" to get session name (pane_id is "session:window.pane")
    session_name = pane_id.split(":")[0]
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "select-window", "-t", pane_id])
    else:
        subprocess.run(["tmux", "attach", "-t", session_name])


# ── Status ───────────────────────────────────────────────────────────────────

def _fmt_util(data: dict | None, key: str) -> str:
    if not data:
        return "N/A"
    bucket = (data.get(key) or {})
    if not bucket:
        return "N/A"
    pct = bucket.get("utilization", "?")
    resets = bucket.get("resets_at", "")
    resets_str = f"  resets {resets}" if resets else ""
    return f"{pct}%{resets_str}"


def show_status():
    print("═══ Claude Management System ═══\n")
    for acct in ("primary", "secondary"):
        if not is_configured(acct):
            print(f"  {acct.upper():10s}  [not configured]\n")
            continue
        data = fetch_usage(acct)
        print(f"  {acct.upper():10s}")
        print(f"    Session (5h):  {_fmt_util(data, 'five_hour')}")
        print(f"    Weekly  (7d):  {_fmt_util(data, 'seven_day')}")
        print(f"    Sonnet  (7d):  {_fmt_util(data, 'seven_day_sonnet')}")
        print()

    state = load_state()
    sessions = state.get("sessions", {})
    if sessions:
        print(f"  Active sessions: {len(sessions)}")
        for pid, info in sessions.items():
            print(f"    {pid}  ({info['account']})  started {info['started_at'][:19]}")
    else:
        print("  No active sessions tracked.")


# ── Daemon ───────────────────────────────────────────────────────────────────

def _plist_content() -> str:
    py = sys.executable
    script = str(BASE_DIR / "daemon.py")
    log = str(BASE_DIR / "daemon.log")
    err_log = str(BASE_DIR / "daemon-error.log")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{DAEMON_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{script}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{err_log}</string>
    <key>WorkingDirectory</key>
    <string>{BASE_DIR}</string>
</dict>
</plist>"""


def manage_daemon(action: str):
    if action == "start":
        DAEMON_PLIST_DEST.parent.mkdir(parents=True, exist_ok=True)
        DAEMON_PLIST_DEST.write_text(_plist_content())
        r = subprocess.run(
            ["launchctl", "load", str(DAEMON_PLIST_DEST)], capture_output=True, text=True
        )
        if r.returncode == 0:
            print(f"Daemon started (plist: {DAEMON_PLIST_DEST})")
        else:
            print(f"launchctl error: {r.stderr.strip()}")

    elif action == "stop":
        subprocess.run(["launchctl", "unload", str(DAEMON_PLIST_DEST)])
        print("Daemon stopped.")

    elif action == "status":
        r = subprocess.run(
            ["launchctl", "list", DAEMON_LABEL], capture_output=True, text=True
        )
        if r.returncode == 0:
            print("Daemon is RUNNING.")
            print(r.stdout)
        else:
            print("Daemon is NOT running.")

    elif action == "restart":
        manage_daemon("stop")
        time.sleep(1)
        manage_daemon("start")

    elif action == "logs":
        log = BASE_DIR / "daemon.log"
        if log.exists():
            lines = log.read_text().splitlines()
            print("\n".join(lines[-50:]))
        else:
            print("No log file yet.")


# ── Setup ────────────────────────────────────────────────────────────────────

def run_setup(reauth: str | None = None):
    print("═══ CMS Setup ═══\n")
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

    accounts_to_setup = [reauth] if reauth else ["primary", "secondary"]

    for acct in accounts_to_setup:
        acct_dir = ACCOUNTS_DIR / acct
        acct_dir.mkdir(exist_ok=True)
        token_file = acct_dir / "oauth_token.json"

        print(f"── {acct.upper()} ACCOUNT ──")

        if acct == "primary" and not token_file.exists():
            print("Extracting OAuth token from macOS Keychain...")
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                keychain = json.loads(r.stdout)
                oauth = keychain.get("claudeAiOauth", {})
                # Store only the fields CLAUDE_CODE_OAUTH_TOKEN needs
                token_data = {
                    k: oauth[k]
                    for k in ("accessToken", "refreshToken", "expiresAt")
                    if k in oauth
                }
                token_file.write_text(json.dumps(token_data))
                token_file.chmod(0o600)
                print("  ✓ Token extracted from Keychain")
            else:
                print("  Could not read Keychain. Make sure you're logged into Claude Code.")
                print("  Run: claude login   then re-run: cms setup")
                sys.exit(1)

            # Copy settings from ~/.claude if they exist
            src_settings = Path.home() / ".claude" / "settings.json"
            dst_settings = acct_dir / "settings.json"
            if src_settings.exists() and not dst_settings.exists():
                import shutil
                shutil.copy2(src_settings, dst_settings)
                print("  ✓ Copied settings.json from ~/.claude")

        elif acct == "secondary" and not token_file.exists():
            print("  For your SECOND account, generate a token with:")
            print("    CLAUDE_CONFIG_DIR=~/.claude-accounts/secondary claude login")
            print("  Then re-run: cms setup")
            print()
            print("  Or paste the OAuth JSON token below (Ctrl+D to skip):")
            try:
                lines = []
                while True:
                    line = input()
                    lines.append(line)
            except EOFError:
                pass
            token_input = "\n".join(lines).strip()
            if token_input:
                try:
                    parsed = json.loads(token_input)
                    token_data = {
                        k: parsed[k]
                        for k in ("accessToken", "refreshToken", "expiresAt")
                        if k in parsed
                    }
                    if "accessToken" not in token_data:
                        print("  Invalid token (missing accessToken). Skipping.")
                    else:
                        token_file.write_text(json.dumps(token_data))
                        token_file.chmod(0o600)
                        print("  ✓ Secondary token saved")
                except json.JSONDecodeError:
                    print("  Invalid JSON. Skipping secondary account for now.")
            else:
                print("  Skipped. Only primary account will be used.")
        else:
            print("  ✓ Already configured")

        # For secondary: set up playwright browser context for scraping
        # (primary uses Chrome default profile cookies via browser_cookie3 — no browser needed)
        if acct == "secondary" and token_file.exists():
            cfg_file = acct_dir / "scraper_config.json"
            context_dir = acct_dir / "browser-context"
            if not cfg_file.exists() or reauth:
                print("  Setting up scraping for secondary account...")
                print("  A browser will open — log into claude.ai with your SECONDARY account.")
                _ensure_playwright()
                import scraper as sc
                sc.setup_browser_context(acct)
                print(f"  ✓ Browser context saved for {acct}")
            else:
                print("  ✓ Scraper already configured")

        print()

    # Install daemon
    print("── DAEMON ──")
    manage_daemon("start")
    print()
    print("✓ Setup complete.")
    print("  Run 'cms' to launch a balanced Claude session.")
    print("  Run 'cms status' to check quota.")


def _ensure_playwright():
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Installing playwright...")
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=True
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog="cms",
        description="Claude Management System — auto-balances two Max accounts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cms                   Launch Claude on the account with most headroom
  cms primary           Force primary account
  cms secondary         Force secondary account
  cms status            Show quota for both accounts
  cms setup             First-run wizard
  cms setup --reauth primary   Re-authenticate primary browser context
  cms daemon start|stop|status|restart|logs
        """,
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status")
    sub.add_parser("primary")
    sub.add_parser("secondary")

    sp = sub.add_parser("setup")
    sp.add_argument("--reauth", choices=["primary", "secondary"], default=None)

    dp = sub.add_parser("daemon")
    dp.add_argument("action", choices=["start", "stop", "status", "restart", "logs"])

    args = p.parse_args()

    if args.cmd == "status":
        show_status()
    elif args.cmd == "setup":
        run_setup(reauth=args.reauth)
    elif args.cmd == "daemon":
        manage_daemon(args.action)
    elif args.cmd in ("primary", "secondary"):
        launch_session(account=args.cmd)
    else:
        # Default: auto-balance launch
        if not is_configured("primary"):
            print("First time? Run: cms setup")
            sys.exit(1)
        launch_session()


if __name__ == "__main__":
    main()
