from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any


class Stage(str, Enum):
    IDLE = "IDLE"
    DEVICE_DISCOVERED = "DEVICE_DISCOVERED"
    DEVICE_PREFLIGHT_OK = "DEVICE_PREFLIGHT_OK"
    PAYLOAD_VERIFIED = "PAYLOAD_VERIFIED"
    APP_INSTALLED = "APP_INSTALLED"
    APP_LAUNCHED = "APP_LAUNCHED"
    AGENT_READY = "AGENT_READY"
    JIT_ATTACHING = "JIT_ATTACHING"
    JIT_HANDSHAKE_OK = "JIT_HANDSHAKE_OK"
    JIT_RX_MAPPING_OK = "JIT_RX_MAPPING_OK"
    DYLD_BYPASS_READY = "DYLD_BYPASS_READY"
    RUNTIME_READY = "RUNTIME_READY"
    PROFILE_READY = "PROFILE_READY"
    LAUNCH_ACCEPTED = "LAUNCH_ACCEPTED"
    JVM_STARTING = "JVM_STARTING"
    JVM_READY = "JVM_READY"
    MC_BOOTSTRAP = "MC_BOOTSTRAP"
    RENDERER_LOADING = "RENDERER_LOADING"
    RENDERER_READY = "RENDERER_READY"
    MENU_READY = "MENU_READY"
    WORLD_LOADING = "WORLD_LOADING"
    WORLD_READY = "WORLD_READY"
    CHUNKS_STABLE = "CHUNKS_STABLE"
    WARMUP = "WARMUP"
    MEASURING = "MEASURING"
    COLLECTING = "COLLECTING"
    CLASSIFYING = "CLASSIFYING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class FailureClass(str, Enum):
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    DEVICE_LOCKED = "DEVICE_LOCKED"
    DEVELOPER_MODE_REQUIRED = "DEVELOPER_MODE_REQUIRED"
    SIGNING_FAILURE = "SIGNING_FAILURE"
    INSTALL_UNKNOWN = "INSTALL_UNKNOWN"
    AGENT_UNREACHABLE = "AGENT_UNREACHABLE"
    STALE_DEBUGSERVER = "STALE_DEBUGSERVER"
    JIT_PROCESSOR_UNCONFIGURED = "JIT_PROCESSOR_UNCONFIGURED"
    JIT_ATTACH_FAILURE = "JIT_ATTACH_FAILURE"
    JIT_MODE_INSUFFICIENT = "JIT_MODE_INSUFFICIENT"
    JIT_VERIFICATION_FAILURE = "JIT_VERIFICATION_FAILURE"
    DYLD_VALIDATION_FAILURE = "DYLD_VALIDATION_FAILURE"
    RUNTIME_ABI_PRECHECK_FAILED = "RUNTIME_ABI_PRECHECK_FAILED"
    AMETHYST_CRASH = "AMETHYST_CRASH"
    AMETHYST_JETSAM = "AMETHYST_JETSAM"
    JVM_CRASH = "JVM_CRASH"
    JAVA_EXCEPTION = "JAVA_EXCEPTION"
    NATIVE_DYLIB_LOAD_FAILURE = "NATIVE_DYLIB_LOAD_FAILURE"
    MOD_LOADER_FAILURE = "MOD_LOADER_FAILURE"
    RENDERER_INIT_FAILURE = "RENDERER_INIT_FAILURE"
    HOT_PAYLOAD_PROVENANCE_MISMATCH = "HOT_PAYLOAD_PROVENANCE_MISMATCH"
    RETRY_REQUIRES_CHANGED_EVIDENCE = "RETRY_REQUIRES_CHANGED_EVIDENCE"
    METAL_VALIDATION_FAILURE = "METAL_VALIDATION_FAILURE"
    MC_READY_TIMEOUT = "MC_READY_TIMEOUT"
    WORLD_LOAD_TIMEOUT = "WORLD_LOAD_TIMEOUT"
    GRAPHICS_REGRESSION = "GRAPHICS_REGRESSION"
    PERFORMANCE_REGRESSION = "PERFORMANCE_REGRESSION"
    UNKNOWN = "UNKNOWN"


PROCESS_SCOPED_STAGES = {
    Stage.JIT_ATTACHING,
    Stage.JIT_HANDSHAKE_OK,
    Stage.JIT_RX_MAPPING_OK,
    Stage.DYLD_BYPASS_READY,
    Stage.RUNTIME_READY,
    Stage.PROFILE_READY,
    Stage.LAUNCH_ACCEPTED,
    Stage.JVM_STARTING,
    Stage.JVM_READY,
    Stage.MC_BOOTSTRAP,
    Stage.RENDERER_LOADING,
    Stage.RENDERER_READY,
    Stage.MENU_READY,
    Stage.WORLD_LOADING,
    Stage.WORLD_READY,
    Stage.CHUNKS_STABLE,
    Stage.WARMUP,
    Stage.MEASURING,
}

