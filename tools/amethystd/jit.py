from __future__ import annotations

import errno
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import TextIO

from .device import DeviceController


@dataclass
class JITAttachEvidence:
    port: int
    processor_log: str
    debugserver_log: str
    attempt: int

    def to_dict(self) -> dict:
        return {
            "port": self.port,
            "processor_log": self.processor_log,
            "debugserver_log": self.debugserver_log,
            "attempt": self.attempt,
        }


@dataclass
class JITHostEvidence:
    exec_ready: bool
    keep_attached: bool
    processor_log: str

    def to_dict(self) -> dict:
        return {
            "exec_ready": self.exec_ready,
            "keep_attached": self.keep_attached,
            "processor_log": self.processor_log,
        }


class JITSession:
    """Own debugserver forwarding and UniversalJIT26 processing for one app process generation.

    Attachment and JIT proof are deliberately separate.  The processor must be
    attached *before* Minecraft launch so it can catch the breakpoints emitted
    by `launchJVM`; executable mapping cannot be proven until after launch.
    """

    STALE_MARKERS = ("E96",)
    ATTACHED_MARKER = "AMETHYST_JIT_PROCESSOR_ATTACHED"
    EXEC_MARKER = "AMETHYST_JIT_HOST_EXEC_READY"
    KEEP_ATTACHED_MARKER = "AMETHYST_JIT_KEEP_ATTACHED value=true"

    def __init__(self, device: DeviceController, artifact_dir: Path) -> None:
        self.device = device
        self.artifact_dir = artifact_dir
        self.debugserver: subprocess.Popen[str] | None = None
        self.processor: subprocess.Popen[str] | None = None
        self._handles: list[TextIO] = []
        self.identity: tuple[int, str] | None = None
        self.processor_log: Path | None = None
        self.debugserver_log: Path | None = None
        self.port: int | None = None
        self.attempt: int = 0

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _text(path: Path | None) -> str:
        if path is None or not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def _contains_any(cls, path: Path | None, markers: tuple[str, ...]) -> bool:
        text = cls._text(path)
        return any(marker in text for marker in markers)

    @staticmethod
    def _port_is_claimed(port: int) -> bool:
        """Check listener ownership without consuming debugserver's only client."""
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

    def _processor_command(self, port: int, pid: int, run_id: str, process_generation: str) -> list[str]:
        configured = os.environ.get("AMETHYST_JIT_PROCESSOR")
        values = {
            "host": "127.0.0.1",
            "port": str(port),
            "pid": str(pid),
            "run_id": run_id,
            "process_generation": process_generation,
        }
        if configured:
            return [part.format(**values) for part in shlex.split(configured)]
        processor = Path(__file__).with_name("universal_jit26_processor.py")
        return [sys.executable, str(processor), "--host", "127.0.0.1", "--port", str(port), "--pid", str(pid)]

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

    def _start_attempt(
        self,
        *,
        pid: int,
        process_generation: str,
        run_id: str,
        deadline: float,
        attempt: int,
    ) -> JITAttachEvidence:
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
        debug_command = [
            *prefix,
            "developer",
            "debugserver",
            "start-server",
            "--userspace",
            "--local-port",
            str(port),
        ]
        self.debugserver = subprocess.Popen(
            debug_command,
            text=True,
            stdout=debug_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self._wait_listener(port, deadline)

        self.processor = subprocess.Popen(
            self._processor_command(port, pid, run_id, process_generation),
            text=True,
            stdout=processor_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.port = port
        self.processor_log = processor_log
        self.debugserver_log = debug_log
        self.attempt = attempt

        while monotonic() < deadline:
            debug_handle.flush()
            processor_handle.flush()
            if self._contains_any(debug_log, self.STALE_MARKERS) or self._contains_any(processor_log, self.STALE_MARKERS):
                raise StaleDebugserverError(f"stale debugserver signature observed on attempt {attempt}")
            if self.ATTACHED_MARKER in self._text(processor_log):
                return JITAttachEvidence(port, str(processor_log), str(debug_log), attempt)
            if self.processor.poll() is not None:
                raise JITAttachError(f"UniversalJIT26 processor exited before attach with {self.processor.returncode}")
            sleep(0.05)
        raise TimeoutError("UniversalJIT26 processor did not confirm attach before timeout")

    def start(
        self,
        *,
        pid: int,
        process_generation: str,
        run_id: str,
        timeout: float = 20,
    ) -> JITAttachEvidence:
        identity = (pid, process_generation)
        if self.identity and self.identity != identity:
            self.close()
        elif self.processor and self.processor.poll() is None and self.identity == identity:
            if self.ATTACHED_MARKER in self._text(self.processor_log):
                return JITAttachEvidence(self.port or 0, str(self.processor_log), str(self.debugserver_log), self.attempt)
        self.identity = identity

        max_attempts = max(1, min(int(os.environ.get("AMETHYST_JIT_MAX_ATTEMPTS", "3")), 5))
        deadline = monotonic() + timeout
        last_stale: StaleDebugserverError | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self._start_attempt(
                    pid=pid,
                    process_generation=process_generation,
                    run_id=run_id,
                    deadline=deadline,
                    attempt=attempt,
                )
            except StaleDebugserverError as exc:
                last_stale = exc
                self._stop_children(reset_identity=False)
                if attempt >= max_attempts or monotonic() >= deadline:
                    raise
                sleep(min(0.15 * attempt, 0.5))
        raise last_stale or JITAttachError("JIT attach exhausted without evidence")

    def wait_for_host_exec(self, *, require_keep_attached: bool, timeout: float) -> JITHostEvidence:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if self._contains_any(self.debugserver_log, self.STALE_MARKERS) or self._contains_any(self.processor_log, self.STALE_MARKERS):
                raise StaleDebugserverError("stale debugserver signature observed after launch")
            text = self._text(self.processor_log)
            exec_ready = self.EXEC_MARKER in text
            keep_attached = self.KEEP_ATTACHED_MARKER in text
            if exec_ready and (keep_attached or not require_keep_attached):
                return JITHostEvidence(exec_ready, keep_attached, str(self.processor_log))
            if self.processor is None or self.processor.poll() is not None:
                code = None if self.processor is None else self.processor.returncode
                raise JITVerificationError(f"UniversalJIT26 processor exited before executable proof (code={code})")
            sleep(0.1)
        raise JITVerificationError(f"UniversalJIT26 host executable proof not observed; see {self.processor_log}")

    def close(self) -> None:
        self._stop_children(reset_identity=True)


class JITAttachError(RuntimeError):
    pass


class StaleDebugserverError(RuntimeError):
    pass


class JITVerificationError(RuntimeError):
    pass


class DyldVerificationError(RuntimeError):
    pass
