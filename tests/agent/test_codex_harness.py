from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.amethystctl import glfw_key
from tools.amethystd.agent_output import bounded_result
from tools.amethystd.container_io import AgentContainerClient, _STREAM_CHUNK_BYTES, _device_connection_type
from tools.amethystd.debug_package import AgentDebugBuildError, MH_EXECUTE, macho_filetypes, require_macho_executable
from tools.amethystd.frame_metrics import analyze_png, compare_png_frames
from tools.amethystd.model import FailureClass, failure_fingerprint


def write_rgba_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    if len(rgba) != width * height * 4:
        raise ValueError("rgba byte count does not match dimensions")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    scanlines = b"".join(b"\x00" + rgba[row * width * 4 : (row + 1) * width * 4] for row in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class OutputBudgetTests(unittest.TestCase):
    def test_oversized_output_is_persisted_and_compacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {
                "ok": False,
                "state": {
                    "run_id": "run-1",
                    "stage": "FAIL",
                    "failure": "JIT_ATTACH_FAILURE",
                    "failure_detail": "same failure " + "x" * 20_000,
                    "failure_fingerprint": "abc123",
                    "evidence": {f"key-{i}": i for i in range(200)},
                },
                "raw": "z" * 100_000,
            }
            compact = bounded_result(value, root, max_bytes=4096)
            self.assertTrue(compact["_output_truncated"])
            self.assertEqual(compact["state"]["failure_fingerprint"], "abc123")
            full = Path(compact["_full_output"])
            self.assertTrue(full.is_file())
            self.assertEqual(json.loads(full.read_text())["raw"], value["raw"])
            encoded = (json.dumps(compact, sort_keys=True, separators=(",", ":")) + "\n").encode()
            self.assertLessEqual(len(encoded), 4096)


class FailureFingerprintTests(unittest.TestCase):
    def test_volatile_ids_do_not_change_failure_fingerprint(self) -> None:
        first = failure_fingerprint(
            FailureClass.JIT_ATTACH_FAILURE,
            "pid 12345 at 0x7ffeeabc port 51001 generation 12345678-1234-1234-1234-123456789abc",
        )
        second = failure_fingerprint(
            FailureClass.JIT_ATTACH_FAILURE,
            "pid 99999 at 0xABCDEF port 62002 generation abcdefab-abcd-abcd-abcd-abcdefabcdef",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, failure_fingerprint(FailureClass.JIT_VERIFICATION_FAILURE, "pid 99999 at 0xABCDEF port 62002"))

    def test_semantic_error_codes_remain_distinct(self) -> None:
        first = failure_fingerprint(FailureClass.STALE_DEBUGSERVER, "debugserver rejected request E96 on pid 12345")
        second = failure_fingerprint(FailureClass.STALE_DEBUGSERVER, "debugserver rejected request E97 on pid 99999")
        self.assertNotEqual(first, second)


class ContainerStreamingTests(unittest.TestCase):
    def test_bounded_read_never_requests_more_than_chunk_budget(self) -> None:
        class FakeAfc:
            def __init__(self) -> None:
                self.requests: list[int] = []
                self.closed = False

            async def fopen(self, _path: str, _mode: str) -> int:
                return 7

            async def fread(self, _handle: int, size: int) -> bytes:
                self.requests.append(size)
                return b"x" * size

            async def fclose(self, _handle: int) -> None:
                self.closed = True

        afc = FakeAfc()
        size = _STREAM_CHUNK_BYTES * 2 + 123
        data = asyncio.run(AgentContainerClient._bounded_read(afc, "/Documents/latestlog.txt", size))
        self.assertEqual(len(data), size)
        self.assertEqual(afc.requests, [_STREAM_CHUNK_BYTES, _STREAM_CHUNK_BYTES, 123])
        self.assertTrue(afc.closed)


class DeviceTransportTests(unittest.TestCase):
    def test_connection_type_reaches_pymobiledevice3_and_is_fail_closed(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        @asynccontextmanager
        async def fake_context():
            yield object()

        with patch.dict(os.environ, {"AMETHYST_DEVICE_CONNECTION_TYPE": "Network"}):
            with patch("pymobiledevice3.lockdown.create_using_usbmux", new_callable=AsyncMock) as create_lockdown:
                with patch(
                    "pymobiledevice3.services.house_arrest.HouseArrestService.create",
                    new_callable=AsyncMock,
                ) as create_house_arrest:
                    create_lockdown.return_value = fake_context()
                    create_house_arrest.return_value = fake_context()
                    client = AgentContainerClient("udid", "bundle")

                    async def exercise() -> None:
                        async with client._afc():
                            pass

                    asyncio.run(exercise())
                    create_lockdown.assert_awaited_once_with(
                        serial="udid", autopair=False, connection_type="Network"
                    )
                    create_house_arrest.assert_awaited_once()
        with patch.dict(os.environ, {"AMETHYST_DEVICE_CONNECTION_TYPE": "WiFi"}):
            with self.assertRaisesRegex(ValueError, "must be USB or Network"):
                _device_connection_type()


class FrameMetricsTests(unittest.TestCase):
    def test_png_metrics_and_frame_delta_are_text_legible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.png"
            second = root / "second.png"
            write_rgba_png(first, 2, 1, bytes([0, 0, 0, 255, 255, 255, 255, 255]))
            write_rgba_png(second, 2, 1, bytes([0, 0, 0, 255, 128, 128, 128, 255]))
            metrics = analyze_png(first)
            self.assertTrue(metrics["supported"])
            self.assertEqual((metrics["width"], metrics["height"]), (2, 1))
            self.assertEqual(metrics["non_black_ratio"], 0.5)
            delta = compare_png_frames(first, second)
            self.assertTrue(delta["supported"])
            self.assertEqual(delta["changed_pixel_ratio"], 0.5)
            self.assertGreater(delta["mean_abs_rgb_delta_0_255"], 0)


class SemanticInputTests(unittest.TestCase):
    def test_glfw_key_names(self) -> None:
        self.assertEqual(glfw_key("W"), 87)
        self.assertEqual(glfw_key("escape"), 256)
        self.assertEqual(glfw_key("F3"), 292)


class AgentDebugShapeTests(unittest.TestCase):
    @staticmethod
    def write_macho(path: Path, filetype: int) -> None:
        path.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<III", 0x0100000C, 0, filetype) + b"\x00" * 16)

    def test_mh_execute_is_accepted_and_dylib_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "AngelAuraAmethyst"
            dylib = root / "libmithril.dylib"
            self.write_macho(executable, MH_EXECUTE)
            self.write_macho(dylib, 0x6)
            self.assertEqual(macho_filetypes(executable), {MH_EXECUTE})
            require_macho_executable(executable)
            with self.assertRaises(AgentDebugBuildError):
                require_macho_executable(dylib)


if __name__ == "__main__":
    unittest.main()

class PayloadLifecycleTests(unittest.TestCase):
    def test_payload_clear_is_idempotent_and_preserves_staging(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        class Missing(Exception):
            pass

        class FakeAfc:
            def __init__(self) -> None:
                digest = "a" * 64
                self.files = {
                    "/Documents/agent-payloads/active/mithril.json": json.dumps({
                        "name": "mithril",
                        "digest": digest,
                        "stage": f"agent-payloads/.staging/{digest}",
                    }).encode(),
                    f"/Documents/agent-payloads/.staging/{digest}/manifest.json": json.dumps({
                        "version": 1, "name": "mithril", "digest": digest, "files": []
                    }).encode(),
                }

            async def get_file_contents(self, path: str) -> bytes:
                if path not in self.files:
                    raise Missing(path)
                return self.files[path]

            async def rm(self, path: str) -> None:
                if path not in self.files:
                    raise Missing(path)
                del self.files[path]

        afc = FakeAfc()
        client = AgentContainerClient("udid", "bundle")

        @asynccontextmanager
        async def fake_afc():
            yield afc

        client._afc = fake_afc  # type: ignore[method-assign]
        fake_module = type("Errors", (), {"AfcFileNotFoundError": Missing})
        with patch.dict("sys.modules", {"pymobiledevice3.exceptions": fake_module}):
            first = asyncio.run(client.clear_payload("mithril"))
            second = asyncio.run(client.clear_payload("mithril"))
        self.assertTrue(first["cleared"])
        self.assertFalse(second["cleared"])
        self.assertTrue(first["staging_preserved"])
        self.assertIn(f"/Documents/agent-payloads/.staging/{'a' * 64}/manifest.json", afc.files)


    def test_payload_inspect_rejects_stage_digest_mismatch(self) -> None:
        from unittest.mock import AsyncMock, patch

        client = AgentContainerClient("udid", "bundle")
        digest = "a" * 64
        pointer = json.dumps({
            "name": "mithril",
            "digest": digest,
            "stage": "agent-payloads/.staging/" + "b" * 64,
        }).encode()
        with patch.object(client, "_read_direct_optional", new=AsyncMock(return_value=pointer)):
            with self.assertRaisesRegex(RuntimeError, "stage/digest mismatch"):
                asyncio.run(client.inspect_payload("mithril"))

    def test_payload_name_rejects_path_traversal(self) -> None:
        client = AgentContainerClient("udid", "bundle")
        with self.assertRaises(ValueError):
            asyncio.run(client.clear_payload("../mithril"))
