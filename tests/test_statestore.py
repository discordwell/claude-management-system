import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import statestore


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.state_file = base / "state.json"
        patches = [
            mock.patch.object(statestore, "STATE_FILE", self.state_file),
            mock.patch.object(statestore, "LOCK_FILE", base / ".state.lock"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_defaults_when_missing(self):
        state = statestore.load_state()
        self.assertEqual(state, {"sessions": {}, "next_session_id": 1})

    def test_defaults_when_corrupt(self):
        self.state_file.write_text("{not json")
        state = statestore.load_state()
        self.assertEqual(state, {"sessions": {}, "next_session_id": 1})

    def test_defaults_when_not_an_object(self):
        self.state_file.write_text('["a", "list"]')
        state = statestore.load_state()
        self.assertEqual(state, {"sessions": {}, "next_session_id": 1})

    def test_missing_keys_are_filled_in(self):
        self.state_file.write_text('{"sessions": {"cms:1.0": {"account": "primary"}}}')
        state = statestore.load_state()
        self.assertEqual(state["next_session_id"], 1)
        self.assertIn("cms:1.0", state["sessions"])

    def test_update_persists_and_returns_mutator_value(self):
        def claim(state):
            sid = state["next_session_id"]
            state["next_session_id"] = sid + 1
            return sid

        self.assertEqual(statestore.update_state(claim), 1)
        self.assertEqual(statestore.update_state(claim), 2)
        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk["next_session_id"], 3)

    def test_update_returns_state_when_mutator_returns_none(self):
        result = statestore.update_state(lambda s: s["sessions"].update({"k": {}}))
        self.assertIn("k", result["sessions"])

    def test_update_failure_leaves_file_untouched(self):
        statestore.update_state(lambda s: s["sessions"].update({"keep": {}}))

        def boom(state):
            raise RuntimeError("mutator failed")

        with self.assertRaises(RuntimeError):
            statestore.update_state(boom)
        self.assertEqual(statestore.load_state()["sessions"], {"keep": {}})

    def test_concurrent_updates_do_not_lose_writes(self):
        threads_n, per_thread = 8, 10

        def claim(state):
            sid = state["next_session_id"]
            state["next_session_id"] = sid + 1
            state["sessions"][f"pane-{sid}"] = {"sid": sid}
            return sid

        def worker():
            for _ in range(per_thread):
                statestore.update_state(claim)

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = statestore.load_state()
        self.assertEqual(state["next_session_id"], threads_n * per_thread + 1)
        self.assertEqual(len(state["sessions"]), threads_n * per_thread)

    def test_writes_are_valid_json_on_disk(self):
        statestore.update_state(lambda s: s["sessions"].update({"a": {"x": 1}}))
        json.loads(self.state_file.read_text())  # must not raise

    def test_update_creates_missing_lock_file(self):
        self.assertFalse(statestore.LOCK_FILE.exists())
        statestore.update_state(lambda s: None)
        self.assertTrue(statestore.LOCK_FILE.exists())
        # And keeps working if someone deletes it between updates.
        statestore.LOCK_FILE.unlink()
        statestore.update_state(lambda s: s["sessions"].update({"k": {}}))
        self.assertIn("k", statestore.load_state()["sessions"])


if __name__ == "__main__":
    unittest.main()
