from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from tools.amethystd.mithril_candidate import (
    MITHRIL_LIBRARY,
    REQUIRED_EGL_EXPORTS,
    REQUIRED_GL_EXPORTS,
    MithrilCandidateError,
    inspect_mithril_candidate,
)


class FakeToolchain:
    def __init__(self) -> None:
        self.archs = "arm64\n"
        self.build = "Load command 1\n      cmd LC_BUILD_VERSION\n platform IOS\n    minos 16.0\n"
        self.install_name = f"candidate:\n@rpath/{MITHRIL_LIBRARY}\n"
        self.dependencies = (
            "candidate:\n"
            f"\t@rpath/{MITHRIL_LIBRARY} (compatibility version 0.0.0, current version 0.0.0)\n"
            "\t/System/Library/Frameworks/Metal.framework/Metal (compatibility version 1.0.0, current version 1.0.0)\n"
            "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
        )
        required = sorted(REQUIRED_EGL_EXPORTS | REQUIRED_GL_EXPORTS)
        self.exports = "".join(f"_{symbol}\n" for symbol in required)
        self.calls: list[list[str]] = []

    @staticmethod
    def find(name: str) -> str | None:
        return f"/usr/bin/{name}"

    def run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        self.calls.append(argv)
        tool = Path(argv[0]).name
        if tool == "lipo":
            stdout = self.archs
        elif tool == "vtool":
            stdout = self.build
        elif tool == "otool" and "-D" in argv:
            stdout = self.install_name
        elif tool == "otool" and "-L" in argv:
            stdout = self.dependencies
        elif tool == "nm":
            stdout = self.exports
        else:
            return subprocess.CompletedProcess(argv, 1, "", "unexpected command")
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class MithrilCandidateTests(unittest.TestCase):
    def candidate(self, root: str) -> Path:
        path = Path(root) / MITHRIL_LIBRARY
        path.write_bytes(b"fixture")
        return path

    def test_valid_ios_arm64_directmetal_candidate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toolchain = FakeToolchain()
            result = inspect_mithril_candidate(
                self.candidate(tmp), runner=toolchain.run, tool_finder=toolchain.find
            )
        self.assertEqual(result["architectures"], ["arm64"])
        self.assertEqual(result["platform"], "IOS")
        self.assertEqual(result["minos"], "16.0")
        self.assertEqual(result["install_name"], f"@rpath/{MITHRIL_LIBRARY}")
        self.assertTrue(result["vulkan_free_dynamic_dependencies"])
        self.assertTrue(result["required_exports_verified"])
        self.assertEqual(
            result["required_export_count"],
            len(REQUIRED_EGL_EXPORTS | REQUIRED_GL_EXPORTS),
        )

    def test_macos_candidate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toolchain = FakeToolchain()
            toolchain.build = "Load command 1\n platform MACOS\n minos 15.0\n"
            with self.assertRaisesRegex(MithrilCandidateError, "must target iPhoneOS"):
                inspect_mithril_candidate(
                    self.candidate(tmp), runner=toolchain.run, tool_finder=toolchain.find
                )

    def test_fat_or_non_arm64_candidate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toolchain = FakeToolchain()
            toolchain.archs = "x86_64 arm64\n"
            with self.assertRaisesRegex(MithrilCandidateError, "thin arm64"):
                inspect_mithril_candidate(
                    self.candidate(tmp), runner=toolchain.run, tool_finder=toolchain.find
                )

    def test_wrong_install_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toolchain = FakeToolchain()
            toolchain.install_name = "candidate:\n@rpath/libwrong.dylib\n"
            with self.assertRaisesRegex(MithrilCandidateError, "wrong install name"):
                inspect_mithril_candidate(
                    self.candidate(tmp), runner=toolchain.run, tool_finder=toolchain.find
                )

    def test_vulkan_or_moltenvk_dependency_is_rejected(self) -> None:
        for dependency in (
            "@rpath/libvulkan.1.dylib",
            "@rpath/MoltenVK.framework/MoltenVK",
        ):
            with self.subTest(dependency=dependency), tempfile.TemporaryDirectory() as tmp:
                toolchain = FakeToolchain()
                toolchain.dependencies += f"\t{dependency} (compatibility version 1.0.0, current version 1.0.0)\n"
                with self.assertRaisesRegex(MithrilCandidateError, "Vulkan-free"):
                    inspect_mithril_candidate(
                        self.candidate(tmp),
                        runner=toolchain.run,
                        tool_finder=toolchain.find,
                    )

    def test_missing_bridge_or_consumer_export_is_rejected(self) -> None:
        for missing in ("eglSwapBuffers", "glCompileShader", "glDrawElements"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                toolchain = FakeToolchain()
                toolchain.exports = toolchain.exports.replace(f"_{missing}\n", "")
                with self.assertRaisesRegex(MithrilCandidateError, missing):
                    inspect_mithril_candidate(
                        self.candidate(tmp),
                        runner=toolchain.run,
                        tool_finder=toolchain.find,
                    )

    def test_missing_host_tool_fails_closed_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toolchain = FakeToolchain()

            def finder(name: str) -> str | None:
                return None if name == "vtool" else FakeToolchain.find(name)

            with self.assertRaisesRegex(MithrilCandidateError, "vtool"):
                inspect_mithril_candidate(
                    self.candidate(tmp), runner=toolchain.run, tool_finder=finder
                )
            self.assertEqual(toolchain.calls, [])

    def test_wrong_filename_is_rejected_before_tooling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.dylib"
            path.write_bytes(b"fixture")
            toolchain = FakeToolchain()
            with self.assertRaisesRegex(MithrilCandidateError, MITHRIL_LIBRARY):
                inspect_mithril_candidate(
                    path, runner=toolchain.run, tool_finder=toolchain.find
                )
            self.assertEqual(toolchain.calls, [])


if __name__ == "__main__":
    unittest.main()
