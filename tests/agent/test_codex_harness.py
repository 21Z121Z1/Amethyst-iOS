from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.amethystctl import glfw_key
from tools.amethystd.agent_output import bounded_result
from tools.amethystd.container_io import AgentContainerClient, _STREAM_CHUNK_BYTES
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
            self.assertLess(len(json.dumps(compact).encode()), 16_000)


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
        # arm64-style little-endian 64-bit Mach-O header prefix.
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
