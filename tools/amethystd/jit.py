from __future__ import annotations

import os
import shlex
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import TextIO

from .device import DeviceController


@dataclass
class JITEvidence:
    exec_ready: bool
    dynamic_library_load_ready: bool
    port: int
    processor_log: str
    debugserver_log: str

    def to_dict(self) -> dict:
        return {
            "exec_ready": self.exec_ready,
            "dynamic_library_load_ready": self.dynamic_library_load_ready,
            "port": self.port,
            "processor_log": self.processor_log,
            "debugserver_log": self.debugserver_log,
        }


class JITSession:
    """Own debugserver forwarding and an external UniversalJIT26 processor for one process generation."""

    def __init__(self, device: DeviceController, artifact_dir: Path) -> None:
        self.device = device
        self.artifact_dir = artifact_dir
        self.debugserver: subprocess.Popen[str] | None = None
        self.processor: subprocess.Popen[str] | None = None
        self._handles: list[TextIO] = []
        self.identity: tuple[int, str] | None = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _contains_all(path: Path, markers: tuple[str, ...]) -> bool:
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        return all(marker in text for marker in markers)

    def _wait_port(self, port: int, deadline: float) -> None:
        while monotonic() < deadline:
            if self.debugserver and self.debugserver.poll() is not None:
                raise RuntimeError(f"debugserver exited early with {self.debugserver.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                sleep(0.05)
        raise TimeoutError("debugserver local forwarding did not become reachable")

    def ensure(
        self,
        *,
        pid: int,
        process_generation: str,
        run_id: str,
        require_dynamic_library_load: bool,
        timeout: float = 45,
    ) -> JITEvidence:
        identity = (pid, process_generation)
        if self.identity and self.identity != identity:
            self.close()
        self.identity = identity

        processor_template = os.environ.get("AMETHYST_JIT_PROCESSOR")
        if not processor_template:
            raise JITProcessorUnconfigured(
                "AMETHYST_JIT_PROCESSOR is not configured; the repository does not contain a verified "
                "host UniversalJIT26 breakpoint processor"
            )
        prefix = self.device.pmd3_prefix()
        if not prefix:
            raise RuntimeError("pymobiledevice3 command is unavailable")
        if not self.device.udid:
            raise RuntimeError("device UDID is required for JIT")

        port = self._free_port()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        debug_log = self.artifact_dir / "debugserver.log"
        processor_log = self.artifact_dir / "jit-processor.log"
        debug_handle = debug_log.open("a", encoding="utf-8")
        processor_handle = processor_log.open("a", encoding="utf-8")
        self._handles.extend([debug_handle, processor_handle])

        env = os.environ.copy()
        env["PYMOBILEDEVICE3_UDID"] = self.device.udid
        debug_command = [*prefix, "developer", "debugserver", "start-server", "--local-port", str(port)]
        self.debugserver = subprocess.Popen(
            debug_command,
            text=True,
            stdout=debug_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
        deadline = monotonic() + timeout
        self._wait_port(port, deadline)

        values = {
            "host": "127.0.0.1",
            "port": str(port),
            "pid": str(pid),
            "run_id": run_id,
            "process_generation": process_generation,
        }
        processor_command = [part.format(**values) for part in shlex.split(processor_template)]
        self.processor = subprocess.Popen(
            processor_command,
            text=True,
            stdout=processor_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )

        exec_markers = tuple(
            m for m in os.environ.get("AMETHYST_JIT_EXEC_MARKERS", "Got JIT mapping,mapping at RW=").split(",") if m
        )
        dyld_markers = tuple(
            m for m in os.environ.get("AMETHYST_JIT_DYLD_MARKERS", "DyldLVBypass hooks succeeded").split(",") if m
        )
        exec_ready = False
        dyld_ready = False
        while monotonic() < deadline:
            if self.processor.poll() is not None:
                break
            exec_ready = self._contains_all(processor_log, exec_markers)
            dyld_ready = self._contains_all(processor_log, dyld_markers)
            if exec_ready and (dyld_ready or not require_dynamic_library_load):
                return JITEvidence(exec_ready, dyld_ready, port, str(processor_log), str(debug_log))
            sleep(0.1)

        exec_ready = self._contains_all(processor_log, exec_markers)
        dyld_ready = self._contains_all(processor_log, dyld_markers)
        if not exec_ready:
            raise JITVerificationError(f"UniversalJIT26 mapping proof not observed; see {processor_log}")
        if require_dynamic_library_load and not dyld_ready:
            raise DyldVerificationError(f"persistent-attached Dyld bypass proof not observed; see {processor_log}")
        return JITEvidence(exec_ready, dyld_ready, port, str(processor_log), str(debug_log))

    def close(self) -> None:
        for process in (self.processor, self.debugserver):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        self.processor = None
        self.debugserver = None
        self.identity = None
        for handle in self._handles:
            handle.close()
        self._handles.clear()


class JITProcessorUnconfigured(RuntimeError):
    pass


class JITVerificationError(RuntimeError):
    pass


class DyldVerificationError(RuntimeError):
    pass
