from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest.mock import patch

from tools.amethystd.mithril_hot_swap import HotSwapLedger, MITHRIL_PAYLOAD, MithrilHotSwapManager
from tools.amethystd.store import StateStore


class RollbackDaemon:
    def __init__(self, active_digest: str, process_generation: str = "rollback-process") -> None:
        self.active_digest = active_digest
        self.process_generation = process_generation
        self.calls: list[tuple[str, dict | None]] = []

    def inspect(self) -> dict:
        if self.active_digest == "bundled":
            return {"ok": True, "name": MITHRIL_PAYLOAD, "device": {"ok": True, "active": False}}
        return {
            "ok": True,
            "name": MITHRIL_PAYLOAD,
            "device": {
                "ok": True,
                "active": True,
                "manifest": {
                    "version": 1,
                    "name": MITHRIL_PAYLOAD,
                    "digest": self.active_digest,
                    "files": [],
                },
            },
        }

    async def __call__(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params))
        if method == "status":
            return {"ok": True, "state": {"process_generation": "old-process"}}
        if method == "inspect_payload":
            return self.inspect()
        if method == "clear_payload":
            self.active_digest = "bundled"
            return {"ok": True, "name": MITHRIL_PAYLOAD, "active": False}
        if method == "run_smoke":
            return {
                "ok": True,
                "state": {
                    "stage": "PASS",
                    "run_id": "rollback-run",
                    "game_session_generation": "rollback-session",
                    "process_generation": self.process_generation,
                    "candidate": {
                        "active": self.active_digest != "bundled",
                        "digest": self.active_digest,
                    },
                },
            }
        raise AssertionError(method)


class RollbackLabClient:
    def __init__(self, daemon: RollbackDaemon) -> None:
        self.daemon = daemon

    async def read_lab_events(self, _run_id: str, _session: str) -> list[dict]:
        digest = self.daemon.active_digest
        loaded = (
            f"/private/var/mobile/X/Documents/agent-payloads/.staging/{digest}/libmithril.dylib"
            if digest != "bundled"
            else "/private/var/containers/Bundle/Application/X/AngelAuraAmethyst.app/Frameworks/libmithril.dylib"
        )
        return [
            {
                "event": "renderer_ready",
                "provenance_ok": True,
                "candidate_digest": digest,
                "loaded_library_path": loaded,
                "glCompileShader": "100",
                "glDrawElements": "200",
            }
        ]


class RollbackOrchestrationTests(unittest.TestCase):
    def test_default_rollback_reactivates_previous_staged_digest_then_revalidates(self) -> None:
        old_digest = "a" * 64
        bad_digest = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            HotSwapLedger(store).append(
                {
                    "version": 1,
                    "id": "bad-swap",
                    "operation": "swap",
                    "status": "failed",
                    "before_digest": old_digest,
                    "after_digest": bad_digest,
                }
            )
            daemon = RollbackDaemon(bad_digest)
            lab = RollbackLabClient(daemon)
            manager = MithrilHotSwapManager(store, daemon, lab)  # type: ignore[arg-type]

            async def activate(_client, digest: str):
                self.assertEqual(digest, old_digest)
                daemon.active_digest = digest
                return {
                    "ok": True,
                    "digest": digest,
                    "manifest": {"version": 1, "name": MITHRIL_PAYLOAD, "digest": digest, "files": []},
                    "pointer": {},
                    "reconciled_after_transport_error": False,
                }

            with patch("tools.amethystd.mithril_hot_swap.activate_staged_mithril", new=activate):
                result = asyncio.run(manager.rollback(profile="mc26.2-directvulkan"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["before_digest"], bad_digest)
            self.assertEqual(result["after_digest"], old_digest)
            self.assertEqual(result["transaction"]["status"], "passed")
            run_call = [params for method, params in daemon.calls if method == "run_smoke"][-1]
            self.assertEqual(run_call["target"], "RENDERER_READY")
            self.assertTrue(run_call["require_dynamic_library_load"])
            host = store.load_candidate(MITHRIL_PAYLOAD)
            self.assertEqual(host["digest"], old_digest)
            self.assertEqual(host["source"], "reactivated_existing_stage")

    def test_explicit_bundled_rollback_clears_active_pointer_then_revalidates(self) -> None:
        active_digest = "c" * 64
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            daemon = RollbackDaemon(active_digest)
            manager = MithrilHotSwapManager(store, daemon, RollbackLabClient(daemon))  # type: ignore[arg-type]
            result = asyncio.run(
                manager.rollback(
                    digest="bundled",
                    profile="mc26.2-directvulkan",
                )
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["after_digest"], "bundled")
            self.assertIn(("clear_payload", {"name": MITHRIL_PAYLOAD}), daemon.calls)
            self.assertEqual(result["transaction"]["status"], "passed")

    def test_rollback_rejects_unchanged_process_generation(self) -> None:
        active_digest = "d" * 64
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            daemon = RollbackDaemon(active_digest, process_generation="old-process")
            manager = MithrilHotSwapManager(store, daemon, RollbackLabClient(daemon))  # type: ignore[arg-type]
            result = asyncio.run(
                manager.rollback(
                    digest="bundled",
                    profile="mc26.2-directvulkan",
                )
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["smoke"]["failure"], "JIT_VERIFICATION_FAILURE")
            self.assertEqual(result["transaction"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