_VOLATILE_HEX = re.compile(r"0x[0-9A-Fa-f]+")
_VOLATILE_UUID = re.compile(r"\b[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\b")
_VOLATILE_LABELLED_NUMBER = re.compile(
    r"(?i)\b(pid|process(?:_id)?|port|fd)\s*([=:]?\s*)\d+\b"
)
_VOLATILE_LONG_NUMBER = re.compile(r"\b\d{10,}\b")


def failure_fingerprint(failure: FailureClass, detail: str) -> str:
    """Fingerprint a failure while preserving semantic error codes.

    UUIDs, addresses, explicitly labelled PID/port/FD values, and timestamp-like
    long integers are normalized. Unlabelled short numbers remain intact so
    errors such as E96 and E97 do not collapse to the same fingerprint.
    """
    normalized = _VOLATILE_UUID.sub("<uuid>", detail)
    normalized = _VOLATILE_HEX.sub("<hex>", normalized)
    normalized = _VOLATILE_LABELLED_NUMBER.sub(lambda match: f"{match.group(1)}{match.group(2)}<n>", normalized)
    normalized = _VOLATILE_LONG_NUMBER.sub("<n>", normalized)
    material = f"{failure.value}\n{normalized.strip()[:4096]}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()[:20]


@dataclass
class RunState:
    run_id: str
    bundle_id: str
    device_udid: str | None = None
    stage: Stage = Stage.IDLE
    failure: FailureClass | None = None
    failure_detail: str | None = None
    failure_fingerprint: str | None = None
    pid: int | None = None
    process_generation: str | None = None
    game_session_generation: str | None = None
    jit_attach_generation: str | None = None
    jit_exec_ready: bool = False
    dynamic_library_load_ready: bool = False
    app_state: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    candidate: dict[str, Any] = field(default_factory=dict)
    attempt_signature: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)

    def transition(self, stage: Stage, **evidence: Any) -> None:
        self.stage = stage
        self.updated_at = time()
        if evidence:
            self.evidence.update(evidence)

    def _clear_session_evidence(self) -> None:
        self.jit_exec_ready = False
        self.dynamic_library_load_ready = False
        self.jit_attach_generation = None
        for key in list(self.evidence):
            if key in {"last_event_source", "last_app_event"} or key.startswith(
                ("jit_", "dyld_", "jvm_", "renderer_", "minecraft_", "menu_", "world_", "chunks_", "benchmark_")
            ):
                self.evidence.pop(key, None)

    def begin_game_session(self, generation: str) -> None:
        if not generation:
            raise ValueError("game session generation must not be empty")
        self._clear_session_evidence()
        self.game_session_generation = generation
        self.failure = None
        self.failure_detail = None
        self.failure_fingerprint = None
        self.updated_at = time()

    def end_game_session(self) -> None:
        self._clear_session_evidence()
        self.game_session_generation = None
        self.app_state = "launcher"
        if self.stage in PROCESS_SCOPED_STAGES or self.stage in {Stage.PASS, Stage.FAIL, Stage.BLOCKED}:
            self.stage = Stage.AGENT_READY
        self.updated_at = time()

    def observe_process(self, pid: int, generation: str) -> bool:
        """Observe host process identity. Return True when all session-scoped proof was invalidated."""
        changed = self.pid is not None and (self.pid != pid or self.process_generation != generation)
        first_identity = self.pid is None or self.process_generation is None
        self.pid = pid
        self.process_generation = generation
        self.updated_at = time()
        if changed:
            self.end_game_session()
            self.app_state = None
            if self.stage in PROCESS_SCOPED_STAGES or self.stage in {Stage.PASS, Stage.FAIL, Stage.BLOCKED, Stage.AGENT_READY}:
                self.stage = Stage.APP_LAUNCHED
            self.failure = None
            self.failure_detail = None
            self.failure_fingerprint = None
        elif first_identity and self.stage == Stage.IDLE:
            self.stage = Stage.APP_LAUNCHED
        return changed

    def mark_jit(self, *, exec_ready: bool, dynamic_library_load_ready: bool, evidence: dict[str, Any]) -> None:
        self.jit_exec_ready = exec_ready
        self.dynamic_library_load_ready = dynamic_library_load_ready
        self.evidence.update(evidence)
        self.updated_at = time()
        if dynamic_library_load_ready:
            self.stage = Stage.DYLD_BYPASS_READY
        elif exec_ready:
            self.stage = Stage.JIT_RX_MAPPING_OK

    def fail(self, failure: FailureClass, detail: str, *, blocked: bool = False) -> None:
        self.failure = failure
        self.failure_detail = detail
        self.failure_fingerprint = failure_fingerprint(failure, detail)
        self.stage = Stage.BLOCKED if blocked else Stage.FAIL
        self.updated_at = time()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stage"] = self.stage.value
        data["failure"] = self.failure.value if self.failure else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        copy = dict(data)
        copy["stage"] = Stage(copy.get("stage", Stage.IDLE.value))
        failure = copy.get("failure")
        copy["failure"] = FailureClass(failure) if failure else None
        return cls(**copy)
