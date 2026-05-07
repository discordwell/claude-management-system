#!/usr/bin/env python3
"""
CMS keepalive daemon.
Checks active Claude tmux panes every 60s. If a pane has been idle for
>= 55 minutes (no terminal activity), sends '. Enter' to keep context cached.
"""
import json
import logging
import os
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "daemon.log"

IDLE_THRESHOLD_SECS = 55 * 60  # 55 minutes
CHECK_INTERVAL_SECS = 60

# launchd doesn't inherit PATH or tmux's socket directory from the user session.
# Set TMUX_TMPDIR so tmux commands can locate the running server's socket.
_uid = os.getuid()
_tmux_dir = f"/tmp/tmux-{_uid}"
if "TMUX_TMPDIR" not in os.environ:
    os.environ["TMUX_TMPDIR"] = _tmux_dir

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"sessions": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def pane_exists(pane_id: str) -> bool:
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and pane_id in result.stdout.splitlines()


def get_pane_activity(pane_id: str) -> int | None:
    """Return Unix timestamp of last terminal activity in this pane, or None."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_activity}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def send_keepalive(pane_id: str) -> bool:
    result = subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, ".", "Enter"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main():
    logging.info("CMS keepalive daemon started")

    while True:
        try:
            state = load_state()
            sessions = state.get("sessions", {})
            now = int(time.time())
            dead = []

            for pane_id, info in sessions.items():
                if not pane_exists(pane_id):
                    dead.append(pane_id)
                    logging.info(f"Pane {pane_id} gone, removing")
                    continue

                activity = get_pane_activity(pane_id)
                if activity is None:
                    continue

                idle = now - activity
                if idle >= IDLE_THRESHOLD_SECS:
                    account = info.get("account", "?")
                    logging.info(
                        f"Pane {pane_id} ({account}) idle {idle}s — sending keepalive"
                    )
                    if send_keepalive(pane_id):
                        logging.info(f"Keepalive sent to {pane_id}")
                    else:
                        logging.warning(f"Failed to send keepalive to {pane_id}")

            if dead:
                for pane_id in dead:
                    del state["sessions"][pane_id]
                save_state(state)

        except Exception as e:
            logging.error(f"Daemon error: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    main()
