from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tools.amethystd.container_io import AgentContainerClient, _is_transient_transport_error
from tools.amethystd.device import CommandResult, DeviceController
from tools.amethystd.store import StateStore


class FreshProcessContractTests(unittest.TestCase):
    def test_device_smoke_launch_terminates_existing_host_by_default(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.argv: list[str] | None = None

            def run(self, argv, **_kwargs):
                self.argv = list(argv)
                return CommandResult(list(argv), 0, "", "")

        runner = FakeRunner()
        controller = DeviceController("device-1", runner)
        with patch("tools.amethystd.device.shutil.which", return_value="/usr/bin/xcrun"):
            result = controller.launch_app("org.example.AgentDebug")
        self.assertTrue(result.ok)
        self.assertEqual(
            runner.argv,
            [
                "xcrun",
                "devicectl",
                "device",
                "process",
                "launch",
                "--terminate-existing",
                "--device",
                "device-1",
                "org.example.AgentDebug",
            ],
        )

    def test_non_game_diagnostic_can_explicitly_reuse_host(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.argv: list[str] | None = None

            def run(self, argv, **_kwargs):
                self.argv = list(argv)
                return CommandResult(list(argv), 0, "", "")

        runner = FakeRunner()
        controller = DeviceController("device-1", runner)
        with patch("tools.amethystd.device.shutil.which", return_value="/usr/bin/xcrun"):
            result = controller.launch_app("org.example.AgentDebug", terminate_existing=False)
        self.assertTrue(result.ok)
        self.assertNotIn("--terminate-existing", runner.argv or [])


class TransportRecoveryTests(unittest.TestCase):
    def test_known_network_house_arrest_errors_are_transient(self) -> None:
        for detail in (
            "Separator is not found in incoming packet",
            "SSL record layer failure",
            "BadDevError: device temporarily disappeared",
            "DeviceNotFoundError: device not found",
            "Connection reset by peer",
        ):
            self.assertTrue(_is_transient_transport_error(RuntimeError(detail)), detail)
        self.assertFalse(_is_transient_transport_error(ValueError("payload manifest failed identity validation")))

    def test_read_reopens_afc_after_one_transient_failure(self) -> None:
        class Missing(Exception):
            pass

        class FakeAfc:
            async def get_file_contents(self, _path: str) -> bytes:
                return b"ok"

        attempts = 0
        client = AgentContainerClient("device-1", "org.example.AgentDebug")

        @asynccontextmanager
        async def flaky_afc():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("Separator is not found in incoming packet")
            yield FakeAfc()

        client._afc = flaky_afc  # type: ignore[method-assign]
        fake_errors = type("Errors", (), {"AfcFileNotFoundError": Missing})
        with patch.dict("sys.modules", {"pymobiledevice3.exceptions": fake_errors}):
            with patch("tools.amethystd.container_io.asyncio.sleep", new_callable=AsyncMock):
                data = asyncio.run(client._read_direct_optional("latestlog.txt"))
        self.assertEqual(data, b"ok")
        self.assertEqual(attempts, 2)


class RetryIsolationTests(unittest.TestCase):
    def test_infrastructure_failures_do_not_poison_candidate_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            signature = store.attempt_signature(
                candidate_digest="abc",
                profile="directmetal-26.2",
                target="MENU_READY",
                require_dynamic_library_load=True,
            )
            store.record_failure(signature, failure="JIT_ATTACH_FAILURE", fingerprint="infra")
            store.record_failure(signature, failure="JIT_ATTACH_FAILURE", fingerprint="infra")
            status = store.retry_status(signature)
            self.assertFalse(status["blocked"])
            self.assertEqual(status["consecutive_same_failure"], 0)

            store.record_failure(signature, failure="MC_READY_TIMEOUT", fingerprint="semantic")
            store.record_failure(signature, failure="MC_READY_TIMEOUT", fingerprint="semantic")
            self.assertTrue(store.retry_status(signature)["blocked"])


class ConsumerIdentityContractTests(unittest.TestCase):
    def test_hot_renderer_is_prebound_before_jli_and_native_bridge_is_not_semantic_ready(self) -> None:
        root = Path(__file__).resolve().parents[2]
        launcher = (root / "Natives" / "JavaLauncher.m").read_text(encoding="utf-8")
        bridge = (root / "Natives" / "egl_bridge.m").read_text(encoding="utf-8")
        self.assertIn("AgentPayloadResolveActiveFile", launcher)
        self.assertIn("JavaLauncherOpenGLLibraryArgument(glLibName", launcher)
        self.assertIn("-Dorg.lwjgl.opengl.libname=%@", launcher)
        self.assertLess(
            launcher.index("JavaLauncherOpenGLLibraryArgument(glLibName"),
            launcher.index('pJLI_Launch = (JLI_Launch_func *)dlsym'),
        )
        self.assertIn('AgentControlEmitActiveEvent(@"renderer_bridge_ready"', bridge)
        self.assertNotIn('AgentControlEmitActiveEvent(@"renderer_ready"', bridge)

    def test_mc262_probe_proves_actual_lwjgl_function_provider(self) -> None:
        root = Path(__file__).resolve().parents[2]
        probe = (
            root
            / "lab/minecraft-probe/mc26.2/src/client/java/org/angelauramc/amethyst/lab/mc262/Minecraft262Probe.java"
        ).read_text(encoding="utf-8")
        self.assertIn("GL.getFunctionProvider()", probe)
        self.assertIn("provider instanceof SharedLibrary", probe)
        self.assertIn('provider.getFunctionAddress("glCompileShader")', probe)
        self.assertIn('provider.getFunctionAddress("glDrawElements")', probe)
        self.assertIn('emit("renderer_ready", fields)', probe)
        self.assertIn("lwjgl_function_provider_does_not_match_requested_staged_image", probe)


if __name__ == "__main__":
    unittest.main()
