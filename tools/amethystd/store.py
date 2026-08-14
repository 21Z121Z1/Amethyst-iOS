from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from typing import Any

from .model import RunState


_INFRASTRUCTURE_FAILURES = frozenset({
    "DEVICE_NOT_FOUND",
    "DEVICE_DISCONNECTED",
    "DEVICE_LOCKED",
    "DEVELOPER_MODE_REQUIRED",
    "INSTALL_UNKNOWN",
    "AGENT_UNREACHABLE",
    "STALE_DEBUGSERVER",
    "JIT_PROCESSOR_UNCONFIGURED",
    "JIT_ATTACH_FAILURE",
    "JIT_MODE_INSUFFICIENT",
    "JIT_VERIFICATION_FAILURE",
})


class StateStore:
    def __init__(self, root: Path | str | None = None) -> None:
        configured = root or os.environ.get("AMETHYST_AGENT_HOME") or ".amethyst-agent"
        self.root = Path(configured).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(exist_ok=True)
        (self.root / "candidates").mkdir(exist_ok=True)

        runtime_override = os.environ.get("AMETHYST_AGENT_RUNTIME")
        if runtime_override:
            self.runtime_root = Path(runtime_override).expanduser().resolve()
        else:
            token = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
            base = Path("/tmp") if Path("/tmp").is_dir() else self.root
            self.runtime_root = base / f"amethyst-agent-{token}"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            self.runtime_root.chmod(0o700)
        except OSError:
            pass

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def retry_path(self) -> Path:
        return self.root / "retry-guard.json"

    @property
    def socket_path(self) -> Path:
        return self.runtime_root / "amethystd.sock"

    def artifact_dir(self, run_id: str) -> Path:
        path = self.root / "artifacts" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, state: RunState) -> None:
        self._atomic_json(self.state_path, state.to_dict())

    def load(self) -> RunState | None:
        if not self.state_path.exists():
            return None
        return RunState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("agent config must be a JSON object")
        return value

    def save_config(self, value: dict[str, Any]) -> None:
        allowed = {"device_udid", "bundle_id"}
        unexpected = set(value) - allowed
        if unexpected:
            raise ValueError(f"unsupported agent config keys: {sorted(unexpected)}")
        clean = {key: str(item) for key, item in value.items() if item is not None and str(item)}
        self._atomic_json(self.config_path, clean)

    def candidate_path(self, name: str) -> Path:
        if not name or len(name) > 96 or not all(char.isalnum() or char in "-_" for char in name):
            raise ValueError(f"invalid candidate name: {name!r}")
        return self.root / "candidates" / f"{name}.json"

    def save_candidate(self, name: str, value: dict[str, Any]) -> None:
        self._atomic_json(self.candidate_path(name), value)

    def load_candidate(self, name: str) -> dict[str, Any] | None:
        path = self.candidate_path(name)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    @staticmethod
    def attempt_signature(*, candidate_digest: str, profile: str, target: str, require_dynamic_library_load: bool) -> str:
        material = json.dumps(
            {
                "candidate_digest": candidate_digest,
                "profile": profile,
                "target": target,
                "require_dynamic_library_load": require_dynamic_library_load,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(material).hexdigest()[:24]

    def _retry_data(self) -> dict[str, Any]:
        if not self.retry_path.is_file():
            return {"version": 1, "attempts": {}}
        value = json.loads(self.retry_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("attempts"), dict):
            return {"version": 1, "attempts": {}}
        return value

    def retry_status(self, signature: str) -> dict[str, Any]:
        entry = self._retry_data()["attempts"].get(signature) or {}
        count = int(entry.get("consecutive_same_failure", 0))
        return {
            "blocked": count >= 2,
            "consecutive_same_failure": count,
            "last_failure": entry.get("failure"),
            "last_failure_fingerprint": entry.get("failure_fingerprint"),
        }

    @staticmethod
    def failure_affects_candidate_retry(failure: str) -> bool:
        """Only semantic candidate failures participate in unchanged-failure blocking.

        Device, transport and JIT orchestration failures remain visible in each
        run artifact but must not consume the two-strike renderer experiment
        budget. Otherwise a flaky Network/AFC or stale debugserver incident can
        prevent the next valid A/B attempt without any renderer evidence.
        """
        return failure not in _INFRASTRUCTURE_FAILURES

    def record_failure(self, signature: str, *, failure: str, fingerprint: str) -> None:
        if not self.failure_affects_candidate_retry(failure):
            return
        data = self._retry_data()
        previous = data["attempts"].get(signature) or {}
        same = previous.get("failure_fingerprint") == fingerprint
        data["attempts"][signature] = {
            "failure": failure,
            "failure_fingerprint": fingerprint,
            "consecutive_same_failure": int(previous.get("consecutive_same_failure", 0)) + 1 if same else 1,
            "updated_at": time(),
        }
        self._atomic_json(self.retry_path, data)

    def reset_retry_guard(self, reason: str) -> None:
        self._atomic_json(
            self.retry_path,
            {"version": 1, "attempts": {}, "last_reset": {"reason": reason, "timestamp": time()}},
        )

    def append_event(self, run_id: str, event: str, **payload: Any) -> None:
        record = {"timestamp": time(), "event": event, **payload}
        path = self.artifact_dir(run_id) / "host-events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            json.dump(value, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)