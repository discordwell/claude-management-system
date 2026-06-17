"""
Shared state storage for cms.py and daemon.py.

state.json holds the tmux pane registry and the session-id counter:
  {"sessions": {"<session>:<window>.<pane>": {...}}, "next_session_id": N}

Both the CLI and the keepalive daemon mutate this file, so every
read-modify-write goes through update_state(), which serializes writers
with an exclusive flock on a sidecar lock file and replaces the file
atomically (a crash mid-write can never leave truncated JSON behind).
"""
import fcntl
import json
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
LOCK_FILE = BASE_DIR / ".state.lock"


def _read_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
        if not isinstance(state, dict):
            raise ValueError("state.json is not a JSON object")
    except (OSError, ValueError):
        state = {}
    state.setdefault("sessions", {})
    state.setdefault("next_session_id", 1)
    return state


def atomic_write_json(path: Path, data: dict):
    """Write JSON via temp file + rename so a crash can't leave truncated output."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state() -> dict:
    """Read-only snapshot of the current state (no lock needed: writes are atomic)."""
    return _read_state()


def update_state(mutator):
    """
    Atomically apply `mutator(state)` to state.json under an exclusive lock.
    The mutator edits the dict in place; its return value is passed through
    (falling back to the state dict itself when it returns None).
    """
    with open(LOCK_FILE, "a+") as lock:  # "a+" creates the file if missing
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = _read_state()
            result = mutator(state)
            atomic_write_json(STATE_FILE, state)
            return state if result is None else result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
