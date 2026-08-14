from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from tools.amethystd.container_io import AgentContainerClient, documents_path, safe_component
from tools.amethystd.device import DeviceController
from tools.amethystd.jit import JITSession
from tools.amethystd.model import FailureClass, RunState, Stage
from tools.amethystd.runtime_contract import verify_contract
from tools.amethystd.store import StateStore
from tools.amethystd.supervisor import Supervisor
from tools.amethystd.universal_jit26_processor import Remote, reg_hex


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
    def test_minecraft_262_build_contract_uses_jdk25_not_jdk8(self) -> None:
        root = Path(__file__).resolve().parents[2]
        makefile = (root / "Makefile").read_text(encoding="utf-8")
        workflow = (root / ".github" / "workflows" / "agent-harness.yml").read_text(encoding="utf-8")
        native_job = workflow.split("  native-build:", 1)[1]
        self.assertIn("java_home -v 25", makefile)
        self.assertIn("BOOTJDK_VERSION", makefile)
        self.assertIn("Minecraft 26.2 build requires JDK 25", makefile)
        self.assertIn("java-version: '25'", native_job)
        self.assertNotIn("java-version: '8'", native_job)

    def test_renderer_provenance_contract_uses_provider_handle_not_global_lookup(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "Natives" / "egl_bridge.m").read_text(encoding="utf-8")
        self.assertIn("dlsym(rendererHandle, symbol.UTF8String)", source)
        self.assertIn('@"symbol_images": providerImages', source)
        self.assertIn('@"default_symbol_images": defaultImages', source)
        self.assertIn("RTLD_DEFAULT is only a", source)

    def test_safe_components_reject_paths(self) -> None:
        for bad in ("../x", "a/b", "", "a b"):
            with self.assertRaises(ValueError):
                safe_component(bad, "id")
        self.assertEqual(safe_component("run_ABC-123", "id"), "run_ABC-123")

    def test_documents_path_is_rooted_and_rejects_escape(self) -> None:
        self.assertEqual(documents_path("agent-requests/a.json"), "/Documents/agent-requests/a.json")
        for bad in ("../x", "/absolute"):
            with self.assertRaises(ValueError):
                documents_path(bad)

    def test_jsonl_parser_ignores_blank_lines(self) -> None:
        rows = AgentContainerClient._jsonl(b'{"event":"one"}\n\n{"event":"two"}\n')
        self.assertEqual([row["event"] for row in rows], ["one", "two"])

    def test_listener_probe_does_not_connect(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            self.assertTrue(JITSession._port_is_claimed(port))
        self.assertFalse(JITSession._port_is_claimed(port))

    def test_bundled_processor_is_default_and_parameterized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AMETHYST_JIT_PROCESSOR", None)
            session = JITSession(DeviceController("fake-udid"), Path(tmp))
            command = session._processor_command(12345, 42, "run-1", "gen-1", "session-1")
            self.assertTrue(any(part.endswith("universal_jit26_processor.py") for part in command))
            self.assertIn("12345", command)
            self.assertIn("42", command)

    def test_rsp_frame_has_valid_checksum(self) -> None:
        frame = Remote.frame("QStartNoAckMode")
        self.assertTrue(frame.startswith(b"$QStartNoAckMode#"))
        payload, checksum = frame[1:].split(b"#", 1)
        self.assertEqual(int(checksum, 16), sum(payload) & 0xFF)

    def test_universal_sentinel_register_encoding_matches_rollout(self) -> None:
        self.assertEqual(reg_hex(0x690000E0), "e000006900000000")

    def test_new_log_slice_uses_only_post_launch_bytes_and_handles_rotation(self) -> None:
        before = b"old\n"
        after = before + b"[JIT26] Got JIT mapping\n"
        self.assertEqual(Supervisor._new_log_slice(after, len(before)), "[JIT26] Got JIT mapping\n")
        rotated = b"[JIT26] mapping at RW=0x1 RX=0x2\n"
        self.assertEqual(Supervisor._new_log_slice(rotated, len(after) + 100), rotated.decode())

    def test_event_identity_survives_sequence_reset(self) -> None:
        first = {"process_generation": "gen-a", "seq": 1}
        second = {"process_generation": "gen-b", "seq": 1}
        self.assertNotEqual(Supervisor.event_key(first), Supervisor.event_key(second))
        explicit = {"event_id": "evt-1", "process_generation": "gen-a", "seq": 1}
        self.assertEqual(Supervisor.event_key(explicit), ("event_id", "evt-1"))

    def test_runtime_contract_catches_missing_inner_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar = root / "agent.jar"
            with ZipFile(jar, "w") as archive:
                archive.writestr("example/Agent.class", b"outer")
            manifest = root / "contract.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "archives": [
                            {
                                "path": "agent.jar",
                                "required_entries": ["example/Agent.class", "example/Agent$1.class"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = verify_contract(manifest)
            self.assertFalse(result["ok"])
            self.assertEqual(result["failures"][0]["reason"], "archive_entries_missing")
            with ZipFile(jar, "a") as archive:
                archive.writestr("example/Agent$1.class", b"inner")
            self.assertTrue(verify_contract(manifest)["ok"])


class SessionGenerationTests(unittest.TestCase):
    def test_new_game_session_invalidates_jit_and_semantic_evidence_without_host_restart(self) -> None:
        state = RunState(run_id="run-session", bundle_id="test")
        state.observe_process(10, "host-a")
        state.begin_game_session("session-a")
        state.transition(Stage.WORLD_READY)
        state.mark_jit(
            exec_ready=True,
            dynamic_library_load_ready=True,
            evidence={"renderer_ready": True, "world_ready": True, "menu_ready": True},
        )
        state.begin_game_session("session-b")
        self.assertEqual(state.pid, 10)
        self.assertEqual(state.process_generation, "host-a")
        self.assertEqual(state.game_session_generation, "session-b")
        self.assertFalse(state.jit_exec_ready)
        self.assertFalse(state.dynamic_library_load_ready)
        self.assertNotIn("renderer_ready", state.evidence)
        self.assertNotIn("world_ready", state.evidence)
        self.assertNotIn("menu_ready", state.evidence)

    def test_end_game_session_invalidates_session_scoped_proof(self) -> None:
        state = RunState(run_id="run-stop", bundle_id="test")
        state.observe_process(10, "host-a")
        state.begin_game_session("session-a")
        state.transition(Stage.WORLD_READY, world_ready=True)
        state.end_game_session()
        self.assertIsNone(state.game_session_generation)
        self.assertEqual(state.stage, Stage.AGENT_READY)
        self.assertNotIn("world_ready", state.evidence)


class RetryGuardTests(unittest.TestCase):
    def test_identical_failure_is_blocked_after_two_occurrences_and_reset_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            signature = store.attempt_signature(
                candidate_digest="abc",
                profile="DirectVulkan",
                target="WORLD_READY",
                require_dynamic_library_load=True,
            )
            self.assertFalse(store.retry_status(signature)["blocked"])
            store.record_failure(signature, failure="MC_READY_TIMEOUT", fingerprint="same")
            self.assertFalse(store.retry_status(signature)["blocked"])
            store.record_failure(signature, failure="MC_READY_TIMEOUT", fingerprint="same")
            self.assertTrue(store.retry_status(signature)["blocked"])
            store.reset_retry_guard("observed launcher recovery")
            self.assertFalse(store.retry_status(signature)["blocked"])

    def test_candidate_change_changes_attempt_signature(self) -> None:
        one = StateStore.attempt_signature(
            candidate_digest="aaa", profile="DirectVulkan", target="WORLD_READY", require_dynamic_library_load=True
        )
        two = StateStore.attempt_signature(
            candidate_digest="bbb", profile="DirectVulkan", target="WORLD_READY", require_dynamic_library_load=True
        )
        self.assertNotEqual(one, two)


class RuntimePathTests(unittest.TestCase):
    def test_socket_path_stays_short_when_persistent_root_is_long(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            long_root = Path(tmp) / ("very-long-component-" * 8)
            store = StateStore(long_root)
            self.assertNotEqual(store.socket_path.parent, store.root)
            self.assertLess(len(str(store.socket_path).encode()), 100)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(store.socket_path))
            store.socket_path.unlink(missing_ok=True)


class CandidateStoreTests(unittest.TestCase):
    def test_candidate_manifest_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            value = {"name": "mithril", "active": True, "digest": "abc", "files": []}
            store.save_candidate("mithril", value)
            self.assertEqual(store.load_candidate("mithril"), value)
            with self.assertRaises(ValueError):
                store.save_candidate("../escape", value)

class SupervisorPreDeviceTests(unittest.TestCase):
    def test_smoke_reaches_structured_device_preflight_without_stale_retry_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = Supervisor(StateStore(tmp), device_udid="fake", bundle_id="test")
            doctor = {
                "device": {
                    "pymobiledevice3": {"available": False, "python_api_available": False, "ok": False},
                    "devicectl": {"available": False},
                }
            }
            with patch.object(supervisor, "doctor", return_value=doctor):
                result = asyncio.run(supervisor.run_smoke(profile="DirectVulkan", target="MENU_READY"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"]["failure"], FailureClass.DEVICE_NOT_FOUND.value)
            self.assertIn("host_retry_status", result["state"]["evidence"])

    def test_repeat_override_requires_reason_and_flag_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = Supervisor(StateStore(tmp), device_udid="fake", bundle_id="test")
            with self.assertRaises(ValueError):
                asyncio.run(supervisor.run_smoke(profile="DirectVulkan", allow_repeat_failure=True))
            with self.assertRaises(ValueError):
                asyncio.run(supervisor.run_smoke(profile="DirectVulkan", retry_reason="diagnostic"))


if __name__ == "__main__":
    unittest.main()
