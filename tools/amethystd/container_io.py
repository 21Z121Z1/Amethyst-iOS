from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, AsyncIterator
from uuid import uuid4

_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
_REMOTE_DOCUMENTS = "/Documents"
_STREAM_PREFIXES = ("agent-events/", "agent-lab/")
_STREAM_FILES = {"latestlog.txt"}
_STREAM_CHUNK_BYTES = 256 * 1024
_DEVICE_CONNECTION_TYPES = frozenset({"USB", "Network"})
_AFC_READ_ATTEMPTS = 3
_AFC_RETRY_BASE_DELAY = 0.15
_TRANSIENT_TRANSPORT_MARKERS = (
    "separator is not found",
    "ssl",
    "record layer",
    "baddev",
    "device not found",
    "devicenotfound",
    "connection reset",
    "connection aborted",
    "connection closed",
    "broken pipe",
    "unexpected eof",
    "usbmux",
)
_MACHO_MAGICS = {
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
    b"\xbe\xba\xfe\xca",
    b"\xbf\xba\xfe\xca",
}


def _device_connection_type() -> str:
    connection_type = os.environ.get("AMETHYST_DEVICE_CONNECTION_TYPE", "USB")
    if connection_type not in _DEVICE_CONNECTION_TYPES:
        raise ValueError("AMETHYST_DEVICE_CONNECTION_TYPE must be USB or Network")
    return connection_type


def _is_transient_transport_error(error: BaseException) -> bool:
    """Return True only for transport/session failures that are safe to retry on reads.

    Network-paired iOS devices can transiently drop the House Arrest/AFC service
    while the app and Minecraft process remain healthy. Read-only operations
    reopen the service and retry with bounded backoff; semantic/file-contract
    errors are never hidden by this policy.
    """
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True
    text = f"{type(error).__name__}: {error}".casefold()
    return any(marker in text for marker in _TRANSIENT_TRANSPORT_MARKERS)


def safe_component(value: str, label: str) -> str:
    if not _SAFE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def documents_path(relative: str) -> str:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid Documents-relative path: {relative!r}")
    return str(PurePosixPath(_REMOTE_DOCUMENTS) / path)


