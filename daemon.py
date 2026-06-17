#!/usr/bin/env python3
"""
CMS keepalive daemon.
Checks active Claude tmux panes every 60s. If a pane's window has been idle
for >= 55 minutes (no terminal output), sends '. Enter' to keep context cached.

Panes are tracked in state.json keyed by "session:window.pane" (so older
daemon/CLI versions interoperate), but each entry also records tmux's
immutable pane id (%N) and pane PID. Keepalives target the pane id, and the
PID must still match — window indices can be recycled by tmux, and typing
into whatever now lives at a recycled index would be worse than doing nothing.
"""
import functools
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import statestore

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "daemon.log"

IDLE_THRESHOLD_SECS = 55 * 60  # 55 minutes
CHECK_INTERVAL_SECS = 60

# One list-panes call per cycle covers existence, identity, and idle time.
# #{window_activity} is the documented "time of last activity" variable
# (#{pane_activity} does not exist and expands to an empty string).
# Tab-separated; the session name goes last since it may contain spaces.
PANE_LIST_FORMAT = (
    "#{pane_id}\t#{pane_pid}\t#{window_activity}\t"
    "#{session_name}:#{window_index}.#{pane_index}"
)


@functools.lru_cache(maxsize=1)
def _tmux_bin() -> str:
    """launchd's default PATH lacks Homebrew, so resolve tmux explicitly."""
    found = shutil.which("tmux")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if Path(candidate).exists():
            return candidate
    return "tmux"


def _ensure_tmux_env():
    """launchd doesn't inherit the user session's tmux socket directory."""
    if "TMUX_TMPDIR" not in os.environ:
        os.environ["TMUX_TMPDIR"] = f"/tmp/tmux-{os.getuid()}"


def list_live_panes() -> list[dict] | None:
    """
    Return every pane on the server as
      {"uid": "%3", "pid": "412", "activity": 1781156029 | None, "addr": "cms:1.0"}.
    Returns [] when no server is running (panes are definitively gone) and
    None on unexpected tmux errors (caller should skip the cycle, not prune).
    """
    result = subprocess.run(
        [_tmux_bin(), "list-panes", "-a", "-F", PANE_LIST_FORMAT],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.lower()
        # "error connecting to <socket>" is what some tmux builds emit when
        # only a stale socket file is left behind — also definitively no server.
        if "no server running" in stderr or "error connecting to" in stderr:
            return []
        return None

    panes = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        uid, pid, activity, addr = parts
        try:
            activity_ts = int(activity)
        except ValueError:
            activity_ts = None
        panes.append({"uid": uid, "pid": pid, "activity": activity_ts, "addr": addr})
    return panes


def match_pane(key: str, info: dict, panes: list[dict]) -> dict | None:
    """Find the live pane for a tracked session, or None if it is gone."""
    uid, pid = info.get("pane_uid"), info.get("pane_pid")
    if uid:
        # Once a pane id was recorded, never fall back to address matching —
        # a recycled address must not receive keystrokes. The PID is verified
        # too when recorded.
        for pane in panes:
            if pane["uid"] == uid and (not pid or pane["pid"] == str(pid)):
                return pane
        return None
    # Legacy entry (written before pane ids were recorded): match by address.
    for pane in panes:
        if pane["addr"] == key:
            return pane
    return None


def scan(sessions: dict, panes: list[dict], now: int):
    """
    Pure decision step. Returns (to_ping, dead) where to_ping is a list of
    {"key", "target", "idle", "account"} and dead is a list of (key, info)
    pairs — the info snapshot lets the prune verify the entry is unchanged.
    """
    to_ping, dead = [], []
    for key, info in sessions.items():
        pane = match_pane(key, info, panes)
        if pane is None:
            dead.append((key, info))
            continue
        if pane["activity"] is None:
            continue
        idle = now - pane["activity"]
        if idle >= IDLE_THRESHOLD_SECS:
            to_ping.append(
                {
                    "key": key,
                    "target": pane["uid"],
                    "idle": idle,
                    "account": info.get("account", "?"),
                }
            )
    return to_ping, dead


def send_keepalive(target: str) -> bool:
    result = subprocess.run(
        [_tmux_bin(), "send-keys", "-t", target, ".", "Enter"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def run_once(now: int | None = None):
    now = int(time.time()) if now is None else now
    sessions = statestore.load_state().get("sessions", {})
    if not sessions:
        return

    panes = list_live_panes()
    if panes is None:
        logging.warning("tmux list-panes failed unexpectedly; skipping this cycle")
        return

    to_ping, dead = scan(sessions, panes, now)

    for ping in to_ping:
        if send_keepalive(ping["target"]):
            logging.info(
                f"Keepalive sent to {ping['key']} / {ping['target']} "
                f"({ping['account']}, idle {ping['idle']}s)"
            )
        else:
            logging.warning(f"Failed to send keepalive to {ping['target']}")

    if dead:
        # Re-reads state under the lock, and only removes an entry if it is
        # still the exact one we saw die — if cms.py just recorded a new
        # session under a recycled address, it survives.
        removed = []

        def prune(state):
            for key, info in dead:
                if state["sessions"].get(key) == info:
                    del state["sessions"][key]
                    removed.append(key)

        statestore.update_state(prune)
        for key in removed:
            logging.info(f"Pane {key} gone, removed from tracking")


def main():
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    _ensure_tmux_env()
    logging.info("CMS keepalive daemon started")

    while True:
        try:
            run_once()
        except Exception as e:
            logging.error(f"Daemon error: {e}", exc_info=True)
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    main()
