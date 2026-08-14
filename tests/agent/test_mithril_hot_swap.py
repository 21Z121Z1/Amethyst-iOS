from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from tools.amethystd.container_io import documents_path
from tools.amethystd.mithril_hot_swap import (
    HotSwapLedger,
    MITHRIL_LIBRARY,
    MITHRIL_PAYLOAD,
    MithrilHotSwapManager,
    activate_staged_mithril,
    verify_staged_mithril,
)
from tools.amethystd.store import StateStore


class FakeAfc:
    def __init__(self, files: dict[str, bytes], *, fail_rename_after_commit: bool = False) -> None:
        self.files = files
        self.fail_rename_after_commit = fail_rename_after_commit

    async def makedirs(self, _path: str) -> None:
        return None

    async def set_file_contents(self, path: str, data: bytes) -> None:
        self.files[path] = bytes(data)

    async def rm(self, path: str) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    async def rename(self, source: str, destination: str) -> None:
        self.files[destination] = self.files.pop(source)
        if self.fail_rename_after_commit:
            raise RuntimeError("SSL record layer failure after rename commit")


class FakePayloadClient:
    def __init__(self, digest: str, data: bytes = b"mithril") -> None:
        self.digest = digest
        self.files: dict[str, bytes] = {}
        sha = hashlib.sha256(data).hexdigest()
        self.manifest = {
            "version": 1,
            "name": MITHRIL_PAYLOAD,
            "digest": digest,
            "files": [
                {
                    "path": MITHRIL_LIBRARY,
                    "size": len(data),
                    "sha256": sha,
                    "code_signature": {"verified": True, "cdhash": "fake"},
                }
            ],
        }
        stage = f"agent-payloads/.staging/{digest}"
        self.files[documents_path(f"{stage}/manifest.json")] = (json.dumps(self.manifest) + "\n").encode()
        self.files[documents_path(f"{stage}/{MITHRIL_LIBRARY}")] = data
        self.fail_rename_after_commit = False

    async def read_optional(self, relative: str) -> bytes | None:
        return self.files.get(documents_path(relative))

    @asynccontextmanager
    async def _afc(self):
        yield FakeAfc(self.files, fail_rename_after_commit=self.fail_rename_after_commit)

    async def inspect_payload(self, name: str) -> dict:
        pointer_raw = self.files.get(documents_path(f"agent-payloads/active/{name}.json"))
        if pointer_raw is None:
            return {"ok": True, "name": name, "active": False}
        pointer = json.loads(pointer_raw.decode())
        digest = pointer["digest"]
        manifest_raw = self.files[documents_path(f"agent-payloads/.staging/{digest}/manifest.json")]
        return {
            "ok": True,
            "name": name,
            "active": True,
            "pointer": pointer,
            "manifest": json.loads(manifest_raw.decode()),
        }


class StagedActivationTests(unittest.TestCase):
    def test_existing_signed_stage_is_verified_and_activated(self) -> None:
        digest = "a" * 64
        client = FakePayloadClient(digest)
        result = asyncio.run(activate_staged_mithril(client, digest))
        self.assertTrue(result["ok"])
        self.assertFalse(result["reconciled_after_transport_error"])
        observed = asyncio.run(client.inspect_payload(MITHRIL_PAYLOAD))
        self.assertTrue(observed["active"])
        self.assertEqual(observed["manifest"]["digest"], digest)

    def test_rename_error_after_commit_is_reconciled_without_replaying_write(self) -> None:
        digest = "b" * 64
        client = FakePayloadClient(digest)
        client.fail_rename_after_commit = True
        result = asyncio.run(activate_staged_mithril(client, digest))
        self.assertTrue(result["ok"])
        self.assertTrue(result["reconciled_after_transport_error"])
        self.assertEqual((asyncio.run(client.inspect_payload(MITHRIL_PAYLOAD)))["manifest"]["digest"], digest)

    def test_tampered_staged_bytes_are_rejected_before_pointer_switch(self) -> None:
        digest = "c" * 64
        client = FakePayloadClient(digest)
        client.files[documents_path(f"agent-payloads/.staging/{digest}/{MITHRIL_LIBRARY}")] = b"tampered"
        with self.assertRaisesRegex(RuntimeError, "SHA-256/size verification"):
            asyncio.run(verify_staged_mithril(client, digest))
        self.assertFalse(asyncio.run(client.inspect_payload(MITHRIL_PAYLOAD))["active"])

    def test_stage_without_verified_signature_provenance_is_rejected(self) -> None:
        digest = "d" * 64
        client = FakePayloadClient(digest)
        client.manifest["files"][0].pop("code_signature")
        client.files[documents_path(f"agent-payloads/.staging/{digest}/manifest.json")] = json.dumps(client.manifest).encode()
        with self.assertRaisesRegex(RuntimeError, "code-signature provenance"):
            asyncio.run(verify_staged_mithril(client, digest))


