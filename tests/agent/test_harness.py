from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.amethystd.container_io import safe_component
from tools.amethystd.jit import JITSession
from tools.amethystd.model import FailureClass, RunState, Stage
from tools.amethystd.store import StateStore


class RunStateTests(unittest.TestCase):
    def test_process_generation_invalidates_process_scoped_evidence(self) -> None:
        state = RunState(run_id="run-1", bundle_id="test")
        state.observe_process(10, "10-a")
        state.transition(Stage.WORLD_READY)
        state.mark_jit(
            exec_ready=True,
            dynamic_library_load_ready=True,
            evidence={"jit_mapping": True, "renderer_ready": True, "world_ready": True},
        )
        changed = state.observe_process(11, "11-b")
        self.assertTrue(changed)
        self.assertEqual(state.stage, Stage.APP_LAUNCHED)
        self.assertFalse(state.jit_exec_ready)
        self.assertFalse(state.dynamic_library_load_ready)
        self.assertNotIn("renderer_ready", state.evidence)
        self.assertNotIn("world_ready", state.evidence)

    def test_failure_serialization_round_trip(self) -> None:
        state = RunState(run_id="run-2", bundle_id="test")
        state.fail(FailureClass.JIT_MODE_INSUFFICIENT, "rx denied")
        restored = RunState.from_dict(state.to_dict())
        self.assertEqual(restored.failure, FailureClass.JIT_MODE_INSUFFICIENT)
        self.assertEqual(restored.stage, Stage.FAIL)


class StoreTests(unittest.TestCase):
    def test_atomic_state_and_append_only_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            state = RunState(run_id="run-3", bundle_id="test")
            store.save(state)
            store.append_event(state.run_id, "proof", value=1)
            self.assertEqual(store.load().run_id, state.run_id)
            rows = (store.artifact_dir(state.run_id) / "host-events.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(rows[0])["event"], "proof")


class ContractTests(unittest.TestCase):
    def test_safe_components_reject_paths(self) -> None:
        for bad in ("../x", "a/b", "", "a b"):
            with self.assertRaises(ValueError):
                safe_component(bad, "id")
        self.assertEqual(safe_component("run_ABC-123", "id"), "run_ABC-123")

    def test_jit_markers_require_all_positive_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jit.log"
            path.write_text("Got JIT mapping\n", encoding="utf-8")
            self.assertFalse(JITSession._contains_all(path, ("Got JIT mapping", "mapping at RW=")))
            path.write_text("Got JIT mapping\nmapping at RW=0x1 RX=0x2\n", encoding="utf-8")
            self.assertTrue(JITSession._contains_all(path, ("Got JIT mapping", "mapping at RW=")))


if __name__ == "__main__":
    unittest.main()
