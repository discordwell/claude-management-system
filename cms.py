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

import statestore

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_DIR = Path.home() / ".claude-accounts"
CACHE_FILE = BASE_DIR / "cache.json"
LAUNCH_SCRIPT = BASE_DIR / "launch.sh"
DAEMON_PLIST_DEST = Path.home() / "Library/LaunchAgents/com.discordwell.cms-daemon.plist"
DAEMON_LABEL = "com.discordwell.cms-daemon"
CACHE_TTL = 300  # 5 minutes

# Captures the stable pane id (%N) and pane PID alongside the human-readable
# address so the daemon can verify it is typing into the right pane even if
# tmux recycles window indices.
PANE_OUTPUT_FORMAT = "#{session_name}:#{window_index}.#{pane_index}|#{pane_id}|#{pane_pid}"


# ── Cache ────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    statestore.atomic_write_json(CACHE_FILE, cache)


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


def _utilization(bucket: dict | None, default: float) -> float:
    """Pull a numeric utilization% from a usage bucket, tolerating null/missing.

    The web API can return an explicit ``null`` utilization (not just omit the
    key), which must not crash the comparison in best_account().
    """
    if not isinstance(bucket, dict):
        return default
    pct = bucket.get("utilization")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return default  # None, missing, or non-numeric → assume the default
    return float(pct)


def _account_score(data: dict | None) -> tuple[int, float]:
    """Sort key for account selection — lower is better.

    Returns ``(weekly_capped, five_hour_utilization)`` so an account whose
    7-day cap is exhausted is avoided *before* 5-hour headroom is compared:
    launching onto a weekly-dead account would fail immediately even though
    its 5-hour bucket looks fresh. Missing/None data is treated as fully
    exhausted (a failed fetch should not win the race to be selected).
    """
    if not data:
        return (1, 100.0)
    five_hour = _utilization(data.get("five_hour"), default=100.0)
    weekly = _utilization(data.get("seven_day"), default=0.0)
    weekly_capped = 1 if weekly >= 100.0 else 0
    return (weekly_capped, five_hour)


def best_account() -> tuple[str, dict]:
    """Return (account_name, {account: usage_dict}) for the account with most headroom."""
    usage = {}
    for acct in ("primary", "secondary"):
        if is_configured(acct):
            usage[acct] = fetch_usage(acct)

    if not usage:
        print("No accounts configured. Run: cms setup")
        sys.exit(1)

    # Prefer the account with a free weekly cap, then the lower 5-hour usage.
    # usage is built in fallback order (primary, secondary) and we compare with
    # a strict "<", so the first account wins ties — i.e. primary by default.
    best = None
    best_score = None
    for acct, data in usage.items():
        score = _account_score(data)
        if best_score is None or score < best_score:
            best, best_score = acct, score

    return best or "primary", usage


# ── tmux ─────────────────────────────────────────────────────────────────────

def _parse_pane_output(out: str) -> tuple[str, str | None, str | None]:
    """Split PANE_OUTPUT_FORMAT output into (address, pane_uid, pane_pid)."""
    parts = out.rsplit("|", 2)
    if len(parts) != 3:
        return out, None, None
    addr, uid, pid = parts
    return addr, uid or None, pid or None


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

    def claim_id(state):
        sid = state["next_session_id"]
        state["next_session_id"] = sid + 1
        return sid

    session_id = statestore.update_state(claim_id)
    pane_name = f"claude-{session_id}"

    # Pass as list to avoid shell-splitting issues with paths containing spaces
    tmux_cmd = ["bash", str(LAUNCH_SCRIPT), account]

    # Create window in existing session, or bootstrap a new one
    result = subprocess.run(
        ["tmux", "new-window", "-n", pane_name, "-P", "-F", PANE_OUTPUT_FORMAT] + tmux_cmd,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # No session running — create one
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", "cms", "-n", pane_name, "-P", "-F",
             PANE_OUTPUT_FORMAT] + tmux_cmd,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Error creating tmux session: {result.stderr}")
            sys.exit(1)

    pane_addr, pane_uid, pane_pid = _parse_pane_output(result.stdout.strip())

    def record_session(state):
        state["sessions"][pane_addr] = {
            "account": account,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "pane_name": pane_name,
            "pane_uid": pane_uid,
            "pane_pid": pane_pid,
        }

    statestore.update_state(record_session)

    print(f"Launched pane: {pane_addr}  (tracked for keepalive)")

    # Attach — split on ":" to get session name (pane_addr is "session:window.pane")
    session_name = pane_addr.split(":")[0]
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "select-window", "-t", pane_uid or pane_addr])
    else:
        subprocess.run(["tmux", "attach", "-t", session_name])


# ── Status ───────────────────────────────────────────────────────────────────

def _fmt_util(data: dict | None, key: str) -> str:
    if not data:
        return "N/A"
    bucket = (data.get(key) or {})
    if not bucket:
        return "N/A"
    pct = bucket.get("utilization")
    if pct is None:
        return "N/A"  # the API can send an explicit null utilization
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

    state = statestore.load_state()
    sessions = state.get("sessions", {})
    if sessions:
        print(f"  Active sessions: {len(sessions)}")
        for pid, info in sessions.items():
            started = (info.get("started_at") or "")[:19]
            print(f"    {pid}  ({info.get('account', '?')})  started {started}")
    else:
        print("  No active sessions tracked.")


# ── Daemon ───────────────────────────────────────────────────────────────────

def _plist_content() -> str:
    py = sys.executable
    script = str(BASE_DIR / "daemon.py")
    log = str(BASE_DIR / "daemon.log")
    err_log = str(BASE_DIR / "daemon-error.log")
    # launchd's default PATH is /usr/bin:/bin:/usr/sbin:/sbin — include the
    # Homebrew prefixes so the daemon can find tmux.
    path_env = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
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
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
    </dict>
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
        err_log = BASE_DIR / "daemon-error.log"
        if err_log.exists() and err_log.stat().st_size > 0:
            err_lines = err_log.read_text().splitlines()
            print("\n── daemon-error.log (last 20) ──")
            print("\n".join(err_lines[-20:]))


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
            oauth = {}
            if r.returncode == 0:
                try:
                    oauth = json.loads(r.stdout).get("claudeAiOauth", {})
                except json.JSONDecodeError:
                    pass
            token_data = {
                k: oauth[k]
                for k in ("accessToken", "refreshToken", "expiresAt")
                if k in oauth
            }
            if "accessToken" in token_data:
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