class FakeDaemon:
    def __init__(self, before_digest: str, after_digest: str, *, process_generation: str = "new-process") -> None:
        self.active_digest = before_digest
        self.after_digest = after_digest
        self.process_generation = process_generation
        self.calls: list[tuple[str, dict | None]] = []

    def payload(self) -> dict:
        if self.active_digest == "bundled":
            return {"ok": True, "name": MITHRIL_PAYLOAD, "device": {"ok": True, "active": False}, "host": None}
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
            "host": {"active": True, "digest": self.active_digest},
        }

    async def __call__(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params))
        if method == "status":
            return {"ok": True, "state": {"process_generation": "old-process"}}
        if method == "inspect_payload":
            return self.payload()
        if method == "stage_payload":
            self.active_digest = self.after_digest
            return {
                "ok": True,
                "manifest": {
                    "version": 1,
                    "name": MITHRIL_PAYLOAD,
                    "digest": self.after_digest,
                    "files": [
                        {
                            "path": MITHRIL_LIBRARY,
                            "size": 1,
                            "sha256": "e" * 64,
                            "code_signature": {"verified": True},
                        }
                    ],
                },
                "candidate": {"active": True, "digest": self.after_digest},
            }
        if method == "run_smoke":
            return {
                "ok": True,
                "state": {
                    "stage": "PASS",
                    "run_id": "run-1",
                    "game_session_generation": "session-1",
                    "process_generation": self.process_generation,
                    "candidate": {"active": self.active_digest != "bundled", "digest": self.active_digest},
                },
            }
        if method == "clear_payload":
            self.active_digest = "bundled"
            return {"ok": True, "active": False}
        raise AssertionError(method)


class FakeLabClient:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    async def read_lab_events(self, run_id: str, session: str) -> list[dict]:
        self.last = (run_id, session)
        loaded = (
            f"/private/var/mobile/Containers/Data/Application/APP/Documents/agent-payloads/.staging/{self.digest}/{MITHRIL_LIBRARY}"
            if self.digest != "bundled"
            else "/private/var/containers/Bundle/Application/APP/AngelAuraAmethyst.app/Frameworks/libmithril.dylib"
        )
        return [
            {
                "event": "renderer_ready",
                "provenance_ok": True,
                "candidate_digest": self.digest,
                "loaded_library_path": loaded,
                "glCompileShader": "4096",
                "glDrawElements": "8192",
            }
        ]


class SwapTransactionTests(unittest.TestCase):
    def test_swap_stages_cold_restarts_and_proves_actual_consumer(self) -> None:
        old_digest = "1" * 64
        new_digest = "2" * 64
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            binary = Path(tmp) / MITHRIL_LIBRARY
            binary.write_bytes(b"candidate")
            daemon = FakeDaemon(old_digest, new_digest)
            manager = MithrilHotSwapManager(store, daemon, FakeLabClient(new_digest))  # type: ignore[arg-type]
            result = asyncio.run(manager.swap(str(binary), profile="mc26.2-directvulkan"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["before_digest"], old_digest)
            self.assertEqual(result["after_digest"], new_digest)
            self.assertEqual(result["transaction"]["status"], "passed")
            self.assertEqual(result["transaction"]["process_generation"], "new-process")
            run_call = [params for method, params in daemon.calls if method == "run_smoke"][-1]
            self.assertTrue(run_call["require_dynamic_library_load"])
            self.assertEqual(run_call["candidate_name"], MITHRIL_PAYLOAD)
            self.assertEqual(run_call["target"], "RENDERER_READY")
            self.assertEqual(HotSwapLedger(store).load()["last_known_good_digest"], new_digest)

    def test_same_process_generation_fails_cold_restart_invariant(self) -> None:
        old_digest = "3" * 64
        new_digest = "4" * 64
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            binary = Path(tmp) / MITHRIL_LIBRARY
            binary.write_bytes(b"candidate")
            daemon = FakeDaemon(old_digest, new_digest, process_generation="old-process")
            manager = MithrilHotSwapManager(store, daemon, FakeLabClient(new_digest))  # type: ignore[arg-type]
            result = asyncio.run(manager.swap(str(binary), profile="mc26.2-directvulkan"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["smoke"]["failure"], "JIT_VERIFICATION_FAILURE")
            self.assertEqual(result["transaction"]["status"], "failed")

    def test_consumer_digest_mismatch_invalidates_an_otherwise_passing_smoke(self) -> None:
        old_digest = "5" * 64
        new_digest = "6" * 64
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            binary = Path(tmp) / MITHRIL_LIBRARY
            binary.write_bytes(b"candidate")
            daemon = FakeDaemon(old_digest, new_digest)
            manager = MithrilHotSwapManager(store, daemon, FakeLabClient("7" * 64))  # type: ignore[arg-type]
            with self.assertRaisesRegex(RuntimeError, "consumer provenance mismatch"):
                asyncio.run(manager.swap(str(binary), profile="mc26.2-directvulkan"))

    def test_ledger_default_rollback_target_tracks_transition_not_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            ledger = HotSwapLedger(store)
            old_digest = "8" * 64
            new_digest = "9" * 64
            ledger.append(
                {
                    "version": 1,
                    "id": "tx-1",
                    "operation": "swap",
                    "status": "failed",
                    "before_digest": old_digest,
                    "after_digest": new_digest,
                }
            )
            self.assertEqual(ledger.rollback_target(new_digest), old_digest)


class CodexEntryPointTests(unittest.TestCase):
    def test_amethystctl_dispatches_mithril_help_to_hot_swap_module(self) -> None:
        root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["AMETHYST_AGENT_PYTHON"] = sys.executable
        result = subprocess.run(
            [str(root / "tools/amethystctl"), "mithril", "--help"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Transactional Mithril dylib hot deployment", result.stdout)
        self.assertIn("swap", result.stdout)
        self.assertIn("rollback", result.stdout)


if __name__ == "__main__":
    unittest.main()
