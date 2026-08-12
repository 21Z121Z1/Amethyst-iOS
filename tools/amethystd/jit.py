from __future__ import annotations

import errno
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
    attempt: int

    def to_dict(self) -> dict:
        return {
            "exec_ready": self.exec_ready,
            "dynamic_library_load_ready": self.dynamic_library_load_ready,
            "port": self.port,
            "processor_log": self.processor_log,
            "debugserver_log": self.debugserver_log,
            "attempt": self.attempt,
        }


class JITSession:
    """Own debugserver forwarding and an external UniversalJIT26 processor for one process generation."""

    STALE_MARKERS = ("E96",)

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

    @staticmethod
    def _contains_any(path: Path, markers: tuple[str, ...]) -> bool:
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        return any(marker in text for marker in markers)

    @staticmethod
    def _port_is_claimed(port: int) -> bool:
        """Check listener ownership without opening a connection that could consume debugserver's only client."""
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    return True
                raise
        return False

    def _wait_listener(self, port: int, deadline: float) -> None:
        while monotonic() < deadline:
            if self.debugserver and self.debugserver.poll() is not None:
                raise RuntimeError(f"debugserver exited early with {self.debugserver.returncode}")
            if self._port_is_claimed(port):
                return
            sleep(0.05)
        raise TimeoutError("debugserver local forwarding did not claim its local port")

    def _stop_children(self, *, reset_identity: bool) -> None:
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
        if reset_identity:
            self.identity = None
        for handle in self._handles:
            handle.close()
        self._handles.clear()

    def _attempt(
        self,
        *,
        pid: int,
        process_generation: str,
        run_id: str,
        processor_template: str,
        require_dynamic_library_load: bool,
        deadline: float,
        attempt: int,
    ) -> JITEvidence:
        prefix = self.device.pmd3_prefix()
        if not prefix:
            raise RuntimeError("pymobiledevice3 command is unavailable")
        if not self.device.udid:
            raise RuntimeError("device UDID is required for JIT")

        port = self._free_port()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        debug_log = self.artifact_dir / f"debugserver-attempt-{attempt}.log"
        processor_log = self.artifact_dir / f"jit-processor-attempt-{attempt}.log"
        debug_handle = debug_log.open("w", encoding="utf-8")
        processor_handle = processor_log.open("w", encoding="utf-8")
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
        self._wait_listener(port, deadline)

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
            marker
            for marker in os.environ.get("AMETHYST_JIT_EXEC_MARKERS", "Got JIT mapping,mapping at RW=").split(",")
            if marker
        )
        dyld_markers = tuple(
            marker
            for marker in os.environ.get("AMETHYST_JIT_DYLD_MARKERS", "DyldLVBypass hooks succeeded").split(",")
            if marker
        )
        while monotonic() < deadline:
            debug_handle.flush()
            processor_handle.flush()
            if self._contains_any(debug_log, self.STALE_MARKERS) or self._contains_any(processor_log, self.STALE_MARKERS):
                raise StaleDebugserverError(f"stale debugserver signature observed on attempt {attempt}")
            exec_ready = self._contains_all(processor_log, exec_markers)
            dyld_ready = self._contains_all(processor_log, dyld_markers)
            if exec_ready and (dyld_ready or not require_dynamic_library_load):
                return JITEvidence(exec_ready, dyld_ready, port, str(processor_log), str(debug_log), attempt)
            if self.processor.poll() is not None:
                break
            sleep(0.1)

        debug_handle.flush()
        processor_handle.flush()
        if self._contains_any(debug_log, self.STALE_MARKERS) or self._contains_any(processor_log, self.STALE_MARKERS):
            raise StaleDebugserverError(f"stale debugserver signature observed on attempt {attempt}")
        exec_ready = self._contains_all(processor_log, exec_markers)
        dyld_ready = self._contains_all(processor_log, dyld_markers)
        if not exec_ready:
            raise JITVerificationError(f"UniversalJIT26 mapping proof not observed; see {processor_log}")
        if require_dynamic_library_load and not dyld_ready:
            raise DyldVerificationError(f"persistent-attached Dyld bypass proof not observed; see {processor_log}")
        return JITEvidence(exec_ready, dyld_ready, port, str(processor_log), str(debug_log), attempt)

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

        max_attempts = max(1, min(int(os.environ.get("AMETHYST_JIT_MAX_ATTEMPTS", "3")), 5))
        deadline = monotonic() + timeout
        last_stale: StaleDebugserverError | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self._attempt(
                    pid=pid,
                    process_generation=process_generation,
                    run_id=run_id,
                    processor_template=processor_template,
                    require_dynamic_library_load=require_dynamic_library_load,
                    deadline=deadline,
                    attempt=attempt,
                )
            except StaleDebugserverError as exc:
                last_stale = exc
                self._stop_children(reset_identity=False)
                if attempt >= max_attempts or monotonic() >= deadline:
                    raise
                sleep(min(0.15 * attempt, 0.5))
        raise last_stale or JITVerificationError("JIT session exhausted without evidence")

    def close(self) -> None:
        self._stop_children(reset_identity=True)


class JITProcessorUnconfigured(RuntimeError):
    pass


class StaleDebugserverError(RuntimeError):
    pass


class JITVerificationError(RuntimeError):
    pass


class DyldVerificationError(RuntimeError):
    pass
