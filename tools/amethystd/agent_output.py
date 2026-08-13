from __future__ import annotations

import json
import os
from pathlib import Path
from time import time_ns
from typing import Any
from uuid import uuid4

DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024
MAX_DETAIL_CHARS = 4096


def _trim_text(value: Any, limit: int = MAX_DETAIL_CHARS) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + f"… <{len(value) - limit} chars omitted>"


def _state_summary(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    keys = (
        "run_id", "bundle_id", "stage", "failure", "failure_detail", "failure_fingerprint",
        "pid", "process_generation", "jit_exec_ready", "dynamic_library_load_ready",
        "app_state", "profile", "updated_at",
    )
    result = {key: value[key] for key in keys if key in value}
    if "failure_detail" in result:
        result["failure_detail"] = _trim_text(result["failure_detail"])
    evidence = value.get("evidence")
    if isinstance(evidence, dict):
        result["evidence_keys"] = sorted(str(key) for key in evidence)[:64]
    return result


def _artifact_summary(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key in ("ok", "run_id", "artifact_dir", "detail"):
        if key in value:
            result[key] = _trim_text(value[key])
    items = value.get("items")
    if isinstance(items, dict):
        result["items"] = {
            str(key): item if not isinstance(item, dict) else {
                subkey: _trim_text(subvalue)
                for subkey, subvalue in item.items()
                if subkey in {"ok", "bytes", "returncode", "error", "stderr", "sha256", "width", "height"}
            }
            for key, item in list(items.items())[:64]
        }
    return result


def summarize_for_agent(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": bool(value.get("ok"))}
    for key in (
        "error", "failure", "failure_fingerprint", "detail", "method", "accepted", "observed",
        "healthy", "socket", "phase", "mode", "key", "hold_ms",
    ):
        if key in value:
            result[key] = _trim_text(value[key])
    if "state" in value:
        result["state"] = _state_summary(value["state"])
    if "artifacts" in value:
        result["artifacts"] = _artifact_summary(value["artifacts"])
    if "items" in value and "artifacts" not in value:
        result["items"] = _artifact_summary({"items": value["items"]}).get("items", {})
    for key in (
        "probe", "configuration", "preflight", "host_preflight", "metrics", "diagnostic",
        "lines", "cursor", "remediation",
    ):
        if key in value:
            candidate = value[key]
            encoded = json.dumps(candidate, sort_keys=True, default=str)
            result[key] = candidate if len(encoded) <= 8192 else _trim_text(encoded, 8192)
    return result


def bounded_result(value: dict[str, Any], root: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    if max_bytes is None:
        try:
            max_bytes = int(os.environ.get("AMETHYSTCTL_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES))
        except ValueError:
            max_bytes = DEFAULT_MAX_OUTPUT_BYTES
    max_bytes = max(4096, min(int(max_bytes), 1024 * 1024))
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
    if len(raw) <= max_bytes:
        return value

    directory = root / "tool-output"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time_ns()}-{uuid4().hex[:12]}.json"
    path.write_bytes(raw)

    result = summarize_for_agent(value)
    result["_output_truncated"] = True
    result["_full_output"] = str(path)
    result["_full_output_bytes"] = len(raw)
    result["_max_output_bytes"] = max_bytes
    return result