def _macho_code_signature(path: Path) -> dict[str, Any] | None:
    """Require a valid host-side signature before staging executable payload bytes."""
    with path.open("rb") as handle:
        if handle.read(4) not in _MACHO_MAGICS:
            return None

    codesign = shutil.which("codesign")
    if not codesign:
        raise RuntimeError(f"Mach-O payload requires codesign before staging: {path}")

    verification = subprocess.run(
        [codesign, "--verify", "--strict", "--verbose=2", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = "\n".join(part for part in (verification.stdout, verification.stderr) if part).strip()
    if verification.returncode != 0:
        raise RuntimeError(
            f"Mach-O payload is not validly code signed: {path}; "
            f"sign it with the AgentDebug development identity before staging; {detail or 'codesign verification failed'}"
        )

    display = subprocess.run(
        [codesign, "-dv", "--verbose=4", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    metadata: dict[str, Any] = {"verified": True}
    fields = {
        "Identifier": "identifier",
        "TeamIdentifier": "team_identifier",
        "CDHash": "cdhash",
        "Format": "format",
        "Authority": "authority",
    }
    for line in f"{display.stdout}\n{display.stderr}".splitlines():
        key, separator, value = line.partition("=")
        output_key = fields.get(key)
        if separator and output_key and value:
            metadata[output_key] = value
    return metadata


class AgentContainerClient:
    """Documents-relative Agent v2 transport using House Arrest/AFC.

    Growing logs/events never use one unbounded `get_file_contents` response.
    File size is checked first; unchanged files reuse the local copy, while a
    changed file is transferred through bounded `fread` chunks. This keeps AFC
    packet sizes deterministic and removes full-file transfers from no-change
    polling iterations.

    Read-only AFC operations reopen the House Arrest service on a small set of
    transient USB/network transport failures. State-changing writes remain
    single-attempt because an interrupted write has an ambiguous commit point.
    """

    def __init__(self, udid: str, bundle_id: str) -> None:
        if not udid:
            raise ValueError("device UDID is required")
        self.udid = udid
        self.bundle_id = bundle_id
        self._stream_cache: dict[str, bytes] = {}
        self._stream_sizes: dict[str, int] = {}

    @asynccontextmanager
    async def _afc(self) -> AsyncIterator[Any]:
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.house_arrest import HouseArrestService
        except ImportError as exc:
            raise RuntimeError(
                "pymobiledevice3 Python package is required by amethystd; install requirements-agent.txt"
            ) from exc

        async with await create_using_usbmux(
            serial=self.udid, autopair=False, connection_type=_device_connection_type()
        ) as lockdown:
            async with await HouseArrestService.create(
                lockdown, self.bundle_id, documents_only=True
            ) as afc:
                yield afc

    async def _retry_read(self, operation: Any) -> Any:
        """Retry an idempotent AFC read by reopening the service each attempt."""
        for attempt in range(_AFC_READ_ATTEMPTS):
            try:
                return await operation()
            except Exception as exc:
                transient = _is_transient_transport_error(exc)
                if not transient:
                    raise
                if attempt + 1 >= _AFC_READ_ATTEMPTS:
                    raise TimeoutError(
                        f"AFC read transport remained unavailable after {_AFC_READ_ATTEMPTS} attempts: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                await asyncio.sleep(_AFC_RETRY_BASE_DELAY * (2**attempt))
        raise AssertionError("unreachable AFC retry state")

    @staticmethod
    def _is_stream_artifact(relative: str) -> bool:
        return relative in _STREAM_FILES or relative.startswith(_STREAM_PREFIXES)

    @staticmethod
    async def _bounded_read(afc: Any, remote: str, size: int) -> bytes:
        handle = await afc.fopen(remote, "r")
        chunks: list[bytes] = []
        remaining = size
        try:
            while remaining > 0:
                requested = min(remaining, _STREAM_CHUNK_BYTES)
                chunk = await afc.fread(handle, requested)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            await afc.fclose(handle)
        return b"".join(chunks)

    async def _read_direct_optional(self, relative: str) -> bytes | None:
        try:
            from pymobiledevice3.exceptions import AfcFileNotFoundError
        except ImportError as exc:
            raise RuntimeError("pymobiledevice3 Python package is required by amethystd") from exc

        async def read_once() -> bytes | None:
            async with self._afc() as afc:
                try:
                    return await afc.get_file_contents(documents_path(relative))
                except AfcFileNotFoundError:
                    return None

        return await self._retry_read(read_once)

    async def _read_stream_optional(self, relative: str) -> bytes | None:
        try:
            from pymobiledevice3.exceptions import AfcFileNotFoundError
        except ImportError as exc:
            raise RuntimeError("pymobiledevice3 Python package is required by amethystd") from exc
        remote = documents_path(relative)

        async def read_once() -> bytes | None:
            async with self._afc() as afc:
                try:
                    info = await afc.stat(remote)
                except AfcFileNotFoundError:
                    self._stream_cache.pop(relative, None)
                    self._stream_sizes.pop(relative, None)
                    return None
                if info.get("st_ifmt") != "S_IFREG":
                    raise RuntimeError(f"device artifact is not a regular file: {relative}")
                size = int(info["st_size"])
                if self._stream_sizes.get(relative) == size and relative in self._stream_cache:
                    return self._stream_cache[relative]
                data = await self._bounded_read(afc, remote, size)
                self._stream_cache[relative] = data
                self._stream_sizes[relative] = size
                return data

        return await self._retry_read(read_once)

    async def read_tail(self, relative: str, *, max_bytes: int = 65536) -> dict[str, Any]:
        """Return only a bounded tail to a Codex-facing log query."""
        max_bytes = max(1024, min(int(max_bytes), 1024 * 1024))
        data = await self.read_optional(relative)
        if data is None:
            return {"ok": True, "exists": False, "path": relative, "data": b""}
        start = max(0, len(data) - max_bytes)
        return {
            "ok": True,
            "exists": True,
            "path": relative,
            "size": len(data),
            "offset": start,
            "next_offset": len(data),
            "eof": True,
            "data": data[start:],
        }

    async def read_optional(self, relative: str) -> bytes | None:
        if self._is_stream_artifact(relative):
            return await self._read_stream_optional(relative)
        return await self._read_direct_optional(relative)

    async def submit(self, request: dict[str, Any]) -> str:
        request_id = safe_component(str(request["request_id"]), "request_id")
        run_id = safe_component(str(request["run_id"]), "run_id")
        if request.get("protocol") != "amethyst-agent/v2":
            raise ValueError("only amethyst-agent/v2 requests are accepted by this transport")
        request["request_id"] = request_id
        request["run_id"] = run_id
        data = (json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n").encode()
        final_path = documents_path(f"agent-requests/{request_id}.json")
        temp_path = documents_path(f"agent-requests/.{request_id}.{uuid4().hex}.tmp")
        async with self._afc() as afc:
            await afc.makedirs(documents_path("agent-requests"))
            await afc.set_file_contents(temp_path, data)
            await afc.rename(temp_path, final_path)
        return request_id

    async def response(self, request_id: str) -> dict[str, Any] | None:
        request_id = safe_component(request_id, "request_id")
        raw = await self._read_direct_optional(f"agent-responses/{request_id}.json")
        if raw is None:
            return None
        value = json.loads(raw.decode("utf-8"))
        if value.get("request_id") != request_id:
            raise RuntimeError("response/request_id mismatch")
        return value

    async def request(self, request: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
        request_id = await self.submit(request)
        deadline = monotonic() + timeout
        delay = 0.05
        while monotonic() < deadline:
            response = await self.response(request_id)
            if response is not None:
                return response
            await asyncio.sleep(delay)
            delay = min(delay * 1.4, 0.5)
        raise TimeoutError(f"agent response timeout for {request_id}")

    @staticmethod
    def _jsonl(raw: bytes | None) -> list[dict[str, Any]]:
        if raw is None:
            return []
        events: list[dict[str, Any]] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        return events

    async def read_events(self, run_id: str) -> list[dict[str, Any]]:
        run_id = safe_component(run_id, "run_id")
        return self._jsonl(await self.read_optional(f"agent-events/{run_id}.jsonl"))

    async def prepare_lab_run(self, run_id: str, game_session_generation: str) -> None:
        run_id = safe_component(run_id, "run_id")
        game_session_generation = safe_component(game_session_generation, "game_session_generation")
        root = documents_path("agent-lab")
        async with self._afc() as afc:
            await afc.makedirs(root)
            for filename, value in (("current-run-id", run_id), ("current-session-id", game_session_generation)):
                temp = documents_path(f"agent-lab/.{filename}-{uuid4().hex}.tmp")
                final = documents_path(f"agent-lab/{filename}")
                await afc.set_file_contents(temp, (value + "\n").encode())
                try:
                    await afc.rm(final)
                except Exception:
                    pass
                await afc.rename(temp, final)

    async def read_lab_events(self, run_id: str, game_session_generation: str | None = None) -> list[dict[str, Any]]:
        run_id = safe_component(run_id, "run_id")
        if game_session_generation is not None:
            game_session_generation = safe_component(game_session_generation, "game_session_generation")
        events = self._jsonl(await self.read_optional(f"agent-lab/{run_id}.jsonl"))
        for event in events:
            if event.get("run_id") != run_id:
                raise RuntimeError("lab event/run_id mismatch")
            if game_session_generation is not None and event.get("game_session_generation") != game_session_generation:
                raise RuntimeError("lab event/game_session_generation mismatch")
        return events

    async def inspect_payload(self, payload_name: str) -> dict[str, Any]:
        payload_name = safe_component(payload_name, "payload_name")
        pointer_raw = await self._read_direct_optional(f"agent-payloads/active/{payload_name}.json")
        if pointer_raw is None:
            return {"ok": True, "name": payload_name, "active": False}
        pointer = json.loads(pointer_raw.decode("utf-8"))
        if not isinstance(pointer, dict) or pointer.get("name") != payload_name:
            raise RuntimeError("active payload pointer failed identity validation")
        stage = pointer.get("stage")
        digest = pointer.get("digest")
        if not isinstance(stage, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RuntimeError("active payload pointer is incomplete or has an invalid digest")
        digest = digest.lower()
        expected_stage = f"agent-payloads/.staging/{digest}"
        if stage.lower() != expected_stage:
            raise RuntimeError("active payload pointer stage/digest mismatch")
        manifest_raw = await self._read_direct_optional(f"{expected_stage}/manifest.json")
        manifest = json.loads(manifest_raw.decode("utf-8")) if manifest_raw is not None else None
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 1
            or manifest.get("name") != payload_name
            or str(manifest.get("digest", "")).lower() != digest
            or not isinstance(manifest.get("files"), list)
        ):
            raise RuntimeError("active payload manifest failed identity validation")
        pointer = {**pointer, "digest": digest, "stage": expected_stage}
        return {"ok": True, "name": payload_name, "active": True, "pointer": pointer, "manifest": manifest}

    async def clear_payload(self, payload_name: str) -> dict[str, Any]:
        payload_name = safe_component(payload_name, "payload_name")
        try:
            from pymobiledevice3.exceptions import AfcFileNotFoundError
        except ImportError as exc:
            raise RuntimeError("pymobiledevice3 Python package is required by amethystd") from exc
        before = await self.inspect_payload(payload_name)
        final = documents_path(f"agent-payloads/active/{payload_name}.json")
        async with self._afc() as afc:
            try:
                await afc.rm(final)
                cleared = True
            except AfcFileNotFoundError:
                cleared = False
        return {
            "ok": True,
            "name": payload_name,
            "active": False,
            "cleared": cleared,
            "previous": before if before.get("active") else None,
            "staging_preserved": True,
        }

    async def stage_payload(self, local_path: Path | str, payload_name: str) -> dict[str, Any]:
        payload_name = safe_component(payload_name, "payload_name")
        root = Path(local_path).resolve()
        if not root.exists():
            raise FileNotFoundError(root)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        manifest_files: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        for path in files:
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            if ".." in PurePosixPath(relative).parts:
                raise ValueError("payload path escapes root")
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            entry: dict[str, Any] = {"path": relative, "size": len(data), "sha256": sha}
            signature = _macho_code_signature(path)
            if signature is not None:
                entry["code_signature"] = signature
            manifest_files.append(entry)
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(bytes.fromhex(sha))
        deployment_digest = digest.hexdigest()
        stage_relative = f"agent-payloads/.staging/{deployment_digest}"
        stage_root = documents_path(stage_relative)
        manifest = {"version": 1, "name": payload_name, "digest": deployment_digest, "files": manifest_files}
        async with self._afc() as afc:
            await afc.makedirs(stage_root)
            for local, entry in zip(files, manifest_files, strict=True):
                remote = str(PurePosixPath(stage_root) / entry["path"])
                await afc.makedirs(str(PurePosixPath(remote).parent))
                data = local.read_bytes()
                await afc.set_file_contents(remote, data)
                observed = await afc.get_file_contents(remote)
                if len(observed) != entry["size"] or hashlib.sha256(observed).hexdigest() != entry["sha256"]:
                    raise IOError(f"payload verification failed for {entry['path']}")
            manifest_data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            await afc.set_file_contents(str(PurePosixPath(stage_root) / "manifest.json"), manifest_data)
            active_root = documents_path("agent-payloads/active")
            await afc.makedirs(active_root)
            pointer = json.dumps(
                {"name": payload_name, "digest": deployment_digest, "stage": stage_relative},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            temp = str(PurePosixPath(active_root) / f".{payload_name}.{uuid4().hex}.tmp")
            final = str(PurePosixPath(active_root) / f"{payload_name}.json")
            await afc.set_file_contents(temp, pointer)
            try:
                await afc.rm(final)
            except Exception:
                pass
            await afc.rename(temp, final)
        return manifest