from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile


def _relative_path(root: Path, value: str) -> Path:
    logical = PurePosixPath(value)
    if logical.is_absolute() or ".." in logical.parts:
        raise ValueError(f"contract path must be relative and cannot escape its root: {value!r}")
    return root.joinpath(*logical.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_contract(manifest_path: str | Path) -> dict[str, Any]:
    """Verify local runtime/package invariants before any device launch.

    The manifest intentionally describes files and archive entries, not Minecraft-specific
    guesses. This catches failures such as a Java agent JAR omitting an anonymous inner
    class before the payload reaches the iPad.
    """
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("runtime contract version must be 1")
    root = manifest_file.parent
    failures: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    for item in manifest.get("files", []):
        path = _relative_path(root, str(item["path"]))
        observed: dict[str, Any] = {"kind": "file", "path": str(item["path"]), "exists": path.is_file()}
        if not path.is_file():
            failures.append({**observed, "reason": "missing"})
            continue
        observed["size"] = path.stat().st_size
        expected_size = item.get("size")
        if expected_size is not None and observed["size"] != int(expected_size):
            failures.append({**observed, "reason": "size_mismatch", "expected_size": int(expected_size)})
        expected_sha = item.get("sha256")
        if expected_sha:
            observed["sha256"] = _sha256(path)
            if observed["sha256"].lower() != str(expected_sha).lower():
                failures.append({**observed, "reason": "sha256_mismatch", "expected_sha256": expected_sha})
        evidence.append(observed)

    for item in manifest.get("archives", []):
        path = _relative_path(root, str(item["path"]))
        observed = {"kind": "archive", "path": str(item["path"]), "exists": path.is_file()}
        if not path.is_file():
            failures.append({**observed, "reason": "missing"})
            continue
        try:
            with ZipFile(path) as archive:
                entries = set(archive.namelist())
        except BadZipFile:
            failures.append({**observed, "reason": "invalid_zip"})
            continue
        required = [str(entry) for entry in item.get("required_entries", [])]
        forbidden = [str(entry) for entry in item.get("forbidden_entries", [])]
        missing = [entry for entry in required if entry not in entries]
        present_forbidden = [entry for entry in forbidden if entry in entries]
        observed.update(
            {
                "entry_count": len(entries),
                "required_entries": required,
                "missing_required_entries": missing,
                "present_forbidden_entries": present_forbidden,
            }
        )
        if missing:
            failures.append({**observed, "reason": "archive_entries_missing"})
        if present_forbidden:
            failures.append({**observed, "reason": "forbidden_archive_entries_present"})
        evidence.append(observed)

    return {
        "ok": not failures,
        "manifest": str(manifest_file),
        "failures": failures,
        "evidence": evidence,
    }
