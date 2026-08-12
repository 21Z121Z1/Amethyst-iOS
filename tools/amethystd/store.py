from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from typing import Any

from .model import RunState


class StateStore:
    def __init__(self, root: Path | str | None = None) -> None:
        configured = root or os.environ.get("AMETHYST_AGENT_HOME") or ".amethyst-agent"
        self.root = Path(configured).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(exist_ok=True)

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def socket_path(self) -> Path:
        return self.root / "amethystd.sock"

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
