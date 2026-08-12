from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        proc = subprocess.run(
            list(argv),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=merged,
            check=False,
        )
        return CommandResult(list(argv), proc.returncode, proc.stdout, proc.stderr)


class DeviceController:
    def __init__(self, udid: str | None, runner: CommandRunner | None = None) -> None:
        self.udid = udid
        self.runner = runner or CommandRunner()

    @staticmethod
    def pmd3_prefix() -> list[str]:
        configured = os.environ.get("AMETHYST_PMD3")
        if configured:
            return shlex.split(configured)
        direct = shutil.which("pymobiledevice3")
        if direct:
            return [direct]
        uvx = shutil.which("uvx")
        if uvx:
            return [uvx, "pymobiledevice3"]
        return []

    def pmd3_env(self) -> dict[str, str]:
        return {"PYMOBILEDEVICE3_UDID": self.udid} if self.udid else {}

    def doctor(self) -> dict:
        prefix = self.pmd3_prefix()
        result: dict = {
            "pymobiledevice3": {"available": bool(prefix), "command": prefix},
            "devicectl": {"available": bool(shutil.which("xcrun"))},
            "configured_udid": self.udid,
        }
        if prefix:
            probe = self.runner.run([*prefix, "usbmux", "list"], timeout=20, env=self.pmd3_env())
            result["pymobiledevice3"].update(
                {"ok": probe.ok, "stdout": probe.stdout, "stderr": probe.stderr, "returncode": probe.returncode}
            )
        return result

    def launch_app(self, bundle_id: str) -> CommandResult:
        if not self.udid:
            raise ValueError("AMETHYST_DEVICE_UDID/--device is required for a state-changing run")
        if not shutil.which("xcrun"):
            raise RuntimeError("xcrun is unavailable; run the harness on macOS with Xcode command-line tools")
        return self.runner.run(
            ["xcrun", "devicectl", "device", "process", "launch", "--device", self.udid, bundle_id],
            timeout=60,
        )

    def install_app(self, app_path: Path | str) -> CommandResult:
        if not self.udid:
            raise ValueError("AMETHYST_DEVICE_UDID/--device is required for installation")
        return self.runner.run(
            ["xcrun", "devicectl", "device", "install", "app", "--device", self.udid, str(app_path)],
            timeout=float(os.environ.get("AMETHYST_INSTALL_TIMEOUT", "300")),
        )

    def pull_crashes(self, target: Path) -> CommandResult:
        prefix = self.pmd3_prefix()
        if not prefix:
            raise RuntimeError("pymobiledevice3 is unavailable")
        target.mkdir(parents=True, exist_ok=True)
        return self.runner.run([*prefix, "crash", "pull", str(target)], timeout=120, env=self.pmd3_env())

    def screenshot(self, target: Path) -> CommandResult:
        prefix = self.pmd3_prefix()
        if not prefix:
            raise RuntimeError("pymobiledevice3 is unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        return self.runner.run(
            [*prefix, "developer", "dvt", "screenshot", str(target)], timeout=60, env=self.pmd3_env()
        )
